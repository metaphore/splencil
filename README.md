# Splencil

![Splencil in action](assets/splencil-in-action.webp)

![Blender 4.5+](https://img.shields.io/badge/Blender-4.5%2B-F5792A?logo=blender&logoColor=white)
![OS: Windows macOS Linux](https://img.shields.io/badge/OS-Windows%20%7C%20macOS%20%7C%20Linux-2E7D32)
![Background removal optional](https://img.shields.io/badge/rembg-optional-6A5ACD)

Paste clipboard images into Blender Texture Paint as stencils. Optional background removal uses `rembg`.

## Install

Download [repo as zip](https://github.com/metaphore/splencil/archive/refs/heads/main.zip), then Blender > Preferences > Add-ons > Install from Disk. Enable **Splencil**.

For background removal, open Splencil preferences and click **Install Dependencies**. Manual fallback: `python -m pip install Pillow numpy rembg`.

## Use

Switch to **Texture Paint**, open the 3D View sidebar with `N`, then find **Splencil**.

| Panel | Actions |
|---|---|
| ![Splencil panel](assets/splencil-panel.png) | **Paste** `Ctrl+V`<br>Clipboard image -> stencil<br><br>**BG Remove** `Ctrl+Shift+V`<br>Paste with background removal<br><br>**Reset** `Ctrl+R`<br>Fit/reset stencil transform<br><br>**Stamp** `Ctrl+D`<br>Paint the current stencil once<br><br>**Flip Horizontal** `Ctrl+Shift+H`<br>Mirror stencil left to right; press again to restore<br><br>**Hardness** `Alt+F`<br>Brush edge hardness |
