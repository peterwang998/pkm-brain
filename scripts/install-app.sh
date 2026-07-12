#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$ROOT/dist/PKM Brain.app"
INSTALL_DIR="${PKM_BRAIN_INSTALL_DIR:-/Applications}"
TARGET="$INSTALL_DIR/PKM Brain.app"
BUILD=false
ACTIVATE=false

for argument in "$@"; do
  case "$argument" in
    --build)
      BUILD=true
      ;;
    --activate)
      ACTIVATE=true
      ;;
    *)
      echo "unsupported argument: $argument" >&2
      exit 2
      ;;
  esac
done

if [[ "$BUILD" == true ]]; then
  "$ROOT/scripts/build-app.sh"
fi

if [[ ! -d "$SOURCE" ]]; then
  echo "app bundle not found at $SOURCE; run scripts/build-app.sh first" >&2
  exit 1
fi

codesign --verify --deep --strict "$SOURCE"
mkdir -p "$INSTALL_DIR"

STAGING="$INSTALL_DIR/.PKM Brain.app.installing.$$"
PREVIOUS="$INSTALL_DIR/.PKM Brain.app.previous"
rm -rf "$STAGING"
ditto "$SOURCE" "$STAGING"
codesign --verify --deep --strict "$STAGING"

if [[ "$ACTIVATE" == true ]]; then
  osascript -e 'tell application id "com.pkm-brain.app" to quit' >/dev/null 2>&1 || true
  sleep 1
fi

rm -rf "$PREVIOUS"
if [[ -d "$TARGET" ]]; then
  mv "$TARGET" "$PREVIOUS"
fi

if ! mv "$STAGING" "$TARGET"; then
  if [[ -d "$PREVIOUS" ]]; then
    mv "$PREVIOUS" "$TARGET"
  fi
  exit 1
fi

codesign --verify --deep --strict "$TARGET"

if [[ "$ACTIVATE" == true ]]; then
  "$TARGET/Contents/MacOS/PKM Brain" --disable-login-item >/dev/null 2>&1 || true
  "$TARGET/Contents/MacOS/PKM Brain" --enable-login-item
  open -n "$TARGET"
fi

printf 'Installed %s\n' "$TARGET"
