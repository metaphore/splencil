# Splencil

![Splencil in action](assets/splencil-in-action.webp)

![Blender 4.5+](https://img.shields.io/badge/Blender-4.5%2B-F5792A?logo=blender&logoColor=white)
![OS: Windows macOS Linux](https://img.shields.io/badge/OS-Windows%20%7C%20macOS%20%7C%20Linux-2E7D32)
![Background removal optional](https://img.shields.io/badge/rembg-optional-6A5ACD)

Paste clipboard images into Blender Texture Paint as stencils. Optional background removal uses `rembg`.

## Install

1. Download the latest `splencil-*.zip` from GitHub Releases. Do not use GitHub's generated source-code zip.
2. In Blender: Edit > Preferences > Add-ons > Install from Disk, select the zip, then enable **Splencil**.
3. For background removal: expand Splencil preferences and click **Install Dependencies**. This needs internet access and may take a while.

If dependency install fails, set **Python executable** in Splencil preferences, click **Check Dependencies**, then **Install Dependencies** again. Manual fallback:

```sh
python -m pip install Pillow numpy rembg
```
