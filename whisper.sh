#!/usr/bin/env bash
# 管理 Whisper ASR（serve_whisper.py），默认通过 launchd 用户守护进程常驻。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

LOGS_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOGS_DIR/whisper-large-v3.log"
PID_FILE="$LOGS_DIR/whisper.pid"
RUN_PID_FILE="$SCRIPT_DIR/run/whisper-large-v3.pid"
START_SCRIPT="$SCRIPT_DIR/scripts/start-whisper.sh"
DAEMON_LABEL="com.local-llm-deploy.whisper"
PLIST="$HOME/Library/LaunchAgents/$DAEMON_LABEL.plist"
LAUNCH_DOMAIN="gui/$(id -u)"

WHISPER_MODEL_NAME="${WHISPER_MODEL_NAME:-whisper-large-v3}"
WHISPER_HOST="${WHISPER_HOST:-127.0.0.1}"
WHISPER_PORT="${WHISPER_PORT:-8007}"

mkdir -p "$LOGS_DIR" "$SCRIPT_DIR/run"

find_pid() {
    pgrep -f "${SCRIPT_DIR}/serve_whisper.py.*--port ${WHISPER_PORT}" 2>/dev/null | head -1 \
        || pgrep -f "${SCRIPT_DIR}/serve_whisper.py" 2>/dev/null | head -1 \
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
    curl -sf --connect-timeout 1 "http://${WHISPER_HOST}:${WHISPER_PORT}/health" >/dev/null 2>&1
}

daemon_loaded() {
    launchctl print "$LAUNCH_DOMAIN/$DAEMON_LABEL" >/dev/null 2>&1
}

write_plist() {
    mkdir -p "$HOME/Library/LaunchAgents"
    # Prefer project ffmpeg if present
    local path_env="$PATH"
    if [[ -x "$SCRIPT_DIR/tools/ffmpeg" ]]; then
        path_env="$SCRIPT_DIR/tools:$PATH"
    fi

    python3 - "$PLIST" "$START_SCRIPT" "$SCRIPT_DIR" "$LOG_FILE" \
        "$WHISPER_MODEL_NAME" "$WHISPER_HOST" "$WHISPER_PORT" "$path_env" <<'PY'
import plistlib
import sys
from pathlib import Path

(
    plist_path,
    start_script,
    script_dir,
    log_file,
    model_name,
    host,
    port,
    path_env,
) = sys.argv[1:9]

data = {
    "Label": "com.local-llm-deploy.whisper",
    "ProgramArguments": [start_script],
    "WorkingDirectory": script_dir,
    "EnvironmentVariables": {
        "WHISPER_MODEL_NAME": model_name,
        "WHISPER_HOST": host,
        "WHISPER_PORT": port,
        "PATH": path_env,
    },
    "StandardOutPath": log_file,
    "StandardErrorPath": log_file,
    "RunAtLoad": True,
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
    rm -f "$PLIST" "$PID_FILE"
    echo "已卸载 LaunchAgent"
}

wait_ready() {
    local i pid
    for i in $(seq 1 120); do
        pid=$(find_pid)
        if [ -n "$pid" ]; then
            echo "$pid" >"$PID_FILE"
            if port_ready; then
                echo "已启动 whisper (launchd PID ${pid}, port ${WHISPER_PORT})"
                echo "模型: ${WHISPER_MODEL_NAME}"
                echo "日志: ${LOG_FILE}"
                echo "健康检查: http://${WHISPER_HOST}:${WHISPER_PORT}/health"
                return 0
            fi
        fi
        sleep 0.5
    done
    echo "whisper 启动超时，请查看日志: tail -f ${LOG_FILE}"
    return 1
}

cmd_start_daemon() {
    if is_running && port_ready; then
        echo "whisper 已在运行 (PID $(head -1 "$PID_FILE" | tr -d '\r\n'), port ${WHISPER_PORT})"
        echo "日志: tail -f ${LOG_FILE}"
        return 0
    fi

    if is_running; then
        echo "停止旧 whisper 进程以便由 launchd 接管..."
        cmd_stop >/dev/null || true
    fi

    write_plist
    if daemon_loaded; then
        launchctl bootout "$LAUNCH_DOMAIN/$DAEMON_LABEL" 2>/dev/null || true
    fi
    # truncate/append: launchd reopens log paths; keep history by not truncating here
    launchctl bootstrap "$LAUNCH_DOMAIN" "$PLIST"
    launchctl kickstart -k "$LAUNCH_DOMAIN/$DAEMON_LABEL"
    wait_ready
}

cmd_start_nohup() {
    if is_running && port_ready; then
        echo "whisper 已在运行 (PID $(head -1 "$PID_FILE" | tr -d '\r\n'))"
        echo "日志: tail -f ${LOG_FILE}"
        exit 1
    fi
    if is_running; then
        cmd_stop >/dev/null || true
    fi
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
        rm -f "$PID_FILE" "$RUN_PID_FILE"
        echo "whisper 未在运行"
        return 0
    fi
    echo "停止 whisper (PID $p)..."
    kill -TERM "$p" 2>/dev/null || true
    local i
    for i in $(seq 1 30); do
        if ! kill -0 "$p" 2>/dev/null; then
            break
        fi
        sleep 0.5
    done
    if kill -0 "$p" 2>/dev/null; then
        echo "仍未退出，强制终止..."
        kill -9 "$p" 2>/dev/null || true
    fi
    rm -f "$PID_FILE" "$RUN_PID_FILE"
    echo "已停止 whisper"
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
        echo "whisper running (${mode}), PID ${pid}, port ${WHISPER_PORT} (${ready})"
    else
        echo "whisper 未运行"
    fi
}

cmd_foreground() {
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
        echo "  stop       停止 whisper（launchd + 进程）"
        echo "  status     是否运行 / 端口是否就绪"
        echo "  foreground 前台运行（调试用）"
        echo "  nohup      传统 nohup 后台启动"
        echo "  daemon     install | uninstall | start"
        echo ""
        echo "环境变量: WHISPER_MODEL_NAME WHISPER_HOST WHISPER_PORT"
        ;;
    *)
        echo "未知子命令: $1"
        echo "运行 $0 help 查看帮助"
        exit 1
        ;;
esac
