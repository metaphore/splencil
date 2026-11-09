"""Run with blender --background --factory-startup --python-exit-code 1 --python tests/test_horizontal_flip.py."""

import importlib.util
from pathlib import Path

import bpy


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("splencil", ROOT / "splencil.py")
splencil = importlib.util.module_from_spec(spec)
spec.loader.exec_module(splencil)


def flip_keymaps():
    return [
        item
        for keymap in bpy.context.window_manager.keyconfigs.addon.keymaps
        for item in keymap.keymap_items
        if item.idname == "paint.flip_splencil_horizontal"
    ]


splencil.register()
area = next(area for area in bpy.context.screen.areas if area.type == "VIEW_3D")
region = next(region for region in area.regions if region.type == "WINDOW")
with bpy.context.temp_override(area=area, region=region):
    assert not bpy.ops.paint.flip_splencil_horizontal.poll(), "Disabled outside Texture Paint"
    bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
    brush = bpy.data.brushes.new("FlipTestBrush", mode="TEXTURE_PAINT")
    brush.asset_mark()
    bpy.ops.brush.asset_activate(
        asset_library_type="LOCAL", relative_asset_identifier=f"Brush/{brush.name}"
    )
    assert splencil.get_active_texture_paint_brush(bpy.context) == brush
    assert not bpy.ops.paint.flip_splencil_horizontal.poll(), "Disabled without stencil"

    # Non-square image with distinct colors and alpha, shared by another texture.
    image = splencil.get_or_create_blender_image(
        3, 2, bytes((255, 0, 0, 255, 0, 255, 0, 128, 0, 0, 255, 0) * 2)
    )
    splencil.assign_stencil_to_active_brush(bpy.context, image)
    other_texture = bpy.data.textures.new("SharedImageTexture", type="IMAGE")
    other_texture.image = image
    pixels = list(image.pixels)
    brush.stencil_pos = (321.0, 123.0)
    brush.stencil_dimension = (180.0, 120.0)
    brush.texture_slot.angle = 0.7
    brush.texture_slot.scale = (1.5, 0.75, 1.0)
    original = (tuple(brush.stencil_pos), tuple(brush.stencil_dimension), brush.texture_slot.angle)

    assert bpy.ops.paint.flip_splencil_horizontal.poll()
    assert bpy.ops.paint.flip_splencil_horizontal() == {"FINISHED"}
    assert tuple(brush.texture_slot.scale) == (-1.5, 0.75, 1.0)
    assert (tuple(brush.stencil_pos), tuple(brush.stencil_dimension), brush.texture_slot.angle) == original
    assert list(image.pixels) == pixels and other_texture.image == image
    assert bpy.ops.paint.flip_splencil_horizontal() == {"FINISHED"}
    assert tuple(brush.texture_slot.scale) == (1.5, 0.75, 1.0)

    # Reset/fit must keep dimensions positive while mirrored.
    bpy.ops.paint.flip_splencil_horizontal()
    assert bpy.ops.paint.reset_splencil() == {"FINISHED"}
    assert all(value > 0 for value in brush.stencil_dimension)
    assert brush.texture_slot.scale.x == -1.5
    brush.texture_slot.map_mode = "VIEW_PLANE"
    assert not bpy.ops.paint.flip_splencil_horizontal.poll(), "Disabled for non-stencil textures"
    brush.texture_slot.map_mode = "STENCIL"
    brush.texture.image = None
    assert not bpy.ops.paint.flip_splencil_horizontal.poll(), "Disabled without stencil image"
    bpy.ops.object.mode_set(mode="OBJECT")

items = flip_keymaps()
assert len(items) == 1
assert (items[0].type, items[0].value, items[0].ctrl, items[0].shift, items[0].alt) == (
    "H", "PRESS", True, True, False
)
splencil.unregister()
assert not flip_keymaps()
splencil.register()
assert len(flip_keymaps()) == 1
splencil.unregister()
assert not flip_keymaps()
print("PASS: horizontal flip toggles, preserves pixels/transforms, guards context, survives reset; hotkey lifecycle clean")
