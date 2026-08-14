#!/usr/bin/env bash
# 启动 serve_whisper.py（供 launchd / whisper.sh 调用）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR" || exit 1

WHISPER_MODEL_NAME="${WHISPER_MODEL_NAME:-whisper-large-v3}"
WHISPER_HOST="${WHISPER_HOST:-127.0.0.1}"
WHISPER_PORT="${WHISPER_PORT:-8007}"

if [[ -x "$SCRIPT_DIR/.venv-whisper/bin/python" ]]; then
  PYTHON_BIN="$SCRIPT_DIR/.venv-whisper/bin/python"
elif [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "找不到 Python（请创建 .venv-whisper）" >&2
  exit 1
fi

if [[ -x "$SCRIPT_DIR/tools/ffmpeg" ]]; then
  export PATH="$SCRIPT_DIR/tools:$PATH"
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/serve_whisper.py" \
  --model-name "$WHISPER_MODEL_NAME" \
  --host "$WHISPER_HOST" \
  --port "$WHISPER_PORT" \
  "$@"
