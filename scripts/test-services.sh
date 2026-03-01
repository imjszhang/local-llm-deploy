#!/usr/bin/env bash
# 本地服务接口测试脚本
# 用法: ./scripts/test-services.sh [--proxy PORT] [--jina PORT] [--no-auth]
# 参考: docs/jina-guide.md
#
# 测试项:
#   - Jina 直连: GET /health, POST /v1/embeddings（单条/批量/task/dimensions）
#   - 经 serve-ui 代理: POST /v1/embeddings, GET /api/models
#   - 可选: POST /v1/chat/completions（需有对话模型在跑）

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# 默认端口（与 docs/jina-guide.md 一致）
BASE_JINA="${BASE_JINA:-http://127.0.0.1:8004}"
BASE_PROXY="${BASE_PROXY:-http://localhost:8888}"

# API Key：优先环境变量，否则读项目根 .api-key
API_KEY="${OPENAI_API_KEY:-${API_KEY:-}}"
if [ -z "$API_KEY" ] && [ -f "$PROJECT_ROOT/.api-key" ]; then
    API_KEY=$(head -1 "$PROJECT_ROOT/.api-key" | tr -d '\r\n')
fi
SKIP_AUTH=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-auth) SKIP_AUTH=true; shift ;;
        --proxy)   BASE_PROXY="http://localhost:${2:-8888}"; shift 2 ;;
        --jina)    BASE_JINA="http://127.0.0.1:${2:-8004}"; shift 2 ;;
        *)         shift ;;
    esac
done

CURL_AUTH=()
if [ "$SKIP_AUTH" = false ] && [ -n "$API_KEY" ]; then
    CURL_AUTH=(-H "Authorization: Bearer $API_KEY")
fi

PASS=0
FAIL=0

run_test() {
    local name="$1"
    local method="$2"
    local url="$3"
    shift 3
    local extra=("$@")
    local out
    local code
    out=$(curl -s -w "\n%{http_code}" -X "$method" "$url" "${CURL_AUTH[@]}" "${extra[@]}" 2>/dev/null) || true
    code=$(echo "$out" | tail -1)
    local body
    body=$(echo "$out" | sed '$d')
    if [ "$code" = "200" ]; then
        echo "  [OK] $name (HTTP $code)"
        ((PASS+=1))
        return 0
    else
        echo "  [FAIL] $name (HTTP $code)"
        echo "$body" | head -3
        ((FAIL+=1))
        return 1
    fi
}

run_test_post_json() {
    local name="$1"
    local url="$2"
    local json="$3"
    local out
    local code
    out=$(curl -s -w "\n%{http_code}" -X POST "$url" \
        -H "Content-Type: application/json" \
        "${CURL_AUTH[@]}" \
        -d "$json" 2>/dev/null) || true
    code=$(echo "$out" | tail -1)
    local body
    body=$(echo "$out" | sed '$d')
    if [ "$code" = "200" ]; then
        echo "  [OK] $name (HTTP $code)"
        ((PASS+=1))
        return 0
    else
        echo "  [FAIL] $name (HTTP $code)"
        echo "$body" | head -5
        ((FAIL+=1))
        return 1
    fi
}

echo "=========================================="
echo "  本地服务接口测试"
echo "=========================================="
echo "Jina 后端: $BASE_JINA"
echo "代理:     $BASE_PROXY"
echo "认证:     $([ -n "$API_KEY" ] && echo '已配置' || echo '未配置 (可用 --no-auth 跳过校验)')"
echo "=========================================="
echo ""

# ---- Jina 直连 (8004) ----
echo ">>> Jina 直连 ($BASE_JINA)"
run_test "GET /health" GET "$BASE_JINA/health"
run_test_post_json "POST /v1/embeddings (单条)" "$BASE_JINA/v1/embeddings" \
    '{"model":"jina-embeddings-v5-text-small","input":"测试文本"}'
run_test_post_json "POST /v1/embeddings (批量)" "$BASE_JINA/v1/embeddings" \
    '{"model":"jina-embeddings-v5-text-small","input":["文本1","文本2"]}'
run_test_post_json "POST /v1/embeddings (task=text-matching)" "$BASE_JINA/v1/embeddings" \
    '{"model":"jina-embeddings-v5-text-small","input":["匹配测试"],"task":"text-matching"}'
run_test_post_json "POST /v1/embeddings (task=retrieval.query)" "$BASE_JINA/v1/embeddings" \
    '{"model":"jina-embeddings-v5-text-small","input":"用户问题","task":"retrieval.query"}'
run_test_post_json "POST /v1/embeddings (task=retrieval.passage)" "$BASE_JINA/v1/embeddings" \
    '{"model":"jina-embeddings-v5-text-small","input":["文档段落"],"task":"retrieval.passage"}'
run_test_post_json "POST /v1/embeddings (dimensions=256)" "$BASE_JINA/v1/embeddings" \
    '{"model":"jina-embeddings-v5-text-small","input":["维度截断"],"dimensions":256}'
echo ""

# ---- 经 serve-ui 代理 (8888) ----
echo ">>> 经 serve-ui 代理 ($BASE_PROXY)"
run_test "GET /api/models" GET "$BASE_PROXY/api/models"
run_test_post_json "POST /v1/embeddings (代理)" "$BASE_PROXY/v1/embeddings" \
    '{"model":"jina-embeddings-v5-text-small","input":["代理测试文本"]}'
run_test_post_json "POST /v1/embeddings (代理+task)" "$BASE_PROXY/v1/embeddings" \
    '{"model":"jina-embeddings-v5-text-small","input":["检索查询"],"task":"retrieval.query"}'
echo ""

# ---- 汇总 ----
echo "=========================================="
echo "  结果: 通过 $PASS, 失败 $FAIL"
echo "=========================================="
[ "$FAIL" -eq 0 ]
