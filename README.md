# Splencil

![Splencil in action](assets/splencil-in-action.webp)

![Blender 4.5+](https://img.shields.io/badge/Blender-4.5%2B-F5792A?logo=blender&logoColor=white)
![OS: Windows macOS Linux](https://img.shields.io/badge/OS-Windows%20%7C%20macOS%20%7C%20Linux-2E7D32)
![Background removal optional](https://img.shields.io/badge/rembg-optional-6A5ACD)

Paste clipboard images into Blender Texture Paint as stencils. Optional background removal uses `rembg`.

## Install

1. Copy `splencil.py` plus `helpers/cliprembg.py` into Blender's `scripts/addons` folder, or install a zip/folder with both files.
2. Enable **Splencil** in Blender Preferences > Add-ons.
3. For background removal, use **Install Dependencies** in the Splencil addon preferences, or run:

```sh
python -m pip install Pillow numpy rembg
```

Set `CLIPREMBG_PYTHON` if Blender should use a specific Python executable.