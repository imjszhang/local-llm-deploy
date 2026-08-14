#!/usr/bin/env bash
# 通过 launchd 管理 Jina Embedding / Rerank 服务。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCH_DOMAIN="gui/$(id -u)"
SERVICE="${1:-}"
COMMAND="${2:-status}"

usage() {
    echo "用法: $0 {embed|rerank|all} {start|stop|status}"
}

configure() {
    case "$1" in
        embed)
            MODEL_NAME="jina-embed"
            PORT="${JINA_EMBED_PORT:-8004}"
            START_SCRIPT="$SCRIPT_DIR/scripts/start-jina-embed.sh"
            LABEL="com.local-llm-deploy.jina-embed"
            ;;
        rerank)
            MODEL_NAME="jina-rerank-mlx"
            PORT="${JINA_RERANK_PORT:-8006}"
            START_SCRIPT="$SCRIPT_DIR/scripts/start-jina-rerank.sh"
            LABEL="com.local-llm-deploy.jina-rerank"
            ;;
        *)
            return 1
            ;;
    esac
    HOST="${JINA_HOST:-127.0.0.1}"
    LOG_FILE="$SCRIPT_DIR/logs/${MODEL_NAME}.log"
    PID_FILE="$SCRIPT_DIR/run/${MODEL_NAME}.pid"
    PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
}

daemon_loaded() {
    launchctl print "$LAUNCH_DOMAIN/$LABEL" >/dev/null 2>&1
}

find_pid() {
    pgrep -f "${SCRIPT_DIR}/serve_(embedding|rerank).py.*--model-name ${MODEL_NAME}" 2>/dev/null \
        | head -1 || true
}

ready() {
    curl -sf --connect-timeout 1 "http://${HOST}:${PORT}/health" >/dev/null 2>&1
}

write_plist() {
    mkdir -p "$HOME/Library/LaunchAgents" "$SCRIPT_DIR/logs" "$SCRIPT_DIR/run"
    python3 - "$PLIST" "$LABEL" "$START_SCRIPT" "$SCRIPT_DIR" "$LOG_FILE" \
        "$MODEL_NAME" "$HOST" "$PORT" <<'PY'
import plistlib
import sys
from pathlib import Path

plist, label, start_script, workdir, log_file, model, host, port = sys.argv[1:9]
data = {
    "Label": label,
    "ProgramArguments": [start_script],
    "WorkingDirectory": workdir,
    "EnvironmentVariables": {
        "JINA_MODEL_NAME": model,
        "JINA_HOST": host,
        "JINA_PORT": port,
    },
    "StandardOutPath": log_file,
    "StandardErrorPath": log_file,
    "RunAtLoad": False,
    # launchd 会在异常退出后重启；stop 通过 bootout 明确卸载。
    "KeepAlive": {"SuccessfulExit": False},
    "ProcessType": "Interactive",
}
Path(plist).write_bytes(plistlib.dumps(data))
PY
}

start_service() {
    local pid i
    if ready; then
        pid=$(find_pid)
        echo "${MODEL_NAME} 已运行 (PID ${pid:-unknown}, port ${PORT})"
        return 0
    fi

    if daemon_loaded; then
        launchctl bootout "$LAUNCH_DOMAIN/$LABEL" 2>/dev/null || true
    fi
    write_plist
    launchctl bootstrap "$LAUNCH_DOMAIN" "$PLIST"
    launchctl kickstart -k "$LAUNCH_DOMAIN/$LABEL"

    for i in $(seq 1 180); do
        if ready; then
            pid=$(find_pid)
            echo "${MODEL_NAME} 已启动 (launchd PID ${pid:-unknown}, port ${PORT})"
            echo "日志: ${LOG_FILE}"
            return 0
        fi
        sleep 1
    done
    echo "${MODEL_NAME} 启动超时，查看日志: ${LOG_FILE}" >&2
    return 1
}

stop_service() {
    local pid
    if daemon_loaded; then
        launchctl bootout "$LAUNCH_DOMAIN/$LABEL" 2>/dev/null || true
    fi
    pid=$(find_pid)
    if [[ -n "$pid" ]]; then
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.5
        done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "${MODEL_NAME} 已停止"
}

status_service() {
    local pid mode="process" state="port-not-ready"
    pid=$(find_pid)
    daemon_loaded && mode="launchd"
    ready && state="ready"
    if [[ -n "$pid" ]]; then
        echo "${MODEL_NAME} running (${mode}), PID ${pid}, port ${PORT} (${state})"
    else
        echo "${MODEL_NAME} 未运行"
    fi
}

run_one() {
    configure "$1" || { usage; exit 1; }
    case "$COMMAND" in
        start) start_service ;;
        stop) stop_service ;;
        status) status_service ;;
        *) usage; exit 1 ;;
    esac
}

case "$SERVICE" in
    embed|rerank)
        run_one "$SERVICE"
        ;;
    all)
        run_one embed
        run_one rerank
        ;;
    *)
        usage
        exit 1
        ;;
esac
