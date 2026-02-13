#!/bin/bash
# GLM-5 监控脚本：使用 .api-key 调用 health/metrics/slots 等接口

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_KEY_FILE="$SCRIPT_DIR/.api-key"
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

case "${1:-metrics}" in
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
        echo "用法: $0 [metrics|health|slots]"
        echo "  默认: metrics"
        exit 1
        ;;
esac
