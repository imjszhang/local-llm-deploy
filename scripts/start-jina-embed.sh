#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODEL_NAME="${JINA_MODEL_NAME:-jina-embed}"
HOST="${JINA_HOST:-127.0.0.1}"
PORT="${JINA_PORT:-8004}"

if [[ -x "$SCRIPT_DIR/.venv-embed/bin/python" ]]; then
  PYTHON_BIN="$SCRIPT_DIR/.venv-embed/bin/python"
elif [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

cd "$SCRIPT_DIR"
exec "$PYTHON_BIN" "$SCRIPT_DIR/serve_embedding.py" \
  --model-name "$MODEL_NAME" \
  --host "$HOST" \
  --port "$PORT" \
  "$@"
