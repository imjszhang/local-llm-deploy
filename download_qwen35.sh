#!/bin/bash
# Qwen3.5-397B-A17B GGUF 模型下载脚本
# 4-bit MXFP4_MOE 版本，约 214GB，适合 256GB Mac
# 使用 huggingface_hub 下载，支持断点续传

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODEL_DIR="${MODEL_DIR:-$SCRIPT_DIR/models/Qwen3.5-397B-A17B-GGUF}"

# 量化版本：MXFP4_MOE (4-bit, ~214GB) 或 UD-Q2_K_XL (2-bit) 等
QUANT="${QUANT:-MXFP4_MOE}"

# 使用项目 .venv 中的 Python（若存在）
if [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
else
    PYTHON_BIN="${PYTHON_BIN:-$(which python3 2>/dev/null || which python 2>/dev/null)}"
fi

echo "========================================"
echo "Qwen3.5-397B-A17B GGUF 模型下载"
echo "========================================"
echo "目标目录: $MODEL_DIR"
echo "量化版本: $QUANT (~214GB)"
echo "Python:   $PYTHON_BIN"
echo "========================================"

# 确保 huggingface_hub 已安装
"$PYTHON_BIN" -c "import huggingface_hub" 2>/dev/null || {
    echo "正在安装 huggingface_hub 和 hf_transfer..."
    "$PYTHON_BIN" -m pip install -U huggingface_hub hf_transfer
}

# 创建目录
mkdir -p "$MODEL_DIR"

# 使用 huggingface_hub 下载（支持断点续传）
echo "开始下载 unsloth/Qwen3.5-397B-A17B-GGUF (包含 *$QUANT*)..."
"$PYTHON_BIN" -c "
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='unsloth/Qwen3.5-397B-A17B-GGUF',
    local_dir='$MODEL_DIR',
    allow_patterns=['*${QUANT}*'],
)
"

echo ""
echo "========================================"
echo "下载完成！文件列表："
echo "========================================"
find "$MODEL_DIR" -name "*.gguf" -exec ls -lh {} \;
echo ""
echo "部署示例："
echo "  ./deploy.sh --cpp-dir \"\$(pwd)/llama.cpp\" --model-dir \"\$(pwd)/models/Qwen3.5-397B-A17B-GGUF/MXFP4_MOE\""
