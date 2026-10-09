"""Run with blender --background --factory-startup --python-exit-code 1 --python tests/test_stencil_lifetime.py."""

import importlib.util
from pathlib import Path
import tempfile

import bpy


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("splencil", ROOT / "splencil.py")
splencil = importlib.util.module_from_spec(spec)
spec.loader.exec_module(splencil)
RGBA = bytes((255, 0, 0, 255, 0, 255, 0, 128, 0, 0, 255, 255, 255, 255, 255, 0))


def attach_stencil(image):
    texture = splencil.get_or_create_stencil_texture(image)
    brush = bpy.data.brushes.new("TestBrush", mode="TEXTURE_PAINT")
    brush.use_fake_user = True
    brush.texture = texture
    bpy.context.scene.tool_settings.image_paint.stencil_image = image
    return brush, texture


def assert_saved_without_stencil(path):
    bpy.ops.wm.open_mainfile(filepath=str(path))
    assert splencil.IMAGE_NAME not in bpy.data.images
    assert splencil.TEXTURE_NAME not in bpy.data.textures
    assert bpy.data.brushes["TestBrush"].texture is None
    assert bpy.context.scene.tool_settings.image_paint.stencil_image is None
    canvas = bpy.data.images["PaintedCanvas"]
    assert canvas.packed_file is not None
    assert not canvas.is_runtime_data
    assert abs(canvas.pixels[0] - 0.25) < 0.01


def clear_test_data():
    for collection in (bpy.data.brushes, bpy.data.textures, bpy.data.images):
        for datablock in list(collection):
            collection.remove(datablock)
    canvas = bpy.data.images.new("PaintedCanvas", width=2, height=2, alpha=True)
    canvas.pixels.foreach_set([0.25, 0.5, 0.75, 1.0] * 4)
    canvas.use_fake_user = True
    canvas.pack()


with tempfile.TemporaryDirectory(prefix="splencil_save_test_") as tmp:
    tmp = Path(tmp)
    splencil.register()
    for auto_pack in (False, True):
        for kind in ("generated", "png"):
            clear_test_data()
            bpy.data.use_autopack = auto_pack
            if kind == "generated":
                image = splencil.get_or_create_blender_image(2, 2, RGBA)
                assert splencil.get_or_create_blender_image(2, 2, RGBA) == image
            else:
                png = tmp / "clipboard.png"
                splencil.write_rgba_png(str(png), 2, 2, RGBA)
                image = splencil.load_png_as_stencil_image(str(png))
                png.unlink()
                assert image.packed_file is not None
            brush, texture = attach_stencil(image)
            assert image.is_runtime_data and texture.is_runtime_data
            original_pixels = list(image.pixels)
            # Also verify the save handler repairs legacy/undo-restored flags.
            image.is_runtime_data = False
            texture.is_runtime_data = False
            path = tmp / f"{kind}_{auto_pack}.blend"
            bpy.ops.wm.save_as_mainfile(filepath=str(path), check_existing=False)
            assert image.is_runtime_data and texture.is_runtime_data
            assert brush.texture == texture and texture.image == image
            assert bpy.context.scene.tool_settings.image_paint.stencil_image == image
            assert list(image.pixels) == original_pixels
            assert_saved_without_stencil(path)
            print(f"PASS: {kind}, Auto Pack={auto_pack}: stencil usable after save, absent after reload; canvas preserved")

    # Simulate a project saved by the old addon, then reopen with the fix active.
    clear_test_data()
    image = splencil.get_or_create_blender_image(2, 2, RGBA)
    brush, texture = attach_stencil(image)
    image.pack()
    image.is_runtime_data = False
    texture.is_runtime_data = False
    splencil.unregister()
    assert splencil.mark_stencil_runtime not in bpy.app.handlers.save_pre
    assert splencil.mark_stencil_runtime not in bpy.app.handlers.load_post
    legacy = tmp / "legacy.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(legacy), check_existing=False)
    splencil.register()
    bpy.ops.wm.open_mainfile(filepath=str(legacy))
    assert bpy.data.images[splencil.IMAGE_NAME].is_runtime_data
    assert bpy.data.textures[splencil.TEXTURE_NAME].is_runtime_data
    cleaned = tmp / "legacy_cleaned.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(cleaned), check_existing=False)
    assert_saved_without_stencil(cleaned)
    splencil.unregister()
    print("PASS: legacy packed stencil migrated on load; handlers removed on unregister")
