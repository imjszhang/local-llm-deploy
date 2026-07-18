# Local LLM Deploy 系统架构说明

> 文档更新时间：2026-03-01

---

## 一、系统概览

Local LLM Deploy 采用**多实例部署架构**：聊天模型每个运行一个独立的 llama-server 进程，Embedding 模型由 `serve_embedding.py` 提供，ASR 模型由 `serve_whisper.py`（mlx-whisper）提供，通过 `manage.sh` 统一管理，`serve-ui.py` 前端代理自动路由到各后端，并支持 OpenAI 兼容 `/v1/*` 与推理请求队列。

```
┌─────────────────────────────────────────────────────────────────┐
│                    用户 / OpenAI 客户端                            │
│                                                                  │
│  http://localhost:8001/v1 (GLM-5)   http://localhost:8002/v1 (Qwen3.5-397B-A17B)  │
│  http://localhost:8003/v1 (MiniMax) http://localhost:8004/v1 (jina-embed) │
│  http://localhost:8007/v1 (whisper) http://localhost:8888/ (前端 + 统一代理) │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                      宿主机推理服务                                │
│                                                                  │
│  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐    │
│  │ llama-server    │ │ llama-server    │ │ llama-server    │    │
│  │ (GLM-5) :8001   │ │ (Qwen3.5-397B-A17B) :8002 │ │ (MiniMax) :8003 │    │
│  │ run/glm-5.pid   │ │ run/qwen3.5.pid │ │ run/minimax.pid │    │
│  └─────────────────┘ └─────────────────┘ └─────────────────┘    │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ serve_embedding.py (jina-embeddings-v5) :8004  run/jina-embed.pid │
│  └─────────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ serve_whisper.py (whisper-large-v3 MLX) :8007  run/whisper-large-v3.pid │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │  serve-ui.py (前端 + API 代理)  端口: 8888                    │ │
│  │  /v1/models, /v1/chat/completions, /v1/embeddings, /v1/audio/transcriptions (OpenAI 兼容，按 model 路由) │
│  │  /api/models → 运行中模型列表（含队列状态）                    │ │
│  │  /api/<model>/* → 路由到对应后端  推理队列：按模型串行，可选 .api-key │
│  └──────────────────────────────────────────────────────────────┘ │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │  models.json (模型注册中心)   manage.sh (list/download/start/stop/status/logs) │
│  └──────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

---

## 二、服务组件

| 组件 | 端口 | 说明 |
|------|------|------|
| **llama-server** (每聊天模型一个) | 由 models.json 的 default_port 配置 | C++ 推理引擎，OpenAI 兼容 API，加载 GGUF |
| **serve_embedding.py** | 默认 8004 | Embedding 服务（如 jina-embeddings-v5），OpenAI 兼容 /v1/embeddings |
| **serve_whisper.py** | 默认 8007 | ASR 服务（mlx-whisper），OpenAI 兼容 /v1/audio/transcriptions |
| **serve-ui.py** | 8888 (UI_PORT) | 前端静态服务 + 多模型 API 代理 + 推理队列；支持 /v1/* 与 /api/* |
| **manage.sh** | - | 统一 CLI：list / download / start / stop / status / logs |

---

## 三、数据流

```
OpenAI Client / curl → serve-ui(:8888)/v1/* 或 直连 llama-server(:800x)
                              ↓
               serve-ui 按请求体 model 或 URL /api/<model>/* 路由
                              ↓
               llama-server / serve_embedding / serve_whisper → 模型文件 (GGUF / safetensors / MLX)
                              ↓
OpenAI Client / curl ← JSON 或 SSE 流 ←

浏览器 → serve-ui(:8888) → 静态页 (monitor.html, chat.html 等)
                          → /api/models → 运行中模型列表与队列状态
                          → /v1/chat/completions 等 → 后端（推理队列按模型串行）
```

---

## 四、模型管理

### models.json 结构

- **聊天模型**：`repo_id`、`repo_name`（可选）、`full_model_name`（可选，人类可读完整型号，供 `manage.sh list/status` 展示）、`quants`（量化及 size_gb）、`alias`（llama-server --alias）、`default_port`、`params`（temp、top_p、ctx_size、n_predict、extra_args 等）；可选 `chat_template_file`、`mmproj` 等。
- **Embedding 模型**：`"type": "embedding"`、`repo_id`、`alias`、`default_port`、`params`（如 dimensions、default_task）；无 `quants`，由 `serve_embedding.py` 加载 safetensors。
- **ASR 模型**：`"type": "asr"`、`repo_id`（MLX Community Whisper）、`alias`、`default_port`、`params`（language、task、response_format）；由 `serve_whisper.py` + mlx-whisper 加载。

### 进程管理

| 文件 | 用途 |
|------|------|
| `run/<model>.pid` | PID 文件：第一行 PID，第二行端口，第三行模型/别名（供 serve-ui 路由与展示） |
| `logs/<model>.log` | 独立日志文件 |

---

## 五、已注册模型

| 模型 | 类型 | 端口 | 量化/说明 | 大小 |
|------|------|------|-----------|------|
| glm-5 | chat | 8001 | UD-IQ2_XXS, UD-TQ1_0 | 241GB / 176GB |
| qwen3.5 | chat | 8002 | **完整型号：Qwen3.5-397B-A17B**（MoE；`repo_id`：`unsloth/Qwen3.5-397B-A17B-GGUF`）。量化：MXFP4_MOE, UD-Q4_K_XL, UD-Q2_K_XL；含 chat_template、mmproj | 214GB / 214GB / 120GB |
| minimax | chat | 8003 | BF16 | 457GB |
| jina-embed | embedding | 8004 | jina-embeddings-v5-text-small (safetensors) | - |
| whisper-large-v3 | asr | 8007 | mlx-community/whisper-large-v3-mlx | ~3GB |

---

## 六、关键文件与文档

| 文件 | 说明 |
|------|------|
| `models.json` | 本地模型注册中心（默认 gitignore） |
| `models.json.example` | 注册表示例模板 |
| `registry_cli.py` | `./manage.sh registry *` 实现 |
| `manage.sh` | 统一 CLI 入口 |
| `deploy.sh` | 聊天模型部署（llama-server） |
| `download.sh` / `download_model.py` | 统一模型下载（GGUF 对话模型与 embedding） |
| `model_paths.py` | 本地权重路径推导与检测（与 `manage.sh` / `model_inventory` 一致） |
| `model_inventory.py` | `manage.sh models` / `remove` / `register`；`models/.manifest.json` |
| `serve-ui.py` | 前端 + 多模型代理 + 推理队列；/v1/* 与 /api/* |
| `serve_embedding.py` | Embedding 服务（jina） |
| `serve_whisper.py` | ASR 服务（mlx-whisper） |
| `requirements-whisper.txt` | ASR 专用 Python 依赖 |
| `.api-key` | 可选；存在时 /v1/* 需 Bearer 认证 |
| `docs/whisper-guide.md` | Whisper ASR 使用指南 |
| `docs/api-guide.md` | 外部调用 serve-ui 接口的使用指南 |

---

## 七、常用运维命令

```bash
# 模型列表与状态
./manage.sh registry init    # 首次克隆后生成 models.json
./manage.sh registry list
./manage.sh list
./manage.sh models
./manage.sh status

# 下载与启动（聊天模型 / embedding 均通过 manage.sh）
./manage.sh download glm-5
./manage.sh start glm-5
./manage.sh start qwen3.5   # 配置键 qwen3.5 → Qwen3.5-397B-A17B
./manage.sh start minimax
./manage.sh start jina-embed
./manage.sh start whisper-large-v3

# 停止
./manage.sh stop glm-5
./manage.sh stop --all

# 日志
./manage.sh logs glm-5

# 监控（直连后端）
./monitor.sh health --model glm-5
./monitor.sh metrics --model qwen3.5   # Qwen3.5-397B-A17B

# 前端与统一 API（代理 + 推理队列）
./serve-ui.sh
# 访问 http://localhost:8888/ → monitor.html；API 见 docs/api-guide.md
```
