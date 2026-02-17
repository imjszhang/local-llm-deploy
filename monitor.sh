#!/bin/bash
# 监控脚本：使用 .api-key 调用 health/metrics/slots 等接口
# 用法: ./monitor.sh [metrics|health|slots] [--model <name>]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_KEY_FILE="$SCRIPT_DIR/.api-key"
RUN_DIR="$SCRIPT_DIR/run"
PORT="${PORT:-}"
MODEL=""

# 解析参数
CMD="${1:-metrics}"
shift || true
while [[ $# -gt 0 ]]; do
    case $1 in
        --model)
            MODEL="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

# 如果指定了模型名，从 PID 文件读取端口
if [ -n "$MODEL" ] && [ -z "$PORT" ]; then
    PID_FILE="$RUN_DIR/$MODEL.pid"
    if [ -f "$PID_FILE" ]; then
        PORT=$(sed -n '2p' "$PID_FILE" 2>/dev/null | tr -d '\r\n')
    else
        echo "错误: 模型 '$MODEL' 未在运行中"
        exit 1
    fi
fi

PORT="${PORT:-8001}"
BASE_URL="http://localhost:$PORT"

# 读取 API Key
get_api_key() {
    if [ -f "$API_KEY_FILE" ]; then
        head -1 "$API_KEY_FILE" 2>/dev/null | tr -d '\r\n'
    else
        echo ""
    fi
}

API_KEY=$(get_api_key)
CURL_AUTH=()
[ -n "$API_KEY" ] && CURL_AUTH=(-H "Authorization: Bearer $API_KEY")

case "$CMD" in
    metrics)
        curl -s "${CURL_AUTH[@]}" "$BASE_URL/metrics"
        ;;
    health)
        curl -s "${CURL_AUTH[@]}" "$BASE_URL/health"
        ;;
    slots)
        curl -s "${CURL_AUTH[@]}" "$BASE_URL/slots"
        ;;
    *)
        echo "用法: $0 [metrics|health|slots] [--model <name>] [--port <port>]"
        echo "  默认: metrics"
        echo ""
        echo "示例:"
        echo "  $0 health"
        echo "  $0 metrics --model glm-5"
        echo "  $0 slots --model qwen3.5"
        exit 1
        ;;
esac
