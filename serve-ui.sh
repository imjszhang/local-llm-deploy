#!/bin/bash
# 启动前端静态服务（监控面板 + 多模型 API 代理）
# 内置 /api 代理，自动读取 .api-key，支持多模型路由
#
# 默认通过 launchd 用户守护进程运行（无 Terminal 窗口，不依赖 Cursor 会话）。
# 需要 access 日志时先 export SERVE_UI_ACCESS_LOG=... SERVE_UI_LOG_BODY=1 再执行 start。

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

LOGS_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOGS_DIR/serve-ui.log"
PID_FILE="$LOGS_DIR/serve-ui.pid"
DAEMON_LABEL="com.local-llm-deploy.serve-ui"
PLIST="$HOME/Library/LaunchAgents/$DAEMON_LABEL.plist"
LAUNCH_DOMAIN="gui/$(id -u)"

mkdir -p "$LOGS_DIR"

python_bin() {
    if [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
        echo "$SCRIPT_DIR/.venv/bin/python"
    else
        echo python3
    fi
}

find_pid() {
    pgrep -f "${SCRIPT_DIR}/serve-ui.py" 2>/dev/null | head -1
}

sync_pid_file() {
    local p
    p=$(find_pid)
    if [ -n "$p" ]; then
        echo "$p" >"$PID_FILE"
    else
        rm -f "$PID_FILE"
    fi
}

is_running() {
    local p
    p=$(find_pid)
    if [ -n "$p" ]; then
        echo "$p" >"$PID_FILE"
        return 0
    fi
    rm -f "$PID_FILE"
    return 1
}

daemon_loaded() {
    launchctl print "$LAUNCH_DOMAIN/$DAEMON_LABEL" >/dev/null 2>&1
}

write_plist() {
    local python_bin_path
    python_bin_path=$(python_bin)
    mkdir -p "$HOME/Library/LaunchAgents"

    python3 - "$PLIST" "$python_bin_path" "$SCRIPT_DIR" "$LOG_FILE" "$UI_PORT" \
        "$SERVE_UI_ACCESS_LOG" "$SERVE_UI_LOG_BODY" "$API_PROXY_TIMEOUT" "$OLLAMA_HOST" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path, python_bin, script_dir, log_file, ui_port, access_log, log_body, proxy_timeout, ollama_host = sys.argv[1:10]

env = {}
if ui_port:
    env["UI_PORT"] = ui_port
if access_log:
    env["SERVE_UI_ACCESS_LOG"] = access_log
if log_body:
    env["SERVE_UI_LOG_BODY"] = log_body
if proxy_timeout:
    env["API_PROXY_TIMEOUT"] = proxy_timeout
if ollama_host:
    env["OLLAMA_HOST"] = ollama_host

data = {
    "Label": "com.local-llm-deploy.serve-ui",
    "ProgramArguments": [python_bin, str(Path(script_dir) / "serve-ui.py")],
    "WorkingDirectory": script_dir,
    "StandardOutPath": log_file,
    "StandardErrorPath": log_file,
    "RunAtLoad": False,
    "KeepAlive": False,
}
if env:
    data["EnvironmentVariables"] = env

Path(plist_path).write_bytes(plistlib.dumps(data))
PY
}

cmd_daemon_install() {
    write_plist
    echo "已写入 LaunchAgent: $PLIST"
}

cmd_daemon_uninstall() {
    if daemon_loaded; then
        launchctl bootout "$LAUNCH_DOMAIN/$DAEMON_LABEL" 2>/dev/null || true
    fi
    rm -f "$PLIST"
    rm -f "$PID_FILE"
    echo "已卸载 LaunchAgent"
}

cmd_start_daemon() {
    if is_running; then
        echo "serve-ui 已在运行（PID $(head -1 "$PID_FILE" | tr -d '\r\n')）"
        echo "日志: tail -f $LOG_FILE"
        return 0
    fi

    write_plist
    if daemon_loaded; then
        launchctl bootout "$LAUNCH_DOMAIN/$DAEMON_LABEL" 2>/dev/null || true
    fi
    launchctl bootstrap "$LAUNCH_DOMAIN" "$PLIST"
    launchctl kickstart -k "$LAUNCH_DOMAIN/$DAEMON_LABEL"

    local i pid
    for i in $(seq 1 20); do
        sleep 0.5
        pid=$(find_pid)
        if [ -n "$pid" ]; then
            echo "$pid" >"$PID_FILE"
            echo "已启动 serve-ui（launchd 守护进程 PID $pid，端口 ${UI_PORT:-8888}）"
            echo "日志: $LOG_FILE"
            echo "地址: http://localhost:${UI_PORT:-8888}/"
            return 0
        fi
    done

    echo "serve-ui 启动超时，请查看日志: tail -f $LOG_FILE"
    return 1
}

cmd_start_nohup() {
    if is_running; then
        echo "serve-ui 已在运行（PID $(head -1 "$PID_FILE" | tr -d '\r\n')）"
        echo "日志: tail -f $LOG_FILE"
        exit 1
    fi
    local PYTHON_BIN pid
    PYTHON_BIN=$(python_bin)
    nohup "$PYTHON_BIN" "$SCRIPT_DIR/serve-ui.py" >>"$LOG_FILE" 2>&1 < /dev/null &
    pid=$!
    disown -h "$pid" 2>/dev/null || true
    echo "$pid" >"$PID_FILE"
    echo "已启动 serve-ui（nohup 后台 PID $pid，端口 ${UI_PORT:-8888}）"
    echo "日志: $LOG_FILE"
    echo "地址: http://localhost:${UI_PORT:-8888}/"
}

cmd_start() {
    cmd_start_daemon
}

cmd_stop() {
    local p
    if daemon_loaded; then
        launchctl bootout "$LAUNCH_DOMAIN/$DAEMON_LABEL" 2>/dev/null || true
    fi
    p=$(find_pid)
    if [ -z "$p" ]; then
        rm -f "$PID_FILE"
        echo "serve-ui 未在运行"
        return 0
    fi
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
    sync_pid_file
    if is_running; then
        local mode="nohup"
        daemon_loaded && mode="launchd"
        echo "serve-ui 运行中（$mode），PID $(head -1 "$PID_FILE" | tr -d '\r\n')，端口 ${UI_PORT:-8888}"
    else
        echo "serve-ui 未运行"
    fi
}

cmd_foreground() {
    exec "$(python_bin)" "$SCRIPT_DIR/serve-ui.py"
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
    nohup)
        shift
        cmd_start_nohup "$@"
        ;;
    daemon)
        shift
        case "${1:-start}" in
            install)
                cmd_daemon_install
                ;;
            uninstall)
                cmd_daemon_uninstall
                ;;
            start)
                cmd_start_daemon
                ;;
            *)
                echo "用法: $0 daemon {install|uninstall|start}"
                exit 1
                ;;
        esac
        ;;
    help|--help|-h)
        echo "用法: $0 {start|stop|status|foreground|nohup|daemon}"
        echo ""
        echo "  start      通过 launchd 用户守护进程启动（默认，无 Terminal 窗口）"
        echo "  stop       停止 serve-ui（launchd + 进程）"
        echo "  status     是否运行"
        echo "  foreground 前台运行（调试用）"
        echo "  nohup      传统 nohup 后台启动"
        echo "  daemon     install | uninstall | start"
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
