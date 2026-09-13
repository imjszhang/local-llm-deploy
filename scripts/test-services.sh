#!/usr/bin/env bash
# Explicit live smoke tests. Unit/contract tests do not invoke this script.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec "${PYTHON_BIN:-python3}" "$SCRIPT_DIR/tests/smoke/services.py" "$@"
