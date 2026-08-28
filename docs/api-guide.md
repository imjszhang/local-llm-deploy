# serve-ui API 外部使用指南

> 本文档说明如何从外部（脚本、SDK、第三方应用）调用 `serve-ui.py` 提供的 HTTP 接口。

---

## 一、服务地址与认证

### 1.1 基址与端口

- **默认端口**：`8888`（可通过环境变量 `UI_PORT` 修改）
- **基址示例**：`http://localhost:8888` 或 `http://<主机>:8888`

### 1.2 认证（可选）

若项目根目录存在 `.api-key` 文件，则 **所有 `/v1/*` 请求** 必须携带合法 API Key：

- **Header**：`Authorization: Bearer <你的 API Key>`
- 未配置 `.api-key` 时，无需认证。

**示例（curl）：**

```bash
# 无认证时
curl http://localhost:8888/v1/models

# 有 .api-key 时
curl -H "Authorization: Bearer YOUR_API_KEY" http://localhost:8888/v1/models
```

---

## 二、两套接口概览

| 类型 | 路径前缀 | 说明 |
|------|----------|------|
| **OpenAI 兼容** | `/v1/*` | 推荐：通过请求体 `model` 字段自动路由到对应后端 |
| **代理路由** | `/api/*` | 通过 URL 路径指定模型或使用默认后端 |
| **知识库反代** | `/knowledge/` | 浏览器知识库 UI 与 `/knowledge/api/v1/*`；转发到 `KNOWLEDGE_COLLECTOR_URL`（默认 `http://127.0.0.1:18789/plugins/js-knowledge`），不是模型 API |

---

## 三、OpenAI 兼容接口（推荐）

基址：`http://localhost:8888/v1`，请求格式与 OpenAI API 一致，便于使用官方 SDK 或兼容库。

### 3.1 模型列表

```http
GET /v1/models
```

**响应示例：**

```json
{
  "object": "list",
  "data": [
    { "id": "glm-5", "object": "model", "created": 1700000000, "owned_by": "local" },
    { "id": "qwen3.5", "object": "model", "created": 1700000000, "owned_by": "local" }
  ]
}
```

- `id` 可为「运行名」或「模型别名」，均可在下文 `model` 字段中使用。

**与本仓库 `models.json` 的对应关系（避免与其它 Qwen3.5 规模混淆）：**

| 列表中的 `id` / 请求里常用 `model` | 完整型号 | 权重仓库（GGUF） |
|-----------------------------------|----------|------------------|
| `qwen3.5` | **Qwen3.5-397B-A17B** | `unsloth/Qwen3.5-397B-A17B-GGUF` |
| `glm-5` | GLM-5 | `unsloth/GLM-5-GGUF` |
| `minimax` | MiniMax-M2.5 | `unsloth/MiniMax-M2.5-GGUF` |

同一后端还可接受 `llama-server --alias` 所设别名（例如 Qwen 条目为 `unsloth/Qwen3.5-397B-A17B`），具体以运行中实例为准。

### 3.2 对话补全（Chat Completions）

```http
POST /v1/chat/completions
Content-Type: application/json
```

**请求体**：标准 OpenAI 格式，**必须包含 `model`**，用于路由到对应后端。

**非流式示例：**

```json
{
  "model": "glm-5",
  "messages": [
    { "role": "user", "content": "你好" }
  ]
}
```

**流式示例：**

```json
{
  "model": "qwen3.5",
  "messages": [
    { "role": "user", "content": "写一首短诗" }
  ],
  "stream": true
}
```

其中 `model: "qwen3.5"` 表示本仓库注册的 **Qwen3.5-397B-A17B**（见上文 §3.1 对照表）。

**路由规则**：  
优先按 `model` 匹配运行中后端的「模型别名」或「运行名」，未匹配则使用当前默认（第一个运行中的）后端。若无任何运行中模型，返回 `503 No running models`。

**curl 示例：**

```bash
curl -X POST http://localhost:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5","messages":[{"role":"user","content":"你好"}]}'
```

### 3.3 文本补全（Completions）

```http
POST /v1/completions
Content-Type: application/json
```

请求体格式同 OpenAI，需包含 `model` 字段，路由规则同 3.2。

### 3.4 嵌入（Embeddings）

```http
POST /v1/embeddings
Content-Type: application/json
```

请求体需包含 `model`，会按 `model` 路由到对应 embedding 后端。若无运行中模型，返回 `503 No running embedding models`。

---

## 四、代理路由接口（/api/*）

通过 URL 显式指定后端，或查询运行状态。

### 4.1 运行中模型列表（含队列状态）

```http
GET /api/models
```

**响应示例：**

```json
[
  {
    "name": "glm-5",
    "model": "glm-5",
    "port": 8001,
    "pid": 12345,
    "queue": 0
  },
  {
    "name": "qwen3.5",
    "model": "qwen3.5",
    "port": 8002,
    "pid": 12346,
    "queue": 1
  }
]
```

- `name`：运行名（用于 URL 路由）
- `model`：模型/别名（用于 `/v1/*` 的 `model` 字段）
- `port`：该模型后端端口
- `queue`：当前该模型推理排队数量

**无需认证**，且不经过 `/v1/*` 的 Bearer 校验。

### 4.2 按模型名代理

```http
POST /api/<model-name>/v1/chat/completions
GET  /api/<model-name>/v1/models
...
```

- `<model-name>` 为 `GET /api/models` 返回的 `name`。
- 请求会原样转发到该模型对应的 `http://127.0.0.1:<port>/...`。

**示例：**

```bash
curl -X POST http://localhost:8888/api/glm-5/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}],"stream":false}'
```

注意：走 `/api/<model-name>/*` 时，请求体里的 `model` 可选（路由已由 URL 决定）。

### 4.3 默认后端代理

```http
POST /api/v1/chat/completions
GET  /api/health
...
```

- 未在路径中指定模型名时，请求转发到「当前默认后端」（第一个运行中的模型）。
- 若无运行中模型，则转发到环境变量 `LLAMA_PORT` 指定的端口（默认 8001）。

---

## 五、推理队列与限流

仅对走 **serve-ui `:8888`** 的请求生效。直连 Ollama `:11434` 或 ds4 `:8005` 没有这层保护。

- **对话快车道**：所有对话模型（Qwen / ds4 / llama 等）**合计同时只跑 1 路 decode**，避免统一内存带宽被打满、所有人一起变慢。第二路进队，而不是并行抢带宽。
- **流式优先**：排队时流式对话优先于非流式，避免交互被批量请求堵住。
- **embed / ASR 分门**：embeddings 与 transcriptions 走独立辅门，不占对话槽。
- **排队**：对话占槽时，新对话请求进入队列：
  - **流式请求**：会定期收到 SSE `: keepalive`，避免连接超时。
  - **非流式请求**：阻塞等待，直到轮到自己或超时。
- **队列满**：若该模型「在跑+在等」达到上限，返回 **429**，并带 `Retry-After: 30`，客户端应稍后重试。
- **排队超时**：非流式请求在队列中等待过久会返回 **504**。

**环境变量（可选）：**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CHAT_LANE_CONCURRENT` | 1 | 对话快车道同时放行路数（跨模型共享） |
| `MAX_GLOBAL_CONCURRENT` | （未设置） | 旧变量；仅当未设 `CHAT_LANE_CONCURRENT` 时作为对话车道覆盖 |
| `EMBED_LANE_CONCURRENT` | 2 | embed 辅门同时放行路数 |
| `ASR_LANE_CONCURRENT` | 1 | ASR 辅门同时放行路数 |
| `MAX_QUEUE_DEPTH` | 12 | 单模型（对话）或单辅门最大排队数量 |
| `QUEUE_KEEPALIVE_SEC` | 5 | 流式排队时 SSE keepalive 间隔（秒） |
| `API_PROXY_TIMEOUT` | 3600 | 转发到后端的超时时间（秒） |

---

## 六、错误码与响应

| HTTP 状态 | 含义 |
|-----------|------|
| 401 | 未提供或无效的 API Key（仅针对 `/v1/*`，且已配置 `.api-key`） |
| 429 | 推理队列已满，需配合 `Retry-After` 重试 |
| 502 | 转发到后端失败（如后端未启动、连接错误） |
| 503 | 无运行中的模型（/v1 路由时）或无 embedding 模型 |
| 504 | 排队等待超时或后端处理超时 |

错误响应体一般为 JSON，例如：

```json
{
  "error": {
    "message": "推理队列已满，请稍后重试",
    "type": "server_error"
  }
}
```

---

## 七、使用 OpenAI 官方 SDK 的示例

将 `base_url` 指向本服务，并视情况设置 `api_key` 即可：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8888/v1",
    api_key="YOUR_API_KEY"  # 无 .api-key 时可填任意非空或省略
)

# 列出模型
models = client.models.list()

# 对话（自动按 model 路由）
r = client.chat.completions.create(
    model="glm-5",
    messages=[{"role": "user", "content": "你好"}],
    stream=False
)
print(r.choices[0].message.content)
```

---

## 八、环境变量汇总

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `UI_PORT` | 8888 | serve-ui 监听端口 |
| `LLAMA_PORT` | 8001 | 无运行中模型时，/api/* 默认转发端口 |
| `API_PROXY_TIMEOUT` | 3600 | 代理请求超时（秒） |
| `MONITOR_PROXY_TIMEOUT` | 8 | health/metrics/slots 等监控接口超时（秒） |
| `CHAT_LANE_CONCURRENT` | 1 | 对话快车道同时放行路数 |
| `MAX_GLOBAL_CONCURRENT` | （未设置） | 旧变量，可覆盖对话车道 |
| `EMBED_LANE_CONCURRENT` | 2 | embed 辅门并发 |
| `ASR_LANE_CONCURRENT` | 1 | ASR 辅门并发 |
| `MAX_QUEUE_DEPTH` | 12 | 单模型 / 辅门最大排队数 |
| `QUEUE_KEEPALIVE_SEC` | 5 | 流式排队 SSE keepalive 间隔（秒） |

---

## 九、快速对照

- **只关心「用哪个模型」**：用 **OpenAI 兼容** `POST /v1/chat/completions`，在 body 里写 `"model": "模型名或别名"`。
- **需要查「当前跑了哪些模型、排队情况」**：用 **GET /api/models**。
- **想固定走某台后端**：用 **/api/<model-name>/** 代理。
- **配置了 .api-key**：所有 **/v1/** 请求记得加 **Authorization: Bearer <key>**。
