#!/usr/bin/env bash
set -euo pipefail

version="${1:-1.0.0}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
dist_dir="$repo_root/dist"
stage_dir="$dist_dir/splencil"
zip_path="$dist_dir/splencil-$version.zip"

rm -rf "$stage_dir"
mkdir -p "$stage_dir/helpers"

cp "$repo_root/splencil.py" "$stage_dir/__init__.py"
cp "$repo_root/helpers/cliprembg.py" "$stage_dir/helpers/cliprembg.py"
rm -f "$zip_path"

if command -v zip >/dev/null 2>&1; then
  (cd "$dist_dir" && zip -qr "$(basename "$zip_path")" splencil)
else
  python_bin="${PYTHON:-}"
  if [[ -z "$python_bin" ]]; then
    if command -v python3 >/dev/null 2>&1; then
      python_bin=python3
    elif command -v python >/dev/null 2>&1; then
      python_bin=python
    else
      echo "zip not found and no python/python3 fallback available" >&2
      exit 1
    fi
  fi
  "$python_bin" - "$dist_dir" "$zip_path" <<'PY'
import pathlib
import sys
import zipfile

dist_dir = pathlib.Path(sys.argv[1])
zip_path = pathlib.Path(sys.argv[2])
with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted((dist_dir / "splencil").rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(dist_dir).as_posix())
PY
fi

echo "$zip_path"
