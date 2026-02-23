#!/bin/bash
# Local LLM Deploy — 统一模型管理入口
# 用法: ./manage.sh <命令> [参数]

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODELS_JSON="$SCRIPT_DIR/models.json"
RUN_DIR="$SCRIPT_DIR/run"
LOGS_DIR="$SCRIPT_DIR/logs"
MODELS_DIR="$SCRIPT_DIR/models"

mkdir -p "$RUN_DIR" "$LOGS_DIR"

# ── JSON 辅助函数（通过 python3 解析，macOS 自带） ──

json_get() {
    python3 -c "
import json, sys
with open('$MODELS_JSON') as f:
    data = json.load(f)
path = '$1'.split('.')
obj = data
for p in path:
    if isinstance(obj, dict) and p in obj:
        obj = obj[p]
    else:
        sys.exit(1)
if isinstance(obj, (dict, list)):
    print(json.dumps(obj))
else:
    print(obj)
" 2>/dev/null
}

json_list_models() {
    python3 -c "
import json
with open('$MODELS_JSON') as f:
    data = json.load(f)
for name in data:
    print(name)
"
}

json_model_field() {
    local model="$1" field="$2"
    python3 -c "
import json, sys
with open('$MODELS_JSON') as f:
    data = json.load(f)
if '$model' not in data:
    sys.exit(1)
m = data['$model']
val = m.get('$field')
if val is None:
    sys.exit(1)
if isinstance(val, (dict, list)):
    print(json.dumps(val))
else:
    print(val)
" 2>/dev/null
}

json_quant_field() {
    local model="$1" quant="$2" field="$3"
    python3 -c "
import json, sys
with open('$MODELS_JSON') as f:
    data = json.load(f)
if '$model' not in data:
    sys.exit(1)
q = data['$model'].get('quants', {}).get('$quant')
if q is None:
    sys.exit(1)
val = q.get('$field')
if val is None:
    sys.exit(1)
print(val)
" 2>/dev/null
}

json_params() {
    local model="$1"
    python3 -c "
import json, sys
with open('$MODELS_JSON') as f:
    data = json.load(f)
if '$model' not in data:
    sys.exit(1)
params = data['$model'].get('params', {})
print(json.dumps(params))
" 2>/dev/null
}

# ── PID 管理 ──

get_pid_file() {
    echo "$RUN_DIR/$1.pid"
}

is_running() {
    local model="$1"
    local pid_file
    pid_file=$(get_pid_file "$model")
    if [ ! -f "$pid_file" ]; then
        return 1
    fi
    local pid
    pid=$(head -1 "$pid_file" 2>/dev/null | tr -d '\r\n')
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 0
    else
        rm -f "$pid_file"
        return 1
    fi
}

get_running_port() {
    local model="$1"
    local pid_file
    pid_file=$(get_pid_file "$model")
    if [ -f "$pid_file" ]; then
        sed -n '2p' "$pid_file" 2>/dev/null | tr -d '\r\n'
    fi
}

get_running_pid() {
    local model="$1"
    local pid_file
    pid_file=$(get_pid_file "$model")
    if [ -f "$pid_file" ]; then
        head -1 "$pid_file" 2>/dev/null | tr -d '\r\n'
    fi
}

# 模型的 GGUF 目录路径
get_model_dir() {
    local model="$1" quant="$2"
    local repo_name
    repo_name=$(json_model_field "$model" "repo_name" 2>/dev/null) || true
    if [ -z "$repo_name" ]; then
        local repo_id
        repo_id=$(json_model_field "$model" "repo_id") || return 1
        repo_name=$(echo "$repo_id" | tr '/' '-')
    fi
    echo "$MODELS_DIR/$repo_name/$quant"
}

# 检查模型是否已下载
is_downloaded() {
    local model="$1"
    local model_type
    model_type=$(json_model_field "$model" "type" 2>/dev/null) || true

    if [ "$model_type" = "embedding" ]; then
        local repo_name
        repo_name=$(json_model_field "$model" "repo_name" 2>/dev/null) || true
        if [ -z "$repo_name" ]; then
            local repo_id
            repo_id=$(json_model_field "$model" "repo_id") || return 1
            repo_name=$(echo "$repo_id" | tr '/' '-')
        fi
        local model_dir="$MODELS_DIR/$repo_name"
        local st_count
        st_count=$(find "$model_dir" -maxdepth 1 -name "*.safetensors" 2>/dev/null | wc -l | tr -d ' ')
        [ "$st_count" -gt 0 ]
    else
        local default_quant
        default_quant=$(json_model_field "$model" "default_quant") || return 1
        local model_dir
        model_dir=$(get_model_dir "$model" "$default_quant") || return 1
        local gguf_count
        gguf_count=$(find "$model_dir" -maxdepth 1 -name "*.gguf" 2>/dev/null | wc -l | tr -d ' ')
        [ "$gguf_count" -gt 0 ]
    fi
}

# ── 命令实现 ──

cmd_list() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}   已注册模型${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""
    printf "  ${CYAN}%-15s %-10s %-12s %-12s %-10s %s${NC}\n" "模型" "类型" "默认端口" "已下载" "状态" "详情"
    echo "  ──────────────────────────────────────────────────────────────────────────"

    for model in $(json_list_models); do
        local port model_type
        port=$(json_model_field "$model" "default_port")
        model_type=$(json_model_field "$model" "type" 2>/dev/null) || model_type="chat"

        local downloaded="否"
        if is_downloaded "$model" 2>/dev/null; then
            downloaded="${GREEN}是${NC}"
        else
            downloaded="${YELLOW}否${NC}"
        fi

        local status_text
        if is_running "$model"; then
            local running_port
            running_port=$(get_running_port "$model")
            status_text="${GREEN}运行中 :${running_port}${NC}"
        else
            status_text="${YELLOW}已停止${NC}"
        fi

        local detail
        if [ "$model_type" = "embedding" ]; then
            detail="safetensors"
        else
            detail=$(python3 -c "
import json
with open('$MODELS_JSON') as f:
    data = json.load(f)
qs = list(data.get('$model', {}).get('quants', {}).keys())
print(', '.join(qs))
" 2>/dev/null)
        fi

        printf "  %-15s %-10s %-12s %-22b %-20b %s\n" "$model" "$model_type" "$port" "$downloaded" "$status_text" "$detail"
    done
    echo ""
}

cmd_status() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}   运行中的模型实例${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""

    local found=false
    for model in $(json_list_models); do
        if is_running "$model"; then
            found=true
            local pid port
            pid=$(get_running_pid "$model")
            port=$(get_running_port "$model")
            local log_file="$LOGS_DIR/$model.log"

            # macOS 上获取进程运行时间和内存
            local elapsed_str mem_str
            elapsed_str=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ' || echo "?")
            mem_str=$(ps -o rss= -p "$pid" 2>/dev/null | awk '{printf "%.1f GB", $1/1024/1024}' 2>/dev/null || echo "?")

            echo -e "  ${GREEN}$model${NC}"
            echo -e "    PID:    $pid"
            echo -e "    端口:   $port"
            echo -e "    运行:   $elapsed_str"
            echo -e "    内存:   $mem_str"
            echo -e "    日志:   $log_file"
            echo ""
        fi
    done

    if [ "$found" = false ]; then
        echo -e "  ${YELLOW}无运行中的模型实例${NC}"
        echo ""
    fi
}

cmd_download() {
    local model="$1"
    shift || true

    if [ -z "$model" ]; then
        echo -e "${RED}用法: $0 download <模型名> [--quant X]${NC}"
        echo "可用模型: $(json_list_models | tr '\n' ' ')"
        exit 1
    fi

    json_model_field "$model" "repo_id" > /dev/null || {
        echo -e "${RED}错误: 未知模型 '$model'${NC}"
        echo "可用模型: $(json_list_models | tr '\n' ' ')"
        exit 1
    }

    local model_type
    model_type=$(json_model_field "$model" "type" 2>/dev/null) || model_type=""

    if [ "$model_type" = "embedding" ]; then
        exec "$SCRIPT_DIR/download_jina_embeddings.py"
    else
        exec "$SCRIPT_DIR/download.sh" "$model" "$@"
    fi
}

cmd_start() {
    local model="$1"
    shift || true

    if [ -z "$model" ]; then
        echo -e "${RED}用法: $0 start <模型名> [--port P] [--quant X] [其他参数]${NC}"
        echo "可用模型: $(json_list_models | tr '\n' ' ')"
        exit 1
    fi

    json_model_field "$model" "repo_id" > /dev/null || {
        echo -e "${RED}错误: 未知模型 '$model'${NC}"
        echo "可用模型: $(json_list_models | tr '\n' ' ')"
        exit 1
    }

    if is_running "$model"; then
        local running_port
        running_port=$(get_running_port "$model")
        echo -e "${YELLOW}$model 已在运行中（端口 $running_port）${NC}"
        echo "如需重启，请先执行: $0 stop $model"
        exit 1
    fi

    local model_type
    model_type=$(json_model_field "$model" "type" 2>/dev/null) || model_type=""

    if [ "$model_type" = "embedding" ]; then
        local emb_port emb_host
        emb_port=$(json_model_field "$model" "default_port" 2>/dev/null) || emb_port="8004"
        emb_host="127.0.0.1"

        # 解析可选参数
        while [[ $# -gt 0 ]]; do
            case $1 in
                --port)  emb_port="$2"; shift 2 ;;
                --host)  emb_host="$2"; shift 2 ;;
                --lan)   emb_host="0.0.0.0"; shift ;;
                *)       shift ;;
            esac
        done

        local log_file="$LOGS_DIR/$model.log"
        echo -e "${BLUE}启动 embedding 服务: $model${NC}"

        if [ -f "$SCRIPT_DIR/.venv-embed/bin/python" ]; then
            PYTHON_BIN="$SCRIPT_DIR/.venv-embed/bin/python"
        elif [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
            PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
        else
            PYTHON_BIN="${PYTHON_BIN:-$(which python3 2>/dev/null || which python 2>/dev/null)}"
        fi

        nohup "$PYTHON_BIN" "$SCRIPT_DIR/serve_embedding.py" \
            --model-name "$model" \
            --port "$emb_port" \
            --host "$emb_host" \
            > "$log_file" 2>&1 &

        echo -e "${GREEN}已启动 $model (PID $!, 端口 $emb_port)${NC}"
        echo "日志: tail -f $log_file"
    else
        exec "$SCRIPT_DIR/deploy.sh" --model-name "$model" "$@"
    fi
}

cmd_stop() {
    local target="$1"

    if [ "$target" = "--all" ]; then
        local stopped=false
        for model in $(json_list_models); do
            if is_running "$model"; then
                local pid
                pid=$(get_running_pid "$model")
                echo -e "${YELLOW}停止 $model (PID $pid)...${NC}"
                kill "$pid" 2>/dev/null || true
                rm -f "$(get_pid_file "$model")"
                stopped=true
            fi
        done
        if [ "$stopped" = false ]; then
            echo -e "${YELLOW}无运行中的模型实例${NC}"
        else
            echo -e "${GREEN}已停止所有模型${NC}"
        fi
        return
    fi

    if [ -z "$target" ]; then
        echo -e "${RED}用法: $0 stop <模型名> 或 $0 stop --all${NC}"
        exit 1
    fi

    if ! is_running "$target"; then
        echo -e "${YELLOW}$target 未在运行中${NC}"
        return
    fi

    local pid
    pid=$(get_running_pid "$target")
    echo -e "${YELLOW}停止 $target (PID $pid)...${NC}"
    kill "$pid" 2>/dev/null || true
    rm -f "$(get_pid_file "$target")"
    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
        echo -e "${YELLOW}进程仍在运行，强制终止...${NC}"
        kill -9 "$pid" 2>/dev/null || true
    fi
    echo -e "${GREEN}已停止 $target${NC}"
}

cmd_logs() {
    local model="$1"
    if [ -z "$model" ]; then
        echo -e "${RED}用法: $0 logs <模型名>${NC}"
        exit 1
    fi
    local log_file="$LOGS_DIR/$model.log"
    if [ ! -f "$log_file" ]; then
        echo -e "${YELLOW}日志文件不存在: $log_file${NC}"
        exit 1
    fi
    exec tail -f "$log_file"
}

cmd_help() {
    echo "用法: $0 <命令> [参数]"
    echo ""
    echo "命令:"
    echo "  list                           列出所有已注册模型"
    echo "  status                         查看运行中的模型实例"
    echo "  download <模型名> [--quant X] [--source S]  下载指定模型"
    echo "  start <模型名> [选项]          启动指定模型"
    echo "  stop <模型名>                  停止指定模型"
    echo "  stop --all                     停止所有模型"
    echo "  logs <模型名>                  查看模型日志"
    echo "  help                           显示帮助"
    echo ""
    echo "示例:"
    echo "  $0 list"
    echo "  $0 download glm-5                              # 默认从 ModelScope 下载"
    echo "  $0 download glm-5 --source huggingface          # 从 HuggingFace 下载"
    echo "  $0 download qwen3.5 --quant UD-Q2_K_XL"
    echo "  $0 download jina-embed                          # 下载 embedding 模型"
    echo "  $0 start glm-5"
    echo "  $0 start qwen3.5 --port 8003"
    echo "  $0 start jina-embed                             # 启动 embedding 服务"
    echo "  $0 stop glm-5"
    echo "  $0 stop --all"
    echo "  $0 status"
    echo "  $0 logs qwen3.5"
}

# ── 主入口 ──

if [ ! -f "$MODELS_JSON" ]; then
    echo -e "${RED}错误: models.json 不存在: $MODELS_JSON${NC}"
    exit 1
fi

case "${1:-help}" in
    list)       cmd_list ;;
    status)     cmd_status ;;
    download)   shift; cmd_download "$@" ;;
    start)      shift; cmd_start "$@" ;;
    stop)       shift; cmd_stop "$@" ;;
    logs)       shift; cmd_logs "$@" ;;
    help|--help|-h) cmd_help ;;
    *)
        echo -e "${RED}未知命令: $1${NC}"
        cmd_help
        exit 1
        ;;
esac
