#!/bin/bash
# AI 探员总部监控系统 - 启动脚本

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

echo "🚀 启动 AI 探员总部监控系统"
echo ""

# 检查 Python 环境
if ! command -v python3 &> /dev/null; then
    echo "❌ 未找到 python3"
    exit 1
fi

# 检查前端构建
if [ ! -f "static/agents.html" ]; then
    echo "⚠️  前端未构建，正在构建..."
    cd frontend
    npm run build:agents
    cd ..
fi

# 检查是否已有 gateway 进程
if lsof -ti:8888 > /dev/null 2>&1; then
    echo "⚠️  端口 8888 已被占用"
    echo "   现有进程："
    ps aux | grep "[p]ython.*gateway" | awk '{print "   PID:", $2, $9}'
    echo ""
    read -p "是否杀掉现有进程并重启？(y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "🔄 停止现有 gateway..."
        lsof -ti:8888 | xargs kill -9 2>/dev/null || true
        sleep 1
    else
        echo "ℹ️  使用现有 gateway 进程"
        echo ""
        echo "🌐 访问地址："
        echo "   http://127.0.0.1:8888/agents.html"
        echo ""
        exit 0
    fi
fi

# 启动 gateway
echo "🔧 启动 Gateway..."
export PYTHONPATH="$PROJECT_ROOT/src"

nohup python3 -m local_llm_deploy.gateway.app \
    --host 127.0.0.1 \
    --port 8888 \
    > /tmp/gateway-agents.log 2>&1 &

GATEWAY_PID=$!
echo "   Gateway PID: $GATEWAY_PID"

# 等待启动
echo "⏳ 等待 Gateway 启动..."
for i in {1..10}; do
    if lsof -ti:8888 > /dev/null 2>&1; then
        echo "✅ Gateway 已启动"
        break
    fi
    sleep 1
    if [ $i -eq 10 ]; then
        echo "❌ Gateway 启动超时"
        echo "   查看日志: tail -f /tmp/gateway-agents.log"
        exit 1
    fi
done

# 验证 API 端点
echo ""
echo "🔍 验证 API 端点..."
if curl -s -f -o /dev/null http://127.0.0.1:8888/agents.html; then
    echo "   ✓ /agents.html"
else
    echo "   ✗ /agents.html"
fi

if curl -s -f -o /dev/null http://127.0.0.1:8888/agent-assets/agents-*.js 2>/dev/null; then
    echo "   ✓ /agent-assets/*.js"
else
    echo "   ✗ /agent-assets/*.js"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✨ AI 探员总部监控系统已启动"
echo ""
echo "🌐 访问地址："
echo "   http://127.0.0.1:8888/agents.html"
echo ""
echo "📊 API 端点："
echo "   http://127.0.0.1:8888/agents-api/v1/snapshot"
echo "   ws://127.0.0.1:8888/agents-api/v1/stream"
echo ""
echo "📝 日志："
echo "   tail -f /tmp/gateway-agents.log"
echo ""
echo "🛑 停止："
echo "   kill $GATEWAY_PID"
echo "   或者: lsof -ti:8888 | xargs kill"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 尝试打开浏览器
if command -v open &> /dev/null; then
    echo "🌐 正在打开浏览器..."
    sleep 1
    open "http://127.0.0.1:8888/agents.html"
elif command -v xdg-open &> /dev/null; then
    xdg-open "http://127.0.0.1:8888/agents.html"
else
    echo "💡 请手动在浏览器中打开上述地址"
fi
