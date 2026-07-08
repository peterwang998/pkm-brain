#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK_ROOT="${1:-$(mktemp -d "${TMPDIR:-/tmp}/pkm-brain-m2-acceptance.XXXXXX")}"
APP_BUNDLE="$ROOT/app/DerivedData/Build/Products/Release/PKM Brain.app"

"$ROOT/scripts/build-app.sh" >/dev/null

swift run --package-path "$ROOT/app" PKMBrainAcceptance \
  --app-bundle "$APP_BUNDLE" \
  --work-root "$WORK_ROOT"
