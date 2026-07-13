#!/bin/bash
# 启动前端静态服务（监控面板 + 多模型 API 代理）
# 内置 /api 代理，自动读取 .api-key，支持多模型路由
#
# 与 qwen3.5 / jina-embed 一样：默认用 nohup 后台运行，不占用终端、也不依赖 Cursor 会话。
# 需要 access 日志时先 export SERVE_UI_ACCESS_LOG=... SERVE_UI_LOG_BODY=1 再执行 start。

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

LOGS_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOGS_DIR/serve-ui.log"
PID_FILE="$LOGS_DIR/serve-ui.pid"

mkdir -p "$LOGS_DIR"

is_running() {
    [ -f "$PID_FILE" ] || return 1
    local p
    p=$(head -1 "$PID_FILE" 2>/dev/null | tr -d '\r\n')
    [ -n "$p" ] && kill -0 "$p" 2>/dev/null
}

cmd_start() {
    if is_running; then
        echo "serve-ui 已在运行（PID $(head -1 "$PID_FILE" | tr -d '\r\n')）"
        echo "日志: tail -f $LOG_FILE"
        exit 1
    fi
    if [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
        PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
    else
        PYTHON_BIN="python3"
    fi
    # 继承当前环境中的 UI_PORT、SERVE_UI_ACCESS_LOG、SERVE_UI_LOG_BODY、API_PROXY_TIMEOUT 等
    nohup "$PYTHON_BIN" "$SCRIPT_DIR/serve-ui.py" >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    echo "已启动 serve-ui（后台 PID $!，端口 ${UI_PORT:-8888}）"
    echo "日志: $LOG_FILE"
    echo "地址: http://localhost:${UI_PORT:-8888}/"
}

cmd_stop() {
    if ! is_running; then
        echo "serve-ui 未在运行"
        rm -f "$PID_FILE"
        return 0
    fi
    local p
    p=$(head -1 "$PID_FILE" | tr -d '\r\n')
    echo "停止 serve-ui (PID $p)..."
    kill "$p" 2>/dev/null || true
    sleep 1
    if kill -0 "$p" 2>/dev/null; then
        echo "强制终止..."
        kill -9 "$p" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "已停止 serve-ui"
}

cmd_status() {
    if is_running; then
        echo "serve-ui 运行中，PID $(head -1 "$PID_FILE" | tr -d '\r\n')，端口 ${UI_PORT:-8888}"
    else
        echo "serve-ui 未运行"
        rm -f "$PID_FILE"
    fi
}

cmd_foreground() {
    exec python3 "$SCRIPT_DIR/serve-ui.py"
}

case "${1:-start}" in
    start)
        cmd_start
        ;;
    stop)
        cmd_stop
        ;;
    status)
        cmd_status
        ;;
    foreground|fg)
        cmd_foreground
        ;;
    help|--help|-h)
        echo "用法: $0 {start|stop|status|foreground}"
        echo ""
        echo "  start      后台启动（nohup，与 manage.sh 启动的模型一样脱离终端）"
        echo "  stop       停止后台 serve-ui"
        echo "  status     是否运行"
        echo "  foreground 前台运行（调试用）"
        echo ""
        echo "示例（带 access 日志）:"
        echo "  export SERVE_UI_LOG_BODY=1"
        echo "  export SERVE_UI_ACCESS_LOG=\"$SCRIPT_DIR/serve-ui-access.jsonl\""
        echo "  $0 start"
        ;;
    *)
        echo "未知子命令: $1"
        echo "运行 $0 help 查看帮助"
        exit 1
        ;;
esac
