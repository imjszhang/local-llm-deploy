#!/usr/bin/env bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="python3"
exec "$PYTHON_BIN" "$SCRIPT_DIR/llm.py" engine init "$@"
