#!/bin/bash
# Local LLM Deploy — 通用模型部署脚本
# 启动 llama-server 提供 OpenAI 兼容 API
# 支持通过 --model-name 从 models.json 读取配置，也支持手动指定参数

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODELS_JSON="$SCRIPT_DIR/models.json"
RUN_DIR="$SCRIPT_DIR/run"
LOGS_DIR="$SCRIPT_DIR/logs"
MODELS_DIR="$SCRIPT_DIR/models"

mkdir -p "$RUN_DIR" "$LOGS_DIR"

# 默认值
CPP_DIR="${CPP_DIR:-$SCRIPT_DIR/llama.cpp}"
MODEL_DIR="${MODEL_DIR:-}"
MODEL_NAME="${MODEL_NAME:-}"
QUANT=""
PORT="${PORT:-}"
API_KEY="${API_KEY:-}"
API_KEY_FILE="${API_KEY_FILE:-}"
HOST="${HOST:-0.0.0.0}"
ALIAS=""
TEMP=""
TOP_P=""
CTX_SIZE=""
N_PREDICT=""
REPEAT_PENALTY=""

DEFAULT_API_KEY_FILE="$SCRIPT_DIR/.api-key"
[ -z "$API_KEY" ] && [ -z "$API_KEY_FILE" ] && [ -f "$DEFAULT_API_KEY_FILE" ] && API_KEY_FILE="$DEFAULT_API_KEY_FILE"

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --model-name)
            MODEL_NAME="$2"
            shift 2
            ;;
        --cpp-dir)
            CPP_DIR="$2"
            shift 2
            ;;
        --model-dir)
            MODEL_DIR="$2"
            shift 2
            ;;
        --quant)
            QUANT="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --api-key)
            API_KEY="$2"
            shift 2
            ;;
        --api-key-file)
            API_KEY_FILE="$2"
            shift 2
            ;;
        --host)
            HOST="$2"
            shift 2
            ;;
        --lan)
            HOST="0.0.0.0"
            shift
            ;;
        --no-lan)
            HOST="127.0.0.1"
            shift
            ;;
        --help)
            echo "用法: $0 [选项]"
            echo ""
            echo "模型选择（二选一）:"
            echo "  --model-name NAME  从 models.json 读取配置（推荐）"
            echo "  --model-dir PATH   手动指定 GGUF 模型目录"
            echo ""
            echo "可选参数:"
            echo "  --cpp-dir PATH     llama.cpp 编译目录 (默认: ./llama.cpp)"
            echo "  --quant QUANT      量化版本，配合 --model-name 使用"
            echo "  --port PORT        服务端口 (默认: 从 models.json 读取或 8001)"
            echo "  --api-key KEY      API Key 认证"
            echo "  --api-key-file F   密钥文件路径"
            echo "  --host HOST        监听地址 (默认: 0.0.0.0)"
            echo "  --lan              允许局域网访问"
            echo "  --no-lan           仅本机访问"
            echo ""
            echo "示例:"
            echo "  $0 --model-name glm-5"
            echo "  $0 --model-name qwen3.5 --quant UD-Q2_K_XL --port 8003"
            echo "  $0 --model-dir ./models/custom-model/  # 高级用法"
            exit 0
            ;;
        *)
            echo -e "${RED}未知参数: $1${NC}"
            echo "使用 --help 查看帮助"
            exit 1
            ;;
    esac
done

# ── 从 models.json 读取配置 ──

EXTRA_ARGS=()

if [ -n "$MODEL_NAME" ] && [ -f "$MODELS_JSON" ]; then
    MODEL_CONFIG=$(python3 -c "
import json, sys
with open('$MODELS_JSON') as f:
    data = json.load(f)
model = data.get('$MODEL_NAME')
if not model:
    print('NOT_FOUND', file=sys.stderr)
    sys.exit(1)
quant = '${QUANT}' or model['default_quant']
qinfo = model.get('quants', {}).get(quant, {})
params = model.get('params', {})
extra = ' '.join(params.get('extra_args', []))
repo_name = model.get('repo_name') or model['repo_id'].replace('/', '-')
print(f\"{model['repo_id']}|{model.get('alias', '')}|{model.get('default_port', 8001)}|{quant}|{params.get('temp', '')}|{params.get('top_p', '')}|{params.get('ctx_size', '')}|{params.get('n_predict', '')}|{params.get('repeat_penalty', '')}|{extra}|{repo_name}\")
" 2>&1) || {
        echo -e "${RED}错误: 模型 '$MODEL_NAME' 未在 models.json 中找到${NC}"
        exit 1
    }

    IFS='|' read -r CFG_REPO_ID CFG_ALIAS CFG_PORT CFG_QUANT CFG_TEMP CFG_TOP_P CFG_CTX CFG_NPREDICT CFG_REPEAT CFG_EXTRA CFG_REPO_NAME <<< "$MODEL_CONFIG"

    ALIAS="${ALIAS:-$CFG_ALIAS}"
    PORT="${PORT:-$CFG_PORT}"
    QUANT="${QUANT:-$CFG_QUANT}"
    TEMP="${TEMP:-$CFG_TEMP}"
    TOP_P="${TOP_P:-$CFG_TOP_P}"
    CTX_SIZE="${CTX_SIZE:-$CFG_CTX}"
    N_PREDICT="${N_PREDICT:-$CFG_NPREDICT}"
    REPEAT_PENALTY="${REPEAT_PENALTY:-$CFG_REPEAT}"

    if [ -n "$CFG_EXTRA" ]; then
        read -ra EXTRA_ARGS <<< "$CFG_EXTRA"
    fi

    if [ -z "$MODEL_DIR" ]; then
        MODEL_DIR="$MODELS_DIR/$CFG_REPO_NAME/$QUANT"
    fi
fi

# 为手动指定 model-dir 的用户提供默认值
PORT="${PORT:-8001}"
ALIAS="${ALIAS:-custom-model}"
TEMP="${TEMP:-1.0}"
TOP_P="${TOP_P:-0.95}"
CTX_SIZE="${CTX_SIZE:-16384}"
N_PREDICT="${N_PREDICT:-32768}"
REPEAT_PENALTY="${REPEAT_PENALTY:-1.0}"

DISPLAY_NAME="${MODEL_NAME:-$ALIAS}"
LOG_FILE="$LOGS_DIR/${MODEL_NAME:-custom}.log"

# ── 路径验证 ──

check_required_paths() {
    local has_error=false

    if [ -z "$CPP_DIR" ]; then
        echo -e "${RED}错误: 必须指定 llama.cpp 编译目录${NC}"
        echo "   使用 --cpp-dir 或设置 CPP_DIR"
        has_error=true
    elif [ ! -d "$CPP_DIR" ]; then
        echo -e "${RED}错误: CPP_DIR 不存在: $CPP_DIR${NC}"
        has_error=true
    elif [ ! -f "$CPP_DIR/build/bin/llama-server" ]; then
        echo -e "${RED}错误: llama-server 未找到: $CPP_DIR/build/bin/llama-server${NC}"
        echo "   请先编译: cd $CPP_DIR && cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build --target llama-server -j"
        has_error=true
    fi

    if [ -z "$MODEL_DIR" ]; then
        echo -e "${RED}错误: 必须指定模型（--model-name 或 --model-dir）${NC}"
        has_error=true
    elif [ ! -d "$MODEL_DIR" ]; then
        echo -e "${RED}错误: MODEL_DIR 不存在: $MODEL_DIR${NC}"
        echo "   请先下载: ./manage.sh download $MODEL_NAME"
        has_error=true
    else
        GGUF_COUNT=$(find "$MODEL_DIR" -maxdepth 1 -name "*.gguf" 2>/dev/null | wc -l | tr -d ' ')
        if [ "$GGUF_COUNT" -eq 0 ]; then
            echo -e "${RED}错误: MODEL_DIR 中无 .gguf 文件: $MODEL_DIR${NC}"
            has_error=true
        fi
    fi

    if [ -n "$API_KEY_FILE" ] && [ ! -f "$API_KEY_FILE" ]; then
        echo -e "${RED}错误: API 密钥文件不存在: $API_KEY_FILE${NC}"
        has_error=true
    fi

    if [ -n "$API_KEY" ] && [ -n "$API_KEY_FILE" ]; then
        echo -e "${RED}错误: --api-key 与 --api-key-file 不能同时使用${NC}"
        has_error=true
    fi

    if [ "$has_error" = true ]; then
        exit 1
    fi
}

check_required_paths

# 用于健康检查的 API Key（任选其一）
HEALTH_API_KEY=""
if [ -n "$API_KEY" ]; then
    HEALTH_API_KEY="$API_KEY"
elif [ -n "$API_KEY_FILE" ]; then
    HEALTH_API_KEY=$(head -1 "$API_KEY_FILE" 2>/dev/null | tr -d '\r\n' || true)
fi

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}   部署: $DISPLAY_NAME${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "${GREEN}模型:    $DISPLAY_NAME${NC}"
echo -e "${GREEN}端口:    $PORT${NC}"
echo -e "${GREEN}监听:    $HOST${NC}"
echo -e "${GREEN}CPP_DIR: $CPP_DIR${NC}"
echo -e "${GREEN}MODEL:   $MODEL_DIR${NC}"
echo -e "${GREEN}参数:    temp=$TEMP top_p=$TOP_P ctx=$CTX_SIZE n_predict=$N_PREDICT${NC}"
echo -e "${GREEN}日志:    $LOG_FILE${NC}"
echo -e "${GREEN}监控:    /metrics 已启用${NC}"
if [ -n "$API_KEY" ] || [ -n "$API_KEY_FILE" ]; then
    echo -e "${GREEN}认证:    已启用 ($([ -n "$API_KEY" ] && echo "API Key" || echo "$API_KEY_FILE"))${NC}"
else
    echo -e "${YELLOW}认证:    未启用${NC}"
fi
echo ""

# 确定第一个分片 GGUF
MODEL_FILE=$(find "$MODEL_DIR" -maxdepth 1 -name "*.gguf" 2>/dev/null | sort | head -1)
if [ -z "$MODEL_FILE" ]; then
    echo -e "${RED}错误: 未找到 GGUF 文件${NC}"
    exit 1
fi

# 检查端口是否被占用
echo -e "${YELLOW}[1/3] 检查端口...${NC}"
if lsof -i :"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    EXISTING_PID=$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)
    echo -e "${YELLOW}   端口 $PORT 已被占用 (PID $EXISTING_PID)，正在停止...${NC}"
    kill "$EXISTING_PID" 2>/dev/null || true
    sleep 3
fi

# 启动 llama-server
echo -e "${YELLOW}[2/3] 启动 llama-server...${NC}"
LLAMA_ARGS=(
    --model "$MODEL_FILE"
    --alias "$ALIAS"
    --fit on
    --temp "$TEMP"
    --top-p "$TOP_P"
    --ctx-size "$CTX_SIZE"
    --n-predict "$N_PREDICT"
    --repeat-penalty "$REPEAT_PENALTY"
    --host "$HOST"
    --port "$PORT"
    --metrics
    --slots
    --threads-http 64
)

# 追加 extra_args（如 --jinja、--reasoning-budget 等）
if [ ${#EXTRA_ARGS[@]} -gt 0 ]; then
    LLAMA_ARGS+=("${EXTRA_ARGS[@]}")
fi

if [ -n "$API_KEY" ]; then
    LLAMA_ARGS+=(--api-key "$API_KEY")
elif [ -n "$API_KEY_FILE" ]; then
    LLAMA_ARGS+=(--api-key-file "$API_KEY_FILE")
fi

LLAMA_SERVER_SLOTS_DEBUG=1
export LLAMA_SERVER_SLOTS_DEBUG
# macOS: 确保动态库路径正确（项目迁移后 @rpath 可能指向旧路径）
LLAMA_BIN_DIR="$CPP_DIR/build/bin"
export DYLD_LIBRARY_PATH="${DYLD_LIBRARY_PATH:+$DYLD_LIBRARY_PATH:}$LLAMA_BIN_DIR"

nohup env LLAMA_SERVER_SLOTS_DEBUG=1 DYLD_LIBRARY_PATH="$LLAMA_BIN_DIR${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}" "$CPP_DIR/build/bin/llama-server" "${LLAMA_ARGS[@]}" > "$LOG_FILE" 2>&1 &
SERVER_PID=$!

# 写入 PID 文件（第一行 PID，第二行端口，第三行 alias/模型名）
PID_FILE_NAME="${MODEL_NAME:-custom}"
echo "$SERVER_PID" > "$RUN_DIR/$PID_FILE_NAME.pid"
echo "$PORT" >> "$RUN_DIR/$PID_FILE_NAME.pid"
echo "${ALIAS:-$MODEL_NAME}" >> "$RUN_DIR/$PID_FILE_NAME.pid"

echo "   PID: $SERVER_PID"
echo "   等待服务启动（模型加载需数分钟）..."
sleep 30

# 健康检查
echo -e "${YELLOW}[3/3] 健康检查...${NC}"
MAX_RETRIES=12
RETRY=0
CURL_OPTS=(-s --connect-timeout 5 --max-time 10 "http://localhost:$PORT/health")
if [ -n "$HEALTH_API_KEY" ]; then
    CURL_OPTS=(-s --connect-timeout 5 --max-time 10 -H "Authorization: Bearer $HEALTH_API_KEY" "http://localhost:$PORT/health")
fi
while [ $RETRY -lt $MAX_RETRIES ]; do
    HEALTH=$(curl "${CURL_OPTS[@]}" 2>/dev/null || echo "")
    if [ -n "$HEALTH" ]; then
        echo -e "${GREEN}服务已就绪${NC}"
        echo "   $HEALTH"
        break
    fi
    RETRY=$((RETRY + 1))
    echo "   等待中... ($RETRY/$MAX_RETRIES)"
    sleep 10
done

if [ $RETRY -eq $MAX_RETRIES ]; then
    echo -e "${RED}健康检查超时${NC}"
    echo "   查看日志: tail -f $LOG_FILE"
    tail -30 "$LOG_FILE" 2>/dev/null || true
    exit 1
fi

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}部署完成: $DISPLAY_NAME${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
if [ "$HOST" = "0.0.0.0" ]; then
    LAN_IP=$(ifconfig 2>/dev/null | grep "inet " | grep -v 127.0.0.1 | awk '{print $2}' | head -1)
    echo -e "${GREEN}本机:     http://localhost:$PORT${NC}"
    [ -n "$LAN_IP" ] && echo -e "${GREEN}局域网:   http://${LAN_IP}:$PORT${NC}"
else
    echo -e "${GREEN}服务地址: http://${HOST}:$PORT${NC}"
fi
echo -e "${GREEN}OpenAI:   http://localhost:$PORT/v1${NC}"
echo ""
echo -e "${YELLOW}前端（含监控）需单独启动:${NC}"
echo "   ./serve-ui.sh"
echo "   然后访问: http://localhost:8888/monitor.html"
if [ -n "$HEALTH_API_KEY" ]; then
    echo ""
    echo -e "${YELLOW}认证已启用，请求时需携带:${NC}"
    echo "   Authorization: Bearer <你的API-Key>"
fi
echo ""
echo -e "${GREEN}管理命令:${NC}"
echo "   查看状态: ./manage.sh status"
echo "   查看日志: ./manage.sh logs $PID_FILE_NAME"
echo "   停止服务: ./manage.sh stop $PID_FILE_NAME"
echo ""
echo -e "${YELLOW}硬件监控 (Apple Silicon):${NC}"
echo "   asitop:  pip install asitop && sudo asitop"
echo "   macmon:  brew install macmon && macmon"
echo ""
