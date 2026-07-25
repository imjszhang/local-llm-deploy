#!/usr/bin/env bash
# 启动 fork/ds4 的 ds4-server（外部后端，默认端口 8005，支持 batched 多并发）
set -euo pipefail

DS4_ROOT="${DS4_ROOT:-/Users/jszhang/github/fork/ds4}"
DS4_BIN="${DS4_BIN:-$DS4_ROOT/ds4-server}"
DS4_MODEL="${DS4_MODEL:-$DS4_ROOT/ds4flash.gguf}"
DS4_HOST="${DS4_HOST:-127.0.0.1}"
DS4_PORT="${DS4_PORT:-8005}"
DS4_CTX="${DS4_CTX:-100000}"
DS4_BATCHED_SESSION="${DS4_BATCHED_SESSION:-4}"
DS4_KV_DIR="${DS4_KV_DIR:-/tmp/ds4-kv}"
DS4_KV_SPACE_MB="${DS4_KV_SPACE_MB:-8192}"

if [[ ! -x "$DS4_BIN" ]]; then
  echo "找不到可执行文件: $DS4_BIN" >&2
  echo "请先在 $DS4_ROOT 执行 make" >&2
  exit 1
fi
if [[ ! -e "$DS4_MODEL" ]]; then
  echo "找不到模型: $DS4_MODEL" >&2
  echo "请在 $DS4_ROOT 执行 ./download_model.sh" >&2
  exit 1
fi

exec "$DS4_BIN" \
  --chdir "$DS4_ROOT" \
  -m "$DS4_MODEL" \
  --host "$DS4_HOST" \
  --port "$DS4_PORT" \
  --ctx "$DS4_CTX" \
  --batched-session "$DS4_BATCHED_SESSION" \
  --kv-disk-dir "$DS4_KV_DIR" \
  --kv-disk-space-mb "$DS4_KV_SPACE_MB" \
  "$@"
