#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./release.sh --version X.Y.Z [--notes "Release notes"]

Options:
  --version X.Y.Z    Semantic version (required)
  --notes "..."      Release notes (optional, will prompt if not provided)
  --no-pypi          Skip PyPI upload
  --no-github        Skip GitHub release
  -h, --help         Show this help

Environment:
  TWINE_PASSWORD     PyPI token (required for PyPI upload)

Examples:
  ./release.sh --version 0.5.22
  ./release.sh --version 0.5.22 --notes "Bug fixes"
EOF
}

version=""
notes=""
upload_pypi="true"
create_gh_release="true"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      version="${2:-}"
      if [[ -z "$version" ]]; then
        echo "Missing value for --version" >&2
        exit 2
      fi
      shift 2
      ;;
    --notes)
      notes="${2:-}"
      shift 2
      ;;
    --no-pypi)
      upload_pypi="false"
      shift
      ;;
    --no-github)
      create_gh_release="false"
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

# Check dependencies
if ! command -v twine &>/dev/null && [[ "$upload_pypi" == "true" ]]; then
  echo "twine not found. Install with: pip install twine" >&2
  exit 2
fi

if ! command -v gh &>/dev/null && [[ "$create_gh_release" == "true" ]]; then
  echo "gh CLI not found. Install from: https://cli.github.com/" >&2
  exit 2
fi

echo "=== Building revpty v$version ==="
bash build.sh --version "$version"

whl="dist/revpty-$version-py3-none-any.whl"
sdist="dist/revpty-$version.tar.gz"

if [[ ! -f "$whl" || ! -f "$sdist" ]]; then
  echo "Build artifacts not found" >&2
  exit 2
fi

# Upload to PyPI
if [[ "$upload_pypi" == "true" ]]; then
  if [[ -z "${TWINE_PASSWORD:-}" ]]; then
    echo "TWINE_PASSWORD not set (PyPI token)" >&2
    exit 2
  fi
  echo "=== Uploading to PyPI ==="
  twine upload "$whl" "$sdist" --username __token__ --password "$TWINE_PASSWORD"
  echo "✓ Published to PyPI: https://pypi.org/project/revpty/$version/"
fi

# Create GitHub release
if [[ "$create_gh_release" == "true" ]]; then
  echo "=== Creating GitHub Release ==="

  # Get release notes if not provided
  if [[ -z "$notes" ]]; then
    echo "Enter release notes (Ctrl+D to finish, or leave empty for default):"
    notes=$(cat)
  fi

  # Default notes if empty
  if [[ -z "$notes" ]]; then
    notes="Release v$version"
  fi

  gh release create "v$version" "$whl" "$sdist" \
    --title "v$version" \
    --notes "$notes"

  echo "✓ GitHub Release: https://github.com/ddmonster/revpty/releases/tag/v$version"
fi

echo ""
echo "=== Release v$version Complete ==="