#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./build.sh --version X.Y.Z [--out-dir DIR] [--sdist] [--wheel]

Options:
  --version X.Y.Z  Semantic version (or set VERSION env)
  --out-dir DIR    Output directory (default: dist)
  --sdist          Build source distribution only
  --wheel          Build wheel only
  -h, --help       Show this help
EOF
}

out_dir="dist"
sdist="false"
wheel="false"
version="${VERSION:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out-dir)
      out_dir="${2:-}"
      if [[ -z "$out_dir" ]]; then
        echo "Missing value for --out-dir" >&2
        exit 2
      fi
      shift 2
      ;;
    --version)
      version="${2:-}"
      if [[ -z "$version" ]]; then
        echo "Missing value for --version" >&2
        exit 2
      fi
      shift 2
      ;;
    --sdist)
      sdist="true"
      shift
      ;;
    --wheel)
      wheel="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$version" ]]; then
  echo "Missing --version" >&2
  exit 2
fi

semver_regex='^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$'
if [[ ! "$version" =~ $semver_regex ]]; then
  echo "Invalid version: $version" >&2
  exit 2
fi

python - "$version" <<'PY'
import re
import sys
from pathlib import Path

version = sys.argv[1]
targets = [
    (Path("pyproject.toml"), r'^(version\s*=\s*")([^"]+)(")\s*$'),
    (Path("revpty/__init__.py"), r'^(__version__\s*=\s*")([^"]+)(")\s*$'),
]

for path, pattern in targets:
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(
        pattern,
        lambda m: f"{m.group(1)}{version}{m.group(3)}",
        text,
        flags=re.M,
    )
    if count != 1:
        raise SystemExit(f"Version update failed: {path}")
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
PY

cmd=(python -m build --outdir "$out_dir")
if [[ "$sdist" == "true" && "$wheel" == "false" ]]; then
  cmd+=(--sdist)
elif [[ "$wheel" == "true" && "$sdist" == "false" ]]; then
  cmd+=(--wheel)
fi

"${cmd[@]}"
