#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
version="${1:-}"
if [[ $# -eq 0 ]]; then
  version="$(sed -nE '
    /^bl_info[[:space:]]*=[[:space:]]*\{/,/^\}/ {
      s/^[[:space:]]*"version":[[:space:]]*\(([0-9]+),[[:space:]]*([0-9]+),[[:space:]]*([0-9]+)\),[[:space:]]*$/\1.\2.\3/p
    }
  ' "$repo_root/splencil.py")"
fi
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  printf 'Error: expected a release version in splencil.py or as an argument (major.minor.patch).\nUsage: %s [version]\n' "$0" >&2
  exit 2
fi

dist_dir="$repo_root/dist"
stage_dir="$dist_dir/splencil"
zip_path="$dist_dir/splencil-$version.zip"

rm -rf "$stage_dir"
mkdir -p "$stage_dir/assets" "$stage_dir/helpers"

cp "$repo_root/splencil.py" "$stage_dir/__init__.py"
cp "$repo_root/assets/splencil-brush.png" "$stage_dir/assets/splencil-brush.png"
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
