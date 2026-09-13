#!/usr/bin/env bash
# Compatibility entry: all command behavior lives in local_llm_deploy.cli.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${LOCAL_LLM_PYTHON:-$SCRIPT_DIR/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3)"
exec "$PYTHON_BIN" "$SCRIPT_DIR/llm.py" --project-root "$SCRIPT_DIR" deploy "$@"
