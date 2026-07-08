#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT"
uv run pytest -q tests/test_mcp_proxy.py tests/test_app_migration.py tests/test_m3_acceptance.py
swift test --package-path app
