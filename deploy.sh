#!/bin/bash
# GLM-5 一键部署脚本
# 启动 llama-server 提供 OpenAI 兼容 API

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CPP_DIR="${CPP_DIR:-}"
MODEL_DIR="${MODEL_DIR:-}"
PORT="${PORT:-8001}"
LOG_FILE="${LOG_FILE:-/tmp/glm5_server.log}"
API_KEY="${API_KEY:-}"
API_KEY_FILE="${API_KEY_FILE:-}"
# 默认开启局域网；若存在 .api-key 则默认使用
HOST="${HOST:-0.0.0.0}"
DEFAULT_API_KEY_FILE="$SCRIPT_DIR/.api-key"
[ -z "$API_KEY" ] && [ -z "$API_KEY_FILE" ] && [ -f "$DEFAULT_API_KEY_FILE" ] && API_KEY_FILE="$DEFAULT_API_KEY_FILE"

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --cpp-dir)
            CPP_DIR="$2"
            shift 2
            ;;
        --model-dir)
            MODEL_DIR="$2"
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
            echo "必须参数:"
            echo "  --cpp-dir PATH     llama.cpp 编译后的根目录 (或设置 CPP_DIR)"
            echo "  --model-dir PATH  GGUF 模型目录，含分片文件 (或设置 MODEL_DIR)"
            echo ""
            echo "可选参数:"
            echo "  --port PORT        服务端口 (默认: 8001)"
            echo "  --api-key KEY      API Key 认证，客户端需在 Authorization: Bearer <KEY> 中携带"
            echo "  --api-key-file F   密钥文件路径，每行一个 key，支持多 key"
            echo "  --host HOST        监听地址 (默认: 0.0.0.0 局域网)"
            echo "  --lan              等同于 --host 0.0.0.0，允许局域网访问"
            echo "  --no-lan           仅本机访问，等同于 --host 127.0.0.1"
            echo ""
            echo "默认行为: 局域网访问、启用 /metrics、若存在 .api-key 则启用认证"
            echo ""
            echo "示例:"
            echo "  $0 --cpp-dir $SCRIPT_DIR/llama.cpp --model-dir $SCRIPT_DIR/models/GLM-5-GGUF/UD-IQ2_XXS"
            echo "  $0 --cpp-dir ... --model-dir ... --no-lan   # 仅本机"
            exit 0
            ;;
        *)
            echo -e "${RED}未知参数: $1${NC}"
            echo "使用 --help 查看帮助"
            exit 1
            ;;
    esac
done

# 路径验证
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
        echo -e "${RED}错误: 必须指定 GGUF 模型目录${NC}"
        echo "   使用 --model-dir 或设置 MODEL_DIR"
        has_error=true
    elif [ ! -d "$MODEL_DIR" ]; then
        echo -e "${RED}错误: MODEL_DIR 不存在: $MODEL_DIR${NC}"
        has_error=true
    else
        GGUF_COUNT=$(find "$MODEL_DIR" -maxdepth 1 -name "*.gguf" 2>/dev/null | wc -l)
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
echo -e "${BLUE}   GLM-5 一键部署脚本${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "${GREEN}端口: $PORT${NC}"
echo -e "${GREEN}监听: $HOST${NC}"
echo -e "${GREEN}CPP_DIR: $CPP_DIR${NC}"
echo -e "${GREEN}MODEL_DIR: $MODEL_DIR${NC}"
echo -e "${GREEN}监控: /metrics 已启用${NC}"
if [ -n "$API_KEY" ] || [ -n "$API_KEY_FILE" ]; then
    echo -e "${GREEN}认证: 已启用 ($([ -n "$API_KEY" ] && echo "API Key" || echo "$API_KEY_FILE"))${NC}"
else
    echo -e "${YELLOW}认证: 未启用${NC}"
fi
echo ""

# 确定第一个分片 GGUF（必须按 00001-of-00006 顺序加载）
MODEL_FILE=$(find "$MODEL_DIR" -maxdepth 1 -name "*.gguf" 2>/dev/null | sort | head -1)
if [ -z "$MODEL_FILE" ]; then
    echo -e "${RED}错误: 未找到 GGUF 文件${NC}"
    exit 1
fi

# 停止已有服务（避免端口冲突）
echo -e "${YELLOW}[1/3] 检查已有服务...${NC}"
pkill -f "llama-server" 2>/dev/null || true
sleep 3

# 启动 llama-server
echo -e "${YELLOW}[2/3] 启动 llama-server...${NC}"
LLAMA_ARGS=(
    --model "$MODEL_FILE"
    --alias "unsloth/GLM-5"
    --fit on
    --temp 1.0
    --top-p 0.95
    --ctx-size 16384
    --host "$HOST"
    --port "$PORT"
    --jinja
    --metrics
)
if [ -n "$API_KEY" ]; then
    LLAMA_ARGS+=(--api-key "$API_KEY")
elif [ -n "$API_KEY_FILE" ]; then
    LLAMA_ARGS+=(--api-key-file "$API_KEY_FILE")
fi
nohup "$CPP_DIR/build/bin/llama-server" "${LLAMA_ARGS[@]}" > "$LOG_FILE" 2>&1 &

echo "   等待服务启动（模型加载需数分钟）..."
sleep 30

# 健康检查
echo -e "${YELLOW}[3/3] 健康检查...${NC}"
MAX_RETRIES=12
RETRY=0
CURL_OPTS=(-s "http://localhost:$PORT/health")
if [ -n "$HEALTH_API_KEY" ]; then
    CURL_OPTS=(-s -H "Authorization: Bearer $HEALTH_API_KEY" "http://localhost:$PORT/health")
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
echo -e "${GREEN}部署完成${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
if [ "$HOST" = "0.0.0.0" ]; then
    LAN_IP=$(ifconfig 2>/dev/null | grep "inet " | grep -v 127.0.0.1 | awk '{print $2}' | head -1)
    echo -e "${GREEN}本机: http://localhost:$PORT${NC}"
    [ -n "$LAN_IP" ] && echo -e "${GREEN}局域网: http://${LAN_IP}:$PORT${NC}"
else
    echo -e "${GREEN}服务地址: http://${HOST}:$PORT${NC}"
fi
echo -e "${GREEN}聊天: http://localhost:$PORT/${NC}"
echo ""
echo -e "${YELLOW}前端（含监控）需单独启动，因 API Key 认证会拦截静态文件:${NC}"
echo "   ./serve-ui.sh"
echo "   然后访问: http://localhost:8888/ (聊天) 或 http://localhost:8888/monitor.html (监控)"
echo -e "${GREEN}OpenAI API: http://localhost:$PORT/v1${NC}"
if [ -n "$HEALTH_API_KEY" ]; then
    echo ""
    echo -e "${YELLOW}认证已启用，请求时需携带:${NC}"
    echo "   Authorization: Bearer <你的API-Key>"
    echo ""
    echo -e "${YELLOW}示例 (curl):${NC}"
    echo "   curl -H 'Authorization: Bearer <key>' http://localhost:$PORT/v1/chat/completions ..."
    echo ""
    echo -e "${YELLOW}示例 (OpenAI Python):${NC}"
    echo "   client = OpenAI(base_url='http://localhost:$PORT/v1', api_key='<你的API-Key>')"
fi
echo ""
echo -e "${GREEN}常用命令:${NC}"
echo "   查看日志: tail -f $LOG_FILE"
echo "   停止服务: pkill -f llama-server"
echo ""
echo -e "${GREEN}监控接口 (需认证时加 -H 'Authorization: Bearer <key>'):${NC}"
if [ -n "$HEALTH_API_KEY" ]; then
    echo "   健康检查: curl -s -H 'Authorization: Bearer <key>' http://localhost:$PORT/health"
    echo "   推理指标: curl -s -H 'Authorization: Bearer <key>' http://localhost:$PORT/metrics"
    echo "   槽位状态: curl -s -H 'Authorization: Bearer <key>' http://localhost:$PORT/slots"
else
    echo "   健康检查: curl -s http://localhost:$PORT/health"
    echo "   推理指标: curl -s http://localhost:$PORT/metrics"
    echo "   槽位状态: curl -s http://localhost:$PORT/slots"
fi
echo ""
echo -e "${YELLOW}硬件监控 (Apple Silicon):${NC}"
echo "   asitop:  pip install asitop && sudo asitop   # GPU/CPU/内存/功耗"
echo "   macmon:  brew install macmon && macmon       # 无需 sudo"
echo ""
