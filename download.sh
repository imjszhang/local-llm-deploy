#!/bin/bash
# 通用模型下载脚本
# 从 models.json 读取 repo_id 和量化版本，使用 huggingface_hub 下载
# 用法: ./download.sh <模型名> [--quant X]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODELS_JSON="$SCRIPT_DIR/models.json"
MODELS_DIR="$SCRIPT_DIR/models"

# 解析参数
MODEL_NAME="$1"
shift || true
QUANT=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --quant)
            QUANT="$2"
            shift 2
            ;;
        *)
            echo "未知参数: $1"
            exit 1
            ;;
    esac
done

if [ -z "$MODEL_NAME" ]; then
    echo "用法: $0 <模型名> [--quant X]"
    echo ""
    echo "可用模型:"
    python3 -c "
import json
with open('$MODELS_JSON') as f:
    data = json.load(f)
for name, cfg in data.items():
    quants = ', '.join(cfg.get('quants', {}).keys())
    default = cfg.get('default_quant', '')
    print(f'  {name:15s} 量化: {quants}  (默认: {default})')
"
    exit 1
fi

# 从 models.json 读取配置
read -r REPO_ID DEFAULT_QUANT PATTERN REPO_NAME <<< "$(python3 -c "
import json, sys
with open('$MODELS_JSON') as f:
    data = json.load(f)
model = data.get('$MODEL_NAME')
if not model:
    print('ERROR', file=sys.stderr)
    sys.exit(1)
quant = '${QUANT}' or model['default_quant']
q = model.get('quants', {}).get(quant)
if not q:
    print(f'可用量化版本: {list(model[\"quants\"].keys())}', file=sys.stderr)
    sys.exit(1)
repo_name = model.get('repo_name') or model['repo_id'].replace('/', '-')
print(model['repo_id'], quant, q['pattern'], repo_name)
" 2>&1)" || {
    echo -e "\033[0;31m错误: 未知模型 '$MODEL_NAME' 或量化版本 '${QUANT}'\033[0m"
    exit 1
}

if [ "$REPO_ID" = "ERROR" ]; then
    echo -e "\033[0;31m错误: 模型 '$MODEL_NAME' 未在 models.json 中注册\033[0m"
    exit 1
fi

QUANT="${QUANT:-$DEFAULT_QUANT}"
REPO_NAME="${REPO_NAME:-$(echo "$REPO_ID" | tr '/' '-')}"
TARGET_DIR="$MODELS_DIR/$REPO_NAME/$QUANT"

# 使用项目 .venv 中的 Python（若存在）
if [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
else
    PYTHON_BIN="${PYTHON_BIN:-$(which python3 2>/dev/null || which python 2>/dev/null)}"
fi

echo "========================================"
echo "模型下载: $MODEL_NAME"
echo "========================================"
echo "HF 仓库:   $REPO_ID"
echo "量化版本:  $QUANT"
echo "匹配模式:  $PATTERN"
echo "目标目录:  $TARGET_DIR"
echo "Python:    $PYTHON_BIN"
echo "========================================"

# 确保 huggingface_hub 已安装
"$PYTHON_BIN" -c "import huggingface_hub" 2>/dev/null || {
    echo "正在安装 huggingface_hub 和 hf_transfer..."
    "$PYTHON_BIN" -m pip install -U huggingface_hub hf_transfer
}

mkdir -p "$TARGET_DIR"

echo "开始下载 $REPO_ID (包含 $PATTERN)..."
"$PYTHON_BIN" -c "
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='$REPO_ID',
    local_dir='$TARGET_DIR',
    allow_patterns=['$PATTERN'],
)
"

echo ""
echo "========================================"
echo "下载完成！文件列表："
echo "========================================"
find "$TARGET_DIR" -name "*.gguf" -exec ls -lh {} \;
echo ""
echo "部署示例:"
echo "  ./manage.sh start $MODEL_NAME"
