# Splencil

![Splencil in action](assets/splencil-in-action.webp)

![Blender 4.5+](https://img.shields.io/badge/Blender-4.5%2B-F5792A?logo=blender&logoColor=white)
![OS: Windows macOS Linux](https://img.shields.io/badge/OS-Windows%20%7C%20macOS%20%7C%20Linux-2E7D32)
![Background removal optional](https://img.shields.io/badge/rembg-optional-6A5ACD)

Paste clipboard images into Blender Texture Paint as stencils. Optional background removal uses `rembg`.

## Install

Download `splencil-*.zip` from Releases, then Blender > Preferences > Add-ons > Install from Disk. Enable **Splencil**.

For background removal, open Splencil preferences and click **Install Dependencies**. Manual fallback: `python -m pip install Pillow numpy rembg`.

## Use

In Blender, switch your object to **Texture Paint**, then open the 3D View sidebar with `N` and find **Splencil**.

![Splencil panel](assets/splencil-panel.png)

- **Paste** (`Ctrl+V`): clipboard image -> stencil
- **BG Remove** (`Ctrl+Shift+V`): paste with background removal
- **Reset** (`Ctrl+R`): fit/reset stencil transform
- **Stamp** (`Ctrl+D`): paint the current stencil once
- **Hardness** (`Alt+F`): brush edge hardness
- **Settings**: blend mode and rembg options
