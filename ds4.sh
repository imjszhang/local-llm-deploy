#!/usr/bin/env bash
# 管理 fork/ds4 的 ds4-server（外部后端，默认端口 8005）
#
# 默认通过 launchd 用户守护进程运行（无 Terminal 窗口，不依赖 Cursor 会话）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

LOGS_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOGS_DIR/ds4.log"
PID_FILE="$LOGS_DIR/ds4.pid"
START_SCRIPT="$SCRIPT_DIR/scripts/start-ds4.sh"
DAEMON_LABEL="com.local-llm-deploy.ds4"
PLIST="$HOME/Library/LaunchAgents/$DAEMON_LABEL.plist"
LAUNCH_DOMAIN="gui/$(id -u)"

DS4_ROOT="${DS4_ROOT:-/Users/jszhang/github/fork/ds4}"
DS4_BIN="${DS4_BIN:-$DS4_ROOT/ds4-server}"
DS4_MODEL="${DS4_MODEL:-$DS4_ROOT/ds4flash.gguf}"
DS4_HOST="${DS4_HOST:-127.0.0.1}"
DS4_PORT="${DS4_PORT:-8005}"
DS4_CTX="${DS4_CTX:-100000}"
DS4_BATCHED_SESSION="${DS4_BATCHED_SESSION:-4}"
DS4_KV_DIR="${DS4_KV_DIR:-/tmp/ds4-kv}"
DS4_KV_SPACE_MB="${DS4_KV_SPACE_MB:-8192}"
DS4_LOCK_FILE="${DS4_LOCK_FILE:-/tmp/ds4.lock}"

mkdir -p "$LOGS_DIR"

find_pid() {
    pgrep -f "${DS4_BIN}.*--port ${DS4_PORT}" 2>/dev/null | head -1 \
        || pgrep -x ds4-server 2>/dev/null | head -1 \
        || true
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

port_ready() {
    curl -sf --connect-timeout 1 "http://${DS4_HOST}:${DS4_PORT}/v1/models" >/dev/null 2>&1
}

daemon_loaded() {
    launchctl print "$LAUNCH_DOMAIN/$DAEMON_LABEL" >/dev/null 2>&1
}

write_plist() {
    mkdir -p "$HOME/Library/LaunchAgents"
    python3 - "$PLIST" "$START_SCRIPT" "$SCRIPT_DIR" "$LOG_FILE" \
        "$DS4_ROOT" "$DS4_BIN" "$DS4_MODEL" "$DS4_HOST" "$DS4_PORT" \
        "$DS4_CTX" "$DS4_BATCHED_SESSION" "$DS4_KV_DIR" "$DS4_KV_SPACE_MB" \
        "$DS4_LOCK_FILE" <<'PY'
import plistlib
import sys
from pathlib import Path

(
    plist_path,
    start_script,
    script_dir,
    log_file,
    ds4_root,
    ds4_bin,
    ds4_model,
    ds4_host,
    ds4_port,
    ds4_ctx,
    ds4_batched,
    ds4_kv_dir,
    ds4_kv_space,
    ds4_lock,
) = sys.argv[1:15]

env = {
    "DS4_ROOT": ds4_root,
    "DS4_BIN": ds4_bin,
    "DS4_MODEL": ds4_model,
    "DS4_HOST": ds4_host,
    "DS4_PORT": ds4_port,
    "DS4_CTX": ds4_ctx,
    "DS4_BATCHED_SESSION": ds4_batched,
    "DS4_KV_DIR": ds4_kv_dir,
    "DS4_KV_SPACE_MB": ds4_kv_space,
    "DS4_LOCK_FILE": ds4_lock,
}

data = {
    "Label": "com.local-llm-deploy.ds4",
    "ProgramArguments": [start_script],
    "WorkingDirectory": script_dir,
    "EnvironmentVariables": env,
    "StandardOutPath": log_file,
    "StandardErrorPath": log_file,
    "RunAtLoad": False,
    "KeepAlive": False,
}

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

wait_ready() {
    local i pid
    # 模型 mmap 通常数秒内就绪；留足余量
    for i in $(seq 1 120); do
        pid=$(find_pid)
        if [ -n "$pid" ]; then
            echo "$pid" >"$PID_FILE"
            if port_ready; then
                echo "已启动 ds4 (launchd PID ${pid}, port ${DS4_PORT})"
                echo "模型: antirez/deepseek-v4-flash"
                echo "日志: ${LOG_FILE}"
                echo "地址: http://${DS4_HOST}:${DS4_PORT}/v1"
                return 0
            fi
        fi
        sleep 0.5
    done
    echo "ds4 启动超时，请查看日志: tail -f $LOG_FILE"
    return 1
}

cmd_start_daemon() {
    if is_running && port_ready; then
        echo "ds4 已在运行 (PID $(head -1 "$PID_FILE" | tr -d '\r\n'), port ${DS4_PORT})"
        echo "日志: tail -f ${LOG_FILE}"
        return 0
    fi

    # 清掉非本守护进程占用的旧实例 / 残留锁
    if is_running; then
        echo "停止旧 ds4 进程以便由 launchd 接管..."
        cmd_stop >/dev/null || true
    fi
    rm -f "$DS4_LOCK_FILE"

    write_plist
    if daemon_loaded; then
        launchctl bootout "$LAUNCH_DOMAIN/$DAEMON_LABEL" 2>/dev/null || true
    fi
    launchctl bootstrap "$LAUNCH_DOMAIN" "$PLIST"
    launchctl kickstart -k "$LAUNCH_DOMAIN/$DAEMON_LABEL"
    wait_ready
}

cmd_start_nohup() {
    if is_running && port_ready; then
        echo "ds4 已在运行 (PID $(head -1 "$PID_FILE" | tr -d '\r\n'))"
        echo "日志: tail -f ${LOG_FILE}"
        exit 1
    fi
    if is_running; then
        cmd_stop >/dev/null || true
    fi
    rm -f "$DS4_LOCK_FILE"
    nohup "$START_SCRIPT" >>"$LOG_FILE" 2>&1 </dev/null &
    local pid=$!
    disown -h "$pid" 2>/dev/null || true
    echo "$pid" >"$PID_FILE"
    wait_ready
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
        rm -f "$PID_FILE" "$DS4_LOCK_FILE"
        echo "ds4 未在运行"
        return 0
    fi
    echo "停止 ds4 (PID $p)..."
    kill -TERM "$p" 2>/dev/null || true
    local i
    for i in $(seq 1 30); do
        if ! kill -0 "$p" 2>/dev/null; then
            break
        fi
        sleep 0.5
    done
    if kill -0 "$p" 2>/dev/null; then
        echo "仍未退出，再发 SIGTERM..."
        kill -TERM "$p" 2>/dev/null || true
        sleep 2
    fi
    if kill -0 "$p" 2>/dev/null; then
        echo "强制终止..."
        kill -9 "$p" 2>/dev/null || true
    fi
    rm -f "$PID_FILE" "$DS4_LOCK_FILE"
    echo "已停止 ds4"
}

cmd_status() {
    sync_pid_file
    if is_running; then
        local mode="process"
        local ready="port-not-ready"
        local pid
        pid=$(head -1 "$PID_FILE" | tr -d '\r\n')
        daemon_loaded && mode="launchd"
        port_ready && ready="ready"
        echo "ds4 running (${mode}), PID ${pid}, port ${DS4_PORT} (${ready})"
    else
        echo "ds4 未运行"
    fi
}

cmd_foreground() {
    rm -f "$DS4_LOCK_FILE"
    exec "$START_SCRIPT"
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
        echo "  start      通过 launchd 用户守护进程启动（默认）"
        echo "  stop       停止 ds4（launchd + 进程）"
        echo "  status     是否运行 / 端口是否就绪"
        echo "  foreground 前台运行（调试用）"
        echo "  nohup      传统 nohup 后台启动"
        echo "  daemon     install | uninstall | start"
        echo ""
        echo "环境变量: DS4_ROOT DS4_PORT DS4_CTX DS4_BATCHED_SESSION 等（见 scripts/start-ds4.sh）"
        ;;
    *)
        echo "未知子命令: $1"
        echo "运行 $0 help 查看帮助"
        exit 1
        ;;
esac
