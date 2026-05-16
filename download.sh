#!/bin/bash
# 模型下载入口：统一调用 download_model.py（依据 models.json）
# 用法与原先一致: ./download.sh <模型名> [--quant X] [--source modelscope|huggingface] [--to <路径>]
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/download_model.py" "$@"
fi
exec python3 "$SCRIPT_DIR/download_model.py" "$@"
