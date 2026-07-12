#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE_ROOT="${1:-$(mktemp -d "${TMPDIR:-/tmp}/pkm-brain-ui-acceptance.XXXXXX")}"
RUN_ROOT="$BASE_ROOT/run-$(date -u +%Y%m%dT%H%M%SZ)-$$"
HOME_PATH="$RUN_ROOT/brain"
APP_SUPPORT="$RUN_ROOT/app-support"
RESULT_BUNDLE="$RUN_ROOT/PKMBrainUI.xcresult"
SOURCE_PACKAGES="$RUN_ROOT/SourcePackages"
PACKAGE_CACHE="$RUN_ROOT/PackageCache"
BRAIN_BIN="$ROOT/.venv/bin/brain"
CONFIG_PATH="/private/tmp/pkm-brain-ui-acceptance.json"
trap 'rm -f "$CONFIG_PATH"' EXIT

if [[ ! -x "$BRAIN_BIN" ]]; then
  echo "missing $BRAIN_BIN; run uv sync --dev first" >&2
  exit 1
fi
if ! command -v xcodegen >/dev/null 2>&1; then
  echo "xcodegen is required" >&2
  exit 127
fi

mkdir -p "$RUN_ROOT"
"$BRAIN_BIN" init --home "$HOME_PATH" >/dev/null
printf '{"home":"%s","brain":"%s","appSupport":"%s"}\n' \
  "$HOME_PATH" "$BRAIN_BIN" "$APP_SUPPORT" \
  > "$CONFIG_PATH"
xcodegen generate --spec "$ROOT/app/project.yml" --project "$ROOT/app"

xcodebuild \
  -project "$ROOT/app/PKM Brain.xcodeproj" \
  -scheme "PKM Brain" \
  -destination "platform=macOS" \
  -clonedSourcePackagesDirPath "$SOURCE_PACKAGES" \
  -packageCachePath "$PACKAGE_CACHE" \
  -disablePackageRepositoryCache \
  -resultBundlePath "$RESULT_BUNDLE" \
  -only-testing:"PKM BrainUITests" \
  PKM_BRAIN_APP_BUNDLE_ID=com.pkm-brain.app.ui-acceptance \
  test

printf 'UI acceptance results: %s\n' "$RESULT_BUNDLE"
