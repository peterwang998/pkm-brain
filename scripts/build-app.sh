#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT/app"

if ! command -v xcodegen >/dev/null 2>&1; then
  echo "xcodegen is required. Install with: brew install xcodegen" >&2
  exit 127
fi

cd "$ROOT"
uv build --wheel
mkdir -p "$APP_DIR/Resources/runtime" "$APP_DIR/Resources/bin"
cp .python-version "$APP_DIR/Resources/runtime/python-version"
uv export --frozen --no-dev --extra embeddings --no-emit-project > "$APP_DIR/Resources/runtime/requirements.lock"
rm -f "$APP_DIR/Resources/runtime"/pkm_brain-*.whl
cp dist/pkm_brain-*.whl "$APP_DIR/Resources/runtime/"
if command -v uv >/dev/null 2>&1; then
  rm -f "$APP_DIR/Resources/bin/uv"
  cp "$(command -v uv)" "$APP_DIR/Resources/bin/uv"
fi

cd "$APP_DIR"
xcodegen generate
xcodebuild \
  -project "PKM Brain.xcodeproj" \
  -scheme "PKM Brain" \
  -configuration Release \
  -derivedDataPath DerivedData \
  build
