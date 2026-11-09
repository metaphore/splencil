bl_info = {
    "name": "Splencil",
    "author": "Anton Chekulaev",
    "version": (0, 1, 0),
    "blender": (4, 5, 0),
    "location": "3D Viewport > Texture Paint",
    "description": "Paste and stamp clipboard images as Texture Paint stencils",
    "category": "Paint",
}

import binascii
import ctypes
import json
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

import blf
import bpy
from bpy.app.handlers import persistent
from bpy.props import EnumProperty, FloatProperty, StringProperty


IMAGE_NAME = "__SplencilImage"
TEXTURE_NAME = "__SplencilTexture"
BRUSH_NAME = "__SplencilBrush"
REMBG_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "helpers", "cliprembg.py")
REMBG_BUSY_MESSAGE = "Removing clipboard background..."
PYTHON_PROBE_CACHE = {}
REMBG_WORKER_PROCESS = None
INSTALL_DEPS_JOB = None
REMBG_MODEL_ITEMS = (
    ("u2netp", "u2netp", "Fastest/lightest general model"),
    ("birefnet-general-lite", "birefnet-general-lite", "Higher quality than u2netp, slower"),
    ("isnet-general-use", "isnet-general-use", "Clean general cutouts, slower"),
    ("u2net", "u2net", "General model, medium speed"),
    ("birefnet-general", "birefnet-general", "High quality, slow"),
    ("bria-rmbg", "bria-rmbg", "Modern model, very slow on CPU here"),
    ("u2net_human_seg", "u2net_human_seg", "People segmentation"),
    ("birefnet-portrait", "birefnet-portrait", "Portraits and people"),
    ("isnet-anime", "isnet-anime", "Anime/illustration"),
    ("u2net_cloth_seg", "u2net_cloth_seg", "Clothing segmentation"),
)
REMBG_EDGE_MODE_ITEMS = (
    ("decontaminate", "Natural", "Soft alpha with cheap edge color cleanup"),
    ("raw", "Raw Alpha", "Use model alpha directly"),
    ("alpha_matting", "Alpha Matting", "Refine soft edges; slower"),
    ("vitmatte", "ViTMatte", "Best fine edge recovery; slow and extra download"),
)

CF_DIB = 8
CF_DIBV5 = 17
BI_RGB = 0
BI_BITFIELDS = 3

user32 = None
kernel32 = None


def _init_windows_clipboard_api():
    global user32, kernel32
    if os.name != "nt":
        raise ClipboardImageError("Native clipboard image paste is not available on this OS")
    if user32 is not None and kernel32 is not None:
        return

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    user32.IsClipboardFormatAvailable.restype = ctypes.c_bool
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p

    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t


class ClipboardImageError(Exception):
    pass


def _read_clipboard_dib():
    _init_windows_clipboard_api()
    if not user32.OpenClipboard(None):
        raise ClipboardImageError("Could not open Windows clipboard")

    handle = None
    locked = None
    try:
        fmt = None
        if user32.IsClipboardFormatAvailable(CF_DIBV5):
            fmt = CF_DIBV5
        elif user32.IsClipboardFormatAvailable(CF_DIB):
            fmt = CF_DIB
        else:
            raise ClipboardImageError("Clipboard does not contain an image")

        handle = user32.GetClipboardData(fmt)
        if not handle:
            raise ClipboardImageError("Could not read clipboard image")

        size = kernel32.GlobalSize(handle)
        locked = kernel32.GlobalLock(handle)
        if not locked or size == 0:
            raise ClipboardImageError("Could not lock clipboard image")

        return ctypes.string_at(locked, size)
    finally:
        if locked:
            kernel32.GlobalUnlock(handle)
        user32.CloseClipboard()


def _mask_shift_and_bits(mask):
    if not mask:
        return 0, 0
    shift = 0
    while (mask & 1) == 0:
        mask >>= 1
        shift += 1
    bits = 0
    while mask & 1:
        bits += 1
        mask >>= 1
    return shift, bits


def _extract_masked(value, mask):
    shift, bits = _mask_shift_and_bits(mask)
    if bits == 0:
        return 255
    component = (value & mask) >> shift
    return int(round(component * 255 / ((1 << bits) - 1)))


def _decode_dib_to_rgba(dib):
    if len(dib) < 40:
        raise ClipboardImageError("Unsupported clipboard bitmap")

    header_size = struct.unpack_from("<I", dib, 0)[0]
    if header_size < 40 or len(dib) < header_size:
        raise ClipboardImageError("Unsupported clipboard bitmap header")

    width, raw_height = struct.unpack_from("<ii", dib, 4)
    planes, bit_count = struct.unpack_from("<HH", dib, 12)
    compression = struct.unpack_from("<I", dib, 16)[0]
    colors_used = struct.unpack_from("<I", dib, 32)[0]

    if planes != 1 or width <= 0 or raw_height == 0:
        raise ClipboardImageError("Unsupported clipboard bitmap")

    height = abs(raw_height)
    top_down = raw_height < 0

    if compression not in {BI_RGB, BI_BITFIELDS}:
        raise ClipboardImageError("Unsupported clipboard bitmap compression")

    masks = None
    mask_bytes = 0
    if compression == BI_BITFIELDS:
        if header_size == 40:
            if len(dib) < header_size + 12:
                raise ClipboardImageError("Unsupported clipboard bitmap masks")
            raw_masks = struct.unpack_from("<III", dib, header_size)
            mask_bytes = 12
            if bit_count == 32 and len(dib) >= header_size + 16:
                raw_masks += struct.unpack_from("<I", dib, header_size + 12)
                mask_bytes = 16
            masks = raw_masks
        elif header_size >= 56:
            masks = struct.unpack_from("<IIII", dib, 40)

    palette_entries = 0
    if bit_count <= 8:
        palette_entries = colors_used or (1 << bit_count)

    pixel_offset = header_size + mask_bytes + palette_entries * 4
    row_stride = ((width * bit_count + 31) // 32) * 4
    needed = pixel_offset + row_stride * height
    if len(dib) < needed:
        raise ClipboardImageError("Clipboard bitmap data is incomplete")

    palette = []
    if palette_entries:
        for i in range(palette_entries):
            b, g, r, _ = struct.unpack_from("<BBBB", dib, header_size + mask_bytes + i * 4)
            palette.append((r, g, b, 255))

    rgba = bytearray(width * height * 4)
    for y in range(height):
        src_y = height - 1 - y if top_down else y
        row = pixel_offset + src_y * row_stride
        out = y * width * 4

        if bit_count == 32:
            for x in range(width):
                value = struct.unpack_from("<I", dib, row + x * 4)[0]
                if masks:
                    r = _extract_masked(value, masks[0])
                    g = _extract_masked(value, masks[1])
                    b = _extract_masked(value, masks[2])
                    a = _extract_masked(value, masks[3]) if len(masks) > 3 and masks[3] else 255
                else:
                    b, g, r, a = struct.unpack_from("<BBBB", dib, row + x * 4)
                rgba[out:out + 4] = bytes((r, g, b, a))
                out += 4
        elif bit_count == 24:
            for x in range(width):
                b, g, r = struct.unpack_from("<BBB", dib, row + x * 3)
                rgba[out:out + 4] = bytes((r, g, b, 255))
                out += 4
        elif bit_count == 8:
            for x in range(width):
                rgba[out:out + 4] = bytes(palette[dib[row + x]])
                out += 4
        elif bit_count == 4:
            for x in range(width):
                byte = dib[row + x // 2]
                index = (byte >> 4) if x % 2 == 0 else (byte & 0x0F)
                rgba[out:out + 4] = bytes(palette[index])
                out += 4
        elif bit_count == 1:
            for x in range(width):
                byte = dib[row + x // 8]
                index = (byte >> (7 - (x % 8))) & 1
                rgba[out:out + 4] = bytes(palette[index])
                out += 4
        else:
            raise ClipboardImageError(f"Unsupported clipboard bitmap depth: {bit_count} bpp")

    return width, height, rgba


def get_clipboard_rgba():
    return _decode_dib_to_rgba(_read_clipboard_dib())


def _run_stdout_image_command(command, output_path):
    if not shutil.which(command[0]):
        return False
    try:
        with open(output_path, "wb") as file:
            completed = subprocess.run(
                command,
                stdout=file,
                stderr=subprocess.PIPE,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
    except Exception:
        return False
    return completed.returncode == 0 and os.path.getsize(output_path) > 0


def _run_file_image_command(command, output_path):
    if not shutil.which(command[0]):
        return False
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:
        return False
    return completed.returncode == 0 and os.path.getsize(output_path) > 0


def write_clipboard_image_file(output_path):
    if os.name == "nt":
        width, height, rgba_pixels = get_clipboard_rgba()
        write_rgba_png(output_path, width, height, rgba_pixels)
        return output_path

    if sys.platform == "darwin":
        if _run_file_image_command(["pngpaste", output_path], output_path):
            return output_path
        for fmt in ("png", "tiff", "jpeg"):
            if _run_stdout_image_command(["pbpaste", "-Prefer", fmt], output_path):
                return output_path

    for command in (
        ["wl-paste", "--no-newline", "--type", "image/png"],
        ["wl-paste", "--no-newline", "--type", "image/tiff"],
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
        ["xclip", "-selection", "clipboard", "-t", "image/tiff", "-o"],
        ["xsel", "--clipboard", "--output"],
    ):
        if _run_stdout_image_command(command, output_path):
            return output_path

    raise ClipboardImageError(
        "Clipboard image paste needs pngpaste/pbpaste on macOS or wl-paste/xclip/xsel on Linux"
    )


def get_clipboard_image():
    if os.name == "nt":
        width, height, rgba_pixels = get_clipboard_rgba()
        return get_or_create_blender_image(width, height, rgba_pixels)

    with tempfile.TemporaryDirectory(prefix="blender_clipboard_") as tmpdir:
        input_path = os.path.join(tmpdir, "clipboard_image.png")
        write_clipboard_image_file(input_path)
        return load_png_as_stencil_image(input_path)


def get_or_create_blender_image(width, height, rgba_pixels):
    image = bpy.data.images.get(IMAGE_NAME)
    if image and tuple(image.size) != (width, height):
        bpy.data.images.remove(image)
        image = None

    if image is None:
        image = bpy.data.images.new(IMAGE_NAME, width=width, height=height, alpha=True)

    image.is_runtime_data = True
    image.pixels.foreach_set([component / 255.0 for component in rgba_pixels])
    image.update()
    return image


def load_png_as_stencil_image(path):
    old_image = bpy.data.images.get(IMAGE_NAME)
    if old_image:
        bpy.data.images.remove(old_image)

    image = bpy.data.images.load(path, check_existing=False)
    image.name = IMAGE_NAME
    image.is_runtime_data = True
    try:
        # Keep pixels alive after the temporary PNG is deleted, without saving them.
        image.pack()
    except Exception:
        pass
    return image


def _png_chunk(chunk_type, payload):
    raw = chunk_type + payload
    crc = binascii.crc32(raw) & 0xFFFFFFFF
    return struct.pack("!I", len(payload)) + raw + struct.pack("!I", crc)


def write_rgba_png(path, width, height, rgba_pixels):
    rows = []
    for y in range(height - 1, -1, -1):
        start = y * width * 4
        rows.append(b"\x00" + bytes(rgba_pixels[start:start + width * 4]))

    payload = b"".join(rows)
    header = struct.pack("!IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", header)
    png += _png_chunk(b"IDAT", zlib.compress(payload, 9))
    png += _png_chunk(b"IEND", b"")

    with open(path, "wb") as file:
        file.write(png)


def python_candidates():
    seen = set()
    candidates = []

    prefs_getter = globals().get("get_addon_preferences")
    prefs = prefs_getter() if prefs_getter else None
    prefs_python = getattr(prefs, "dependency_python", "").strip() if prefs else ""
    if prefs_python:
        candidates.append(shlex.split(prefs_python))

    env_python = os.environ.get("CLIPREMBG_PYTHON")
    if env_python:
        candidates.append(shlex.split(env_python))
    if shutil.which("python"):
        candidates.append(["python"])
    if shutil.which("python3"):
        candidates.append(["python3"])
    if shutil.which("py"):
        candidates.append(["py", "-3"])

    for candidate in candidates:
        key = tuple(candidate)
        if key in seen:
            continue
        seen.add(key)
        yield candidate

def validate_rembg_python(python_cmd):
    key = tuple(python_cmd)
    if key in PYTHON_PROBE_CACHE:
        return PYTHON_PROBE_CACHE[key]

    probe = (
        "import importlib.util, sys; "
        "missing=[m for m in ('PIL','numpy','rembg') if importlib.util.find_spec(m) is None]; "
        "print(', '.join(missing)); "
        "sys.exit(1 if missing else 0)"
    )
    try:
        completed = subprocess.run(
            [*python_cmd, "-c", probe],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception as exc:
        result = (False, str(exc))
        PYTHON_PROBE_CACHE[key] = result
        return result

    if completed.returncode == 0:
        result = (True, None)
    else:
        message = (completed.stdout or completed.stderr or "missing rembg dependencies").strip()
        result = (False, f"missing {message}" if message else "missing rembg dependencies")
    PYTHON_PROBE_CACHE[key] = result
    return result


def get_dependency_status():
    if not os.path.exists(REMBG_HELPER):
        return False, None, f"rembg helper not found: {REMBG_HELPER}"

    last_error = None
    for python_cmd in python_candidates():
        ok, error = validate_rembg_python(python_cmd)
        if ok:
            return True, python_cmd, "Pillow, numpy, and rembg are available"
        last_error = f"{' '.join(python_cmd)}: {error}"

    return False, None, last_error or "No system Python found"


def update_dependency_status(context, force=False):
    prefs = get_addon_preferences(context)
    if not prefs:
        return False, None, "Addon preferences unavailable"
    if force:
        PYTHON_PROBE_CACHE.clear()
    ok, python_cmd, message = get_dependency_status()
    prefs.dependency_status = "Ready" if ok else "Missing dependencies"
    prefs.dependency_details = f"{' '.join(python_cmd)}: {message}" if python_cmd else message
    return ok, python_cmd, message

def start_rembg_process(input_path, output_path):
    if not os.path.exists(REMBG_HELPER):
        raise ClipboardImageError(f"rembg helper not found: {REMBG_HELPER}")

    last_error = None
    for python_cmd in python_candidates():
        ok, error = validate_rembg_python(python_cmd)
        if not ok:
            last_error = f"{' '.join(python_cmd)}: {error}"
            continue
        try:
            return subprocess.Popen(
                [*python_cmd, REMBG_HELPER, input_path, output_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:
            last_error = exc

    raise ClipboardImageError(f"Could not start system Python with PIL, numpy, and rembg: {last_error}")


def texture_paint_poll(context):
    obj = context.active_object
    return (
        context.area is not None
        and context.area.type == "VIEW_3D"
        and obj is not None
        and obj.mode == "TEXTURE_PAINT"
    )


def get_active_texture_paint_brush(context):
    brush = getattr(context, "brush", None)
    if brush is not None:
        return brush

    image_paint = getattr(context.tool_settings, "image_paint", None)
    return getattr(image_paint, "brush", None)


def activate_clipboard_brush_asset(context, source_brush):
    local_brush = bpy.data.brushes.get(BRUSH_NAME)
    if local_brush is None:
        local_brush = source_brush.copy()
        local_brush.name = BRUSH_NAME

    local_brush.use_paint_image = True
    local_brush.image_brush_type = getattr(source_brush, "image_brush_type", "DRAW")
    if local_brush.asset_data is None:
        local_brush.asset_mark()

    result = bpy.ops.brush.asset_activate(
        asset_library_type="LOCAL",
        relative_asset_identifier=f"Brush/{local_brush.name}",
    )
    if "FINISHED" not in result:
        raise ClipboardImageError("Could not activate local clipboard brush")

    return get_active_texture_paint_brush(context) or local_brush


def get_mutable_texture_paint_brush(context):
    brush = get_active_texture_paint_brush(context)
    if brush is None:
        raise ClipboardImageError("No active Texture Paint brush")
    if brush.library:
        brush = activate_clipboard_brush_asset(context, brush)
    return brush


def get_or_create_stencil_texture(image):
    texture = bpy.data.textures.get(TEXTURE_NAME)
    if texture is None:
        texture = bpy.data.textures.new(TEXTURE_NAME, type="IMAGE")
    else:
        texture.type = "IMAGE"
    texture.is_runtime_data = True
    image.is_runtime_data = True
    texture.image = image
    return texture


def assign_stencil_to_active_brush(context, image):
    brush = get_mutable_texture_paint_brush(context)
    texture = get_or_create_stencil_texture(image)

    image_paint = getattr(context.tool_settings, "image_paint", None)
    if image_paint and not image_paint.is_property_readonly("stencil_image"):
        image_paint.stencil_image = image

    brush.texture = texture
    brush.texture_slot.map_mode = "STENCIL"
    return brush, texture


def redraw_ui(context):
    for area in context.screen.areas:
        area.tag_redraw()


def reset_stencil_keep_position(context):
    brush = get_active_texture_paint_brush(context)
    old_pos = brush.stencil_pos.copy() if brush else None

    bpy.ops.brush.stencil_reset_transform(mask=False)
    bpy.ops.brush.stencil_fit_image_aspect(use_repeat=True, use_scale=True, mask=False)

    brush = get_active_texture_paint_brush(context)
    if brush and old_pos is not None:
        brush.stencil_pos = old_pos
        brush.texture_slot.angle = 0.0


def apply_stencil_image(context, image):
    assign_stencil_to_active_brush(context, image)
    redraw_ui(context)
    reset_stencil_keep_position(context)


def set_brush_edge_hardness(context, value):
    brush = get_mutable_texture_paint_brush(context)
    value = max(0.0, min(1.0, value))

    curve = brush.curve_distance_falloff
    curve.initialize()
    points = curve.curves[0].points
    while len(points) > 2:
        points.remove(points[-1])

    points[0].location = (0.0, 1.0)
    points[1].location = (1.0, 0.0)

    if value > 0.001:
        points.new(min(0.98, value), 1.0)

    brush.curve_distance_falloff_preset = "CUSTOM"
    curve.update()


def update_edge_hardness(self, context):
    try:
        set_brush_edge_hardness(context, self.splencil_edge_hardness / 100.0)
    except Exception:
        pass


def stop_rembg_worker():
    global REMBG_WORKER_PROCESS
    process = REMBG_WORKER_PROCESS
    REMBG_WORKER_PROCESS = None
    if process and process.poll() is None:
        try:
            process.terminate()
        except Exception:
            pass


def get_rembg_worker():
    global REMBG_WORKER_PROCESS
    if REMBG_WORKER_PROCESS and REMBG_WORKER_PROCESS.poll() is None:
        return REMBG_WORKER_PROCESS

    last_error = None
    for python_cmd in python_candidates():
        ok, error = validate_rembg_python(python_cmd)
        if not ok:
            last_error = f"{' '.join(python_cmd)}: {error}"
            continue
        try:
            env = os.environ.copy()
            env["CLIPREMBG_MODEL"] = get_rembg_model()
            env["CLIPREMBG_EDGE_MODE"] = get_rembg_edge_mode()
            REMBG_WORKER_PROCESS = subprocess.Popen(
                [*python_cmd, REMBG_HELPER, "--worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return REMBG_WORKER_PROCESS
        except Exception as exc:
            last_error = exc

    raise ClipboardImageError(f"Could not start rembg worker with PIL, numpy, and rembg: {last_error}")


def start_rembg_worker_job(input_path, output_path):
    state = {"done": False, "ok": False, "error": None}

    def run():
        try:
            worker = get_rembg_worker()
            if worker.poll() is not None:
                raise ClipboardImageError("rembg worker exited")
            worker.stdin.write(json.dumps({"input": input_path, "output": output_path}) + "\n")
            worker.stdin.flush()
            line = worker.stdout.readline()
            if not line:
                stop_rembg_worker()
                raise ClipboardImageError("rembg worker stopped without a result")
            response = json.loads(line)
            if not response.get("ok"):
                raise ClipboardImageError(response.get("error", "rembg worker failed"))
            state["ok"] = True
        except Exception as exc:
            state["error"] = str(exc)
        finally:
            state["done"] = True

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    state["thread"] = thread
    return state

def draw_rembg_busy_overlay():
    font_id = 0
    area = bpy.context.area
    x = 24
    y = 48
    if area:
        y = max(48, area.height - 56)
    blf.size(font_id, 16)
    blf.color(font_id, 1.0, 1.0, 1.0, 0.95)
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, REMBG_BUSY_MESSAGE)


def clear_rembg_busy_ui(context, handler):
    if handler:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handler, "WINDOW")
        except Exception:
            pass
    try:
        context.window.cursor_modal_restore()
    except Exception:
        pass
    try:
        context.window_manager.progress_end()
    except Exception:
        pass
    context.workspace.status_text_set(None)


def update_rembg_options(self, context):
    stop_rembg_worker()


def get_addon_preferences(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(__name__)
    return addon.preferences if addon else None


def get_rembg_model(context=None):
    prefs = get_addon_preferences(context)
    return prefs.rembg_model if prefs else "u2netp"


def get_rembg_edge_mode(context=None):
    prefs = get_addon_preferences(context)
    return prefs.rembg_edge_mode if prefs else "decontaminate"


class SPLENCIL_OT_check_dependencies(bpy.types.Operator):
    bl_idname = "preferences.splencil_check_dependencies"
    bl_label = "Check Dependencies"
    bl_options = {"REGISTER"}

    def execute(self, context):
        ok, _python_cmd, message = update_dependency_status(context, force=True)
        self.report({"INFO" if ok else "WARNING"}, message)
        return {"FINISHED"}


class SPLENCIL_OT_install_dependencies(bpy.types.Operator):
    bl_idname = "preferences.splencil_install_dependencies"
    bl_label = "Install Dependencies"
    bl_options = {"REGISTER"}

    _timer = None
    _process = None
    _log_file = None
    _log_path = None

    def execute(self, context):
        global INSTALL_DEPS_JOB
        if INSTALL_DEPS_JOB is not None:
            self.report({"WARNING"}, "Dependency install is already running")
            return {"CANCELLED"}

        prefs = get_addon_preferences(context)
        if not prefs:
            self.report({"ERROR"}, "Addon preferences unavailable")
            return {"CANCELLED"}

        python_cmd = next(python_candidates(), None)
        if not python_cmd:
            prefs.dependency_status = "No Python found"
            prefs.dependency_details = "Set Python executable, then try again"
            self.report({"ERROR"}, prefs.dependency_details)
            return {"CANCELLED"}

        try:
            self._log_file = tempfile.NamedTemporaryFile(
                "w+", prefix="splencil-pip-", suffix=".log", delete=False
            )
            self._log_path = self._log_file.name
            self._process = subprocess.Popen(
                [*python_cmd, "-m", "pip", "install", "Pillow", "numpy", "rembg"],
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:
            prefs.dependency_status = "Install failed"
            prefs.dependency_details = str(exc)
            self.report({"ERROR"}, f"Could not start pip: {exc}")
            return {"CANCELLED"}

        INSTALL_DEPS_JOB = self
        prefs.dependency_status = "Installing..."
        prefs.dependency_details = f"Running {' '.join(python_cmd)} -m pip install Pillow numpy rembg"
        self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if self._process and self._process.poll() is None:
            return {"RUNNING_MODAL"}

        global INSTALL_DEPS_JOB
        prefs = get_addon_preferences(context)
        returncode = self._process.returncode if self._process else 1
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if self._log_file:
            self._log_file.close()

        PYTHON_PROBE_CACHE.clear()
        if returncode == 0:
            ok, _python_cmd, message = update_dependency_status(context, force=True)
            self.report({"INFO" if ok else "WARNING"}, message)
        else:
            tail = "pip failed"
            if self._log_path and os.path.exists(self._log_path):
                with open(self._log_path, "r", encoding="utf-8", errors="replace") as file:
                    lines = file.readlines()[-8:]
                tail = "".join(lines).strip() or tail
            if prefs:
                prefs.dependency_status = "Install failed"
                prefs.dependency_details = tail[:600]
            self.report({"ERROR"}, "Dependency install failed")

        INSTALL_DEPS_JOB = None
        return {"FINISHED"}


class SPLENCIL_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    dependency_python: StringProperty(
        name="Python executable",
        description="Optional Python command for rembg dependencies; leave empty to auto-detect",
        default="",
    )
    dependency_status: StringProperty(default="Not checked")
    dependency_details: StringProperty(default="")
    rembg_model: EnumProperty(
        name="Background Removal Model",
        description="rembg model used by Ctrl+Shift+V",
        items=REMBG_MODEL_ITEMS,
        default="u2netp",
        update=update_rembg_options,
    )
    rembg_edge_mode: EnumProperty(
        name="Background Removal Edges",
        description="How rembg turns the model mask into transparent edges",
        items=REMBG_EDGE_MODE_ITEMS,
        default="decontaminate",
        update=update_rembg_options,
    )

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Background Removal Dependencies")
        status = self.dependency_status or "Not checked"
        icon = "CHECKMARK" if status == "Ready" else "ERROR"
        box.label(text=status, icon=icon)
        if self.dependency_details:
            box.label(text=self.dependency_details)
        box.prop(self, "dependency_python")
        row = box.row(align=True)
        row.operator(SPLENCIL_OT_check_dependencies.bl_idname, icon="VIEWZOOM")
        row.operator(SPLENCIL_OT_install_dependencies.bl_idname, icon="IMPORT")

        layout.prop(self, "rembg_model")
        layout.prop(self, "rembg_edge_mode")

class PAINT_OT_reset_splencil(bpy.types.Operator):
    bl_idname = "paint.reset_splencil"
    bl_label = "Reset Splencil"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return texture_paint_poll(context)

    def execute(self, context):
        try:
            bpy.ops.brush.stencil_reset_transform(mask=False)
            bpy.ops.brush.stencil_fit_image_aspect(use_repeat=True, use_scale=True, mask=False)
        except Exception as exc:
            self.report({"ERROR"}, f"Splencil reset failed: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class PAINT_OT_flip_splencil_horizontal(bpy.types.Operator):
    bl_idname = "paint.flip_splencil_horizontal"
    bl_label = "Flip Splencil Horizontally"
    bl_description = "Mirror the stencil left to right without changing its position, size, or rotation"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if not texture_paint_poll(context):
            return False
        brush = get_active_texture_paint_brush(context)
        return (
            brush is not None
            and brush.texture is not None
            and brush.texture.type == "IMAGE"
            and brush.texture.image is not None
            and brush.texture_slot.map_mode == "STENCIL"
        )

    def execute(self, context):
        try:
            brush = get_mutable_texture_paint_brush(context)
            # Mirror texture coordinates; negative stencil dimensions prevent painting.
            brush.texture_slot.scale.x = -brush.texture_slot.scale.x
            redraw_ui(context)
        except Exception as exc:
            self.report({"ERROR"}, f"Splencil horizontal flip failed: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class PAINT_OT_paste_splencil(bpy.types.Operator):
    bl_idname = "paint.paste_splencil"
    bl_label = "Paste Clipboard as Stencil"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return texture_paint_poll(context)

    def execute(self, context):
        try:
            image = get_clipboard_image()
            apply_stencil_image(context, image)
        except ClipboardImageError as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Splencil paste failed: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class PAINT_OT_paste_splencil_rembg(bpy.types.Operator):
    bl_idname = "paint.paste_splencil_rembg"
    bl_label = "Paste Clipboard as Stencil, Remove Background"
    bl_options = {"REGISTER", "UNDO"}

    _timer = None
    _job = None
    _tmpdir = None
    _output_path = None
    _draw_handler = None
    _started_at = 0.0
    _timeout_seconds = 180.0
    _running = False

    @classmethod
    def poll(cls, context):
        return texture_paint_poll(context)

    def execute(self, context):
        if type(self)._running:
            self.report({"WARNING"}, "Background removal is still running")
            return {"CANCELLED"}

        try:
            context.window.cursor_modal_set("WAIT")
            context.window_manager.progress_begin(0, 100)
            context.window_manager.progress_update(1)
            context.workspace.status_text_set(REMBG_BUSY_MESSAGE)
            self._draw_handler = bpy.types.SpaceView3D.draw_handler_add(
                draw_rembg_busy_overlay, (), "WINDOW", "POST_PIXEL"
            )
            self._tmpdir = tempfile.TemporaryDirectory(prefix="blender_cliprembg_")
            input_path = os.path.join(self._tmpdir.name, "input.png")
            self._output_path = os.path.join(self._tmpdir.name, "output.png")
            write_clipboard_image_file(input_path)
            self._job = start_rembg_worker_job(input_path, self._output_path)
            self._started_at = time.monotonic()
            type(self)._running = True
            self._timer = context.window_manager.event_timer_add(0.25, window=context.window)
            context.window_manager.modal_handler_add(self)
            return {"RUNNING_MODAL"}
        except ClipboardImageError as exc:
            clear_rembg_busy_ui(context, self._draw_handler)
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            clear_rembg_busy_ui(context, self._draw_handler)
            self.report({"ERROR"}, f"Splencil rembg paste failed: {exc}")
            return {"CANCELLED"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        elapsed = max(0.0, time.monotonic() - self._started_at)
        context.window_manager.progress_update(min(95, 5 + int(elapsed * 8)))
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()

        if elapsed > self._timeout_seconds and self._job and not self._job.get("done"):
            stop_rembg_worker()
            self._job["done"] = True
            self._job["error"] = "rembg timed out"

        if not self._job or not self._job.get("done"):
            return {"RUNNING_MODAL"}

        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

        try:
            if not self._job.get("ok"):
                self.report({"ERROR"}, f"rembg failed: {self._job.get('error') or 'unknown error'}")
                return {"CANCELLED"}
            if not os.path.exists(self._output_path):
                self.report({"ERROR"}, "rembg did not produce an output image")
                return {"CANCELLED"}

            image = load_png_as_stencil_image(self._output_path)
            apply_stencil_image(context, image)
        except Exception as exc:
            self.report({"ERROR"}, f"Splencil rembg paste failed: {exc}")
            return {"CANCELLED"}
        finally:
            clear_rembg_busy_ui(context, self._draw_handler)
            self._draw_handler = None
            self._job = None
            type(self)._running = False
            if self._tmpdir:
                self._tmpdir.cleanup()
                self._tmpdir = None

        return {"FINISHED"}


class PAINT_OT_stamp_splencil(bpy.types.Operator):
    bl_idname = "paint.stamp_splencil"
    bl_label = "Stamp Splencil"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return texture_paint_poll(context)

    def execute(self, context):
        try:
            brush = get_active_texture_paint_brush(context)
            if brush is None or brush.texture is None or brush.texture.image is None:
                self.report({"WARNING"}, "No active stencil to stamp")
                return {"CANCELLED"}

            image = brush.texture.image
            width, height = image.size
            center_x, center_y = brush.stencil_pos
            old_size = brush.size
            old_strength = brush.strength
            old_stroke_method = brush.stroke_method

            try:
                brush.size = max(1, int((width * width + height * height) ** 0.5))
                brush.stroke_method = "DOTS"
                stroke = [{
                    "name": "",
                    "location": (0.0, 0.0, 0.0),
                    "mouse": (float(center_x), float(center_y)),
                    "mouse_event": (float(center_x), float(center_y)),
                    "pressure": 1.0,
                    "size": float(brush.size),
                    "x_tilt": 0.0,
                    "y_tilt": 0.0,
                    "is_start": True,
                    "time": 0.0,
                }]
                result = bpy.ops.paint.image_paint(stroke=stroke, mode="NORMAL")
                if "FINISHED" not in result:
                    self.report({"ERROR"}, "Stencil stamp did not finish")
                    return {"CANCELLED"}
            finally:
                brush.size = old_size
                brush.strength = old_strength
                brush.stroke_method = old_stroke_method

            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Splencil stamp failed: {exc}")
            return {"CANCELLED"}


class SPLENCIL_PT_texture_paint_panel:
    @classmethod
    def poll(cls, context):
        return texture_paint_poll(context)


class SPLENCIL_PT_view3d_panel(SPLENCIL_PT_texture_paint_panel, bpy.types.Panel):
    bl_label = "Splencil"
    bl_idname = "SPLENCIL_PT_view3d_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Splencil"

    def draw(self, context):
        pass


class SPLENCIL_PT_actions_panel(SPLENCIL_PT_texture_paint_panel, bpy.types.Panel):
    bl_label = "Actions"
    bl_idname = "SPLENCIL_PT_actions_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Splencil"
    bl_parent_id = "SPLENCIL_PT_view3d_panel"

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.operator(PAINT_OT_paste_splencil.bl_idname, text="Paste", icon="PASTEDOWN")
        row.operator(PAINT_OT_paste_splencil_rembg.bl_idname, text="BG Remove", icon="IMAGE_ALPHA")
        row = layout.row(align=True)
        row.operator(PAINT_OT_reset_splencil.bl_idname, text="Reset", icon="FILE_REFRESH")
        row.operator(PAINT_OT_stamp_splencil.bl_idname, text="Stamp", icon="BRUSH_DATA")
        layout.operator(PAINT_OT_flip_splencil_horizontal.bl_idname, text="Flip Horizontal", icon="ARROW_LEFTRIGHT")


class SPLENCIL_PT_settings_panel(SPLENCIL_PT_texture_paint_panel, bpy.types.Panel):
    bl_label = "Settings"
    bl_idname = "SPLENCIL_PT_settings_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Splencil"
    bl_parent_id = "SPLENCIL_PT_view3d_panel"

    def draw(self, context):
        layout = self.layout
        brush = get_active_texture_paint_brush(context)

        if brush:
            layout.prop(brush, "blend", text="Blend")
        else:
            row = layout.row()
            row.enabled = False
            row.label(text="Blend: No active brush")


class SPLENCIL_PT_rembg_panel(SPLENCIL_PT_texture_paint_panel, bpy.types.Panel):
    bl_label = "Background Removal"
    bl_idname = "SPLENCIL_PT_rembg_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Splencil"
    bl_parent_id = "SPLENCIL_PT_view3d_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        layout.label(text="Settings passed to rembg")
        prefs = get_addon_preferences(context)
        if prefs:
            layout.prop(prefs, "rembg_model", text="Model")
            layout.prop(prefs, "rembg_edge_mode", text="Edges")


class SPLENCIL_PT_hotkeys_panel(SPLENCIL_PT_texture_paint_panel, bpy.types.Panel):
    bl_label = "Hotkeys"
    bl_idname = "SPLENCIL_PT_hotkeys_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Splencil"
    bl_parent_id = "SPLENCIL_PT_view3d_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        layout.label(text="Ctrl+V Paste")
        layout.label(text="Ctrl+Shift+V BG Remove")
        layout.label(text="Ctrl+R Reset")
        layout.label(text="Ctrl+D Stamp")
        layout.label(text="Ctrl+Shift+H Flip Horizontal")
        layout.label(text="Alt+F Hardness")


LEGACY_KEYMAP_SUFFIX = "_".join(("clipboard", "stencil"))


SPLENCIL_KEYMAP_IDNAMES = {
    PAINT_OT_paste_splencil.bl_idname,
    PAINT_OT_paste_splencil_rembg.bl_idname,
    PAINT_OT_reset_splencil.bl_idname,
    PAINT_OT_stamp_splencil.bl_idname,
    PAINT_OT_flip_splencil_horizontal.bl_idname,
    f"paint.paste_{LEGACY_KEYMAP_SUFFIX}",
    f"paint.paste_{LEGACY_KEYMAP_SUFFIX}_rembg",
    f"paint.reset_{LEGACY_KEYMAP_SUFFIX}",
    f"paint.stamp_{LEGACY_KEYMAP_SUFFIX}",
    "wm.radial_control",
}


def remove_clipboard_keymaps():
    for keymap, item in list(addon_keymaps):
        try:
            keymap.keymap_items.remove(item)
        except Exception:
            pass
    addon_keymaps.clear()

    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if not keyconfig:
        return
    keymap = keyconfig.keymaps.get("3D View")
    if not keymap:
        return

    for item in list(keymap.keymap_items):
        if item.idname in SPLENCIL_KEYMAP_IDNAMES:
            keymap.keymap_items.remove(item)


@persistent
def mark_stencil_runtime(_unused=None):
    """Exclude disposable stencils, including those saved by older addon versions."""
    for collection, name in (
        (bpy.data.images, IMAGE_NAME),
        (bpy.data.textures, TEXTURE_NAME),
    ):
        datablock = collection.get(name)
        if datablock is not None and datablock.library is None:
            datablock.is_runtime_data = True


def register():
    remove_clipboard_keymaps()
    # Blender restricts bpy.data during add-on registration.
    if not bpy.app.timers.is_registered(mark_stencil_runtime):
        bpy.app.timers.register(mark_stencil_runtime, first_interval=0.0)
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.save_pre):
        if mark_stencil_runtime not in handlers:
            handlers.append(mark_stencil_runtime)

    bpy.types.Scene.splencil_edge_hardness = FloatProperty(
        name="Brush Edge Hardness %",
        description="Texture Paint brush falloff edge hardness",
        min=0.0,
        max=100.0,
        default=0.0,
        update=update_edge_hardness,
    )

    bpy.utils.register_class(SPLENCIL_OT_check_dependencies)
    bpy.utils.register_class(SPLENCIL_OT_install_dependencies)
    bpy.utils.register_class(SPLENCIL_AddonPreferences)
    bpy.utils.register_class(PAINT_OT_paste_splencil)
    bpy.utils.register_class(PAINT_OT_paste_splencil_rembg)
    bpy.utils.register_class(PAINT_OT_reset_splencil)
    bpy.utils.register_class(PAINT_OT_stamp_splencil)
    bpy.utils.register_class(PAINT_OT_flip_splencil_horizontal)
    bpy.utils.register_class(SPLENCIL_PT_view3d_panel)
    bpy.utils.register_class(SPLENCIL_PT_actions_panel)
    bpy.utils.register_class(SPLENCIL_PT_settings_panel)
    bpy.utils.register_class(SPLENCIL_PT_rembg_panel)
    bpy.utils.register_class(SPLENCIL_PT_hotkeys_panel)

    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig:
        keymap = keyconfig.keymaps.new(name="3D View", space_type="VIEW_3D", region_type="WINDOW")

        item = keymap.keymap_items.new(
            PAINT_OT_paste_splencil.bl_idname,
            type="V",
            value="PRESS",
            ctrl=True,
        )
        addon_keymaps.append((keymap, item))

        rembg_item = keymap.keymap_items.new(
            PAINT_OT_paste_splencil_rembg.bl_idname,
            type="V",
            value="PRESS",
            ctrl=True,
            shift=True,
        )
        addon_keymaps.append((keymap, rembg_item))

        reset_item = keymap.keymap_items.new(
            PAINT_OT_reset_splencil.bl_idname,
            type="R",
            value="PRESS",
            ctrl=True,
        )
        addon_keymaps.append((keymap, reset_item))

        stamp_item = keymap.keymap_items.new(
            PAINT_OT_stamp_splencil.bl_idname,
            type="D",
            value="PRESS",
            ctrl=True,
        )
        addon_keymaps.append((keymap, stamp_item))

        flip_item = keymap.keymap_items.new(
            PAINT_OT_flip_splencil_horizontal.bl_idname,
            type="H",
            value="PRESS",
            ctrl=True,
            shift=True,
        )
        addon_keymaps.append((keymap, flip_item))

        hardness_item = keymap.keymap_items.new(
            "wm.radial_control",
            type="F",
            value="PRESS",
            alt=True,
        )
        hardness_item.properties.data_path_primary = "scene.splencil_edge_hardness"
        hardness_item.properties.rotation_path = "tool_settings.image_paint.brush.texture_slot.angle"
        hardness_item.properties.color_path = "tool_settings.image_paint.brush.cursor_color_add"
        hardness_item.properties.image_id = "tool_settings.image_paint.brush"
        addon_keymaps.append((keymap, hardness_item))


def unregister():
    if bpy.app.timers.is_registered(mark_stencil_runtime):
        bpy.app.timers.unregister(mark_stencil_runtime)
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.save_pre):
        if mark_stencil_runtime in handlers:
            handlers.remove(mark_stencil_runtime)
    stop_rembg_worker()
    remove_clipboard_keymaps()

    if hasattr(bpy.types.Scene, "splencil_edge_hardness"):
        del bpy.types.Scene.splencil_edge_hardness

    for cls in (
        SPLENCIL_PT_hotkeys_panel,
        SPLENCIL_PT_rembg_panel,
        SPLENCIL_PT_settings_panel,
        SPLENCIL_PT_actions_panel,
        SPLENCIL_PT_view3d_panel,
        PAINT_OT_stamp_splencil,
        PAINT_OT_flip_splencil_horizontal,
        PAINT_OT_reset_splencil,
        PAINT_OT_paste_splencil_rembg,
        PAINT_OT_paste_splencil,
        SPLENCIL_AddonPreferences,
        SPLENCIL_OT_install_dependencies,
        SPLENCIL_OT_check_dependencies,
    ):
        if getattr(cls, "bl_rna", None) is None:
            continue
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


addon_keymaps = []


if __name__ == "__main__":
    register()
