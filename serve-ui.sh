#!/bin/bash
# 启动前端静态服务（聊天 + 监控）
# 因 API Key 认证会拦截 llama-server 的静态文件，需单独启动

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${UI_PORT:-8888}"

echo "前端服务: http://localhost:$PORT/"
echo "  聊天: http://localhost:$PORT/"
echo "  监控: http://localhost:$PORT/monitor.html"
echo ""
echo "API 默认: http://127.0.0.1:8001 （可在页面中修改）"
echo ""

cd "$SCRIPT_DIR"
python3 -m http.server "$PORT" -d static
