#!/usr/bin/env python3
import json
import os
import sys
import traceback
from pathlib import Path

import numpy as np
from PIL import Image
from rembg import remove, new_session


def make_session():
    model = os.environ.get("CLIPREMBG_MODEL", "u2netp")
    return new_session(model)


def remove_background(src, dst, session):
    img = Image.open(src).convert("RGBA")
    rgb = np.array(img.convert("RGB"))
    edge_mode = os.environ.get("CLIPREMBG_EDGE_MODE", "decontaminate")
    remove_options = {
        "session": session,
        "post_process_mask": False,
    }
    if edge_mode == "decontaminate":
        remove_options["decontaminate"] = True
    elif edge_mode == "alpha_matting":
        remove_options.update({
            "alpha_matting": True,
            "alpha_matting_foreground_threshold": int(os.environ.get("CLIPREMBG_ALPHA_FOREGROUND", "240")),
            "alpha_matting_background_threshold": int(os.environ.get("CLIPREMBG_ALPHA_BACKGROUND", "10")),
            "alpha_matting_erode_size": int(os.environ.get("CLIPREMBG_ALPHA_ERODE", "3")),
        })
    elif edge_mode == "vitmatte":
        remove_options.update({
            "vitmatte": True,
            "vitmatte_model": os.environ.get("CLIPREMBG_VITMATTE_MODEL", "small-distinctions-646"),
            "alpha_matting_foreground_threshold": int(os.environ.get("CLIPREMBG_ALPHA_FOREGROUND", "240")),
            "alpha_matting_background_threshold": int(os.environ.get("CLIPREMBG_ALPHA_BACKGROUND", "10")),
            "alpha_matting_erode_size": int(os.environ.get("CLIPREMBG_ALPHA_ERODE", "3")),
        })

    result = remove(rgb, **remove_options)
    if isinstance(result, Image.Image):
        rgba = np.array(result.convert("RGBA"))
    else:
        rgba = np.asarray(result)
        if rgba.dtype != np.uint8:
            rgba = np.clip(rgba, 0, 255).astype(np.uint8)
        if rgba.ndim == 2:
            rgba = np.array(Image.fromarray(rgba, "L").convert("RGBA"))
        elif rgba.shape[2] == 3:
            alpha_channel = np.full(rgba.shape[:2] + (1,), 255, dtype=np.uint8)
            rgba = np.concatenate((rgba, alpha_channel), axis=2)
        else:
            rgba = rgba[:, :, :4].copy()

    alpha = rgba[:, :, 3].astype(np.float32) / 255.0
    rgba[:, :, 0] = (rgba[:, :, 0] * alpha).astype(np.uint8)
    rgba[:, :, 1] = (rgba[:, :, 1] * alpha).astype(np.uint8)
    rgba[:, :, 2] = (rgba[:, :, 2] * alpha).astype(np.uint8)

    Image.fromarray(rgba, "RGBA").save(dst)


def worker():
    session = make_session()
    for line in sys.stdin:
        try:
            job = json.loads(line)
            remove_background(Path(job["input"]), Path(job["output"]), session)
            print(json.dumps({"ok": True}), flush=True)
        except Exception as exc:
            print(json.dumps({
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }), flush=True)
    return 0


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--worker":
        return worker()
    if len(sys.argv) != 3:
        print("usage: cliprembg.py input.png output.png", file=sys.stderr)
        return 2

    session = make_session()
    remove_background(Path(sys.argv[1]), Path(sys.argv[2]), session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
