# Jina Embedding 模型使用指南

本文档介绍本项目中 **jina-embeddings-v5-text-small** 的配置、下载、启动与调用方式。该模型用于文本向量化（Embedding），提供 OpenAI 兼容的 `/v1/embeddings` 接口，可与对话模型配合做 RAG、检索、语义匹配等。

---

## 一、在项目中的角色

| 项目内名称 | 模型 ID（API 用） | 类型 | 默认端口 | 格式 | 磁盘占用 |
|------------|------------------|------|----------|------|----------|
| `jina-embed` | `jina-embeddings-v5-text-small` | embedding | 8004 | safetensors | ~1.4GB |

- **与对话模型区别**：对话模型（如 qwen3.5、glm-5）使用 llama.cpp + GGUF；Jina 使用 **transformers** 加载 **safetensors**，由独立服务 `serve_embedding.py` 提供 HTTP 接口。
- **路由**：前端代理 `serve-ui.py`（端口 8888）会根据请求体中的 `model` 字段，将 `/v1/embeddings` 转发到 Jina 后端（8004）。

---

## 二、配置（models.json）

在 `models.json` 中，Jina 以 `type: "embedding"` 与对话模型区分：

```json
"jina-embed": {
  "type": "embedding",
  "repo_id": "jinaai/jina-embeddings-v5-text-small",
  "repo_name": "jinaai-jina-embeddings-v5-text-small",
  "alias": "jina-embeddings-v5-text-small",
  "default_port": 8004,
  "params": {
    "dimensions": 1024,
    "default_task": "text-matching"
  }
}
```

- **repo_id**：HuggingFace/ModelScope 仓库 ID，下载脚本使用。
- **repo_name**：本地目录名，模型会下载到 `models/<repo_name>/`。
- **alias**：API 请求里使用的 `model` 名，需与返回给客户端的名称一致。
- **default_port**：`manage.sh start jina-embed` 使用的端口。
- **params**：可选默认参数（如 `dimensions`、`default_task`），实际以请求体为准。

---

## 三、下载模型

Jina 为 **safetensors** 格式，通过 ModelScope 魔搭下载，**不走** GGUF 的 `download.sh`。

### 方式一：manage.sh（推荐）

```bash
./manage.sh download jina-embed
```

内部会调用 `download_jina_embeddings.py`。

### 方式二：直接运行 Python 脚本

```bash
python3 download_jina_embeddings.py
```

- 会按需安装 `modelscope`。
- 下载到：`models/jinaai-jina-embeddings-v5-text-small/`，约 1.4GB。
- 完成后可用 `./manage.sh list` 查看「已下载」状态。

---

## 四、启动服务

### 4.1 基本启动

```bash
./manage.sh start jina-embed
```

- 使用 `serve_embedding.py`，默认监听 **127.0.0.1:8004**。
- 若存在 `.venv-embed/bin/python` 则优先使用，否则用 `.venv/bin/python` 或系统 `python3`。
- 日志：`logs/jina-embed.log`。

### 4.2 可选参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--port` | 端口 | `./manage.sh start jina-embed --port 8005` |
| `--host` | 监听地址 | 默认 `127.0.0.1` |
| `--lan` | 局域网可访问 | 使用 `0.0.0.0`（需在 manage.sh 的 start 分支支持，当前脚本支持 `--host`） |

当前 `manage.sh` 中 embedding 启动逻辑支持 `--port`、`--host`、`--lan`（`--lan` 会设 host 为 `0.0.0.0`）。

### 4.3 手动启动（调试用）

```bash
# 使用项目虚拟环境
.venv/bin/python serve_embedding.py --model-name jina-embed --port 8004 --host 127.0.0.1

# 指定模型目录
.venv/bin/python serve_embedding.py --model-dir ./models/jinaai-jina-embeddings-v5-text-small --port 8004
```

- 未传 `--model-dir` 时，会根据 `--model-name` 从 `models.json` 解析出 `repo_name`，得到 `models/<repo_name>`。
- 模型目录不存在会报错并提示先执行下载。

### 4.4 停止与状态

```bash
./manage.sh stop jina-embed    # 停止
./manage.sh status             # 查看运行中实例（含 jina-embed 的 PID、端口、内存等）
./manage.sh logs jina-embed    # 查看日志
```

---

## 五、服务接口说明

### 5.1 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/embeddings` | 文本向量化（OpenAI 兼容） |
| GET | `/health` | 健康检查 |

### 5.2 请求体（POST /v1/embeddings）

与 OpenAI Embedding API 兼容，并增加 Jina 扩展参数：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | 推荐 | 使用 `jina-embeddings-v5-text-small`，便于代理路由 |
| `input` | string / array of strings | 是 | 单条文本或文本列表，批量会按 BATCH_SIZE=32 分批 |
| `task` | string | 否 | LoRA 任务类型，见下表，默认 `text-matching` |
| `dimensions` | int | 否 | Matryoshka 维度截断，32～1024，不传则用模型最大维度 |
| `prompt_name` | string | 否 | 检索场景用：`query` 表示查询、否则为文档 |

**task 取值与别名**（在 `serve_embedding.py` 的 `TASK_ALIASES` 中）：

| 请求 task | 实际 adapter | 典型用途 |
|-----------|--------------|----------|
| `text-matching` | text-matching | 默认，通用文本匹配 |
| `retrieval`、`retrieval.query`、`retrieval.passage` | retrieval | 检索（query/passage） |
| `classification` | classification | 分类 |
| `clustering` | clustering | 聚类 |

- 检索时：`task: "retrieval.query"` 或 `prompt_name: "query"` 会为输入加 `"Query: "` 前缀；否则加 `"Document: "`。

### 5.3 响应格式（OpenAI 兼容）

```json
{
  "object": "list",
  "model": "jina-embeddings-v5-text-small",
  "data": [
    { "object": "embedding", "index": 0, "embedding": [ ... ] },
    { "object": "embedding", "index": 1, "embedding": [ ... ] }
  ],
  "usage": {
    "prompt_tokens": 123,
    "total_tokens": 123
  }
}
```

### 5.4 认证

- 若项目根目录存在 `.api-key` 文件，服务会读取第一行作为 API Key。
- 请求需带：`Authorization: Bearer <你的API-Key>`，否则返回 401。

---

## 六、通过 serve-ui 代理访问（推荐）

前端与多模型代理 `serve-ui.py`（默认端口 **8888**）会：

1. 根据请求路径识别为 embedding：`/v1/embeddings` 或 `/embeddings`。
2. 从请求体 JSON 的 `model` 字段解析目标：
   - 先按运行中实例的 **alias**（即 `jina-embeddings-v5-text-small`）匹配；
   - 再按内部名称 `jina-embed` 匹配；
   - 若未指定或未匹配，则用当前唯一的 embedding 实例（若只有一个即为 Jina）。
3. 将请求转发到对应后端的 `/v1/embeddings`（如 `http://127.0.0.1:8004/v1/embeddings`）。

因此**推荐**统一访问代理，便于与对话模型共用同一 base URL 和认证：

- 代理地址：`http://localhost:8888/v1/embeddings`
- 请求体中的 `model` 填：`jina-embeddings-v5-text-small`

---

## 七、使用示例

### 7.1 curl（直连后端 8004）

```bash
curl -X POST http://127.0.0.1:8004/v1/embeddings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <你的API-Key>" \
  -d '{
    "model": "jina-embeddings-v5-text-small",
    "input": ["第一段文本", "第二段文本"],
    "task": "text-matching",
    "dimensions": 1024
  }'
```

### 7.2 curl（经 serve-ui 代理 8888）

```bash
curl -X POST http://localhost:8888/v1/embeddings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <你的API-Key>" \
  -d '{"model":"jina-embeddings-v5-text-small","input":["文本1","文本2"]}'
```

### 7.3 检索场景（query + document）

```bash
# 查询向量（query）
curl -X POST http://localhost:8888/v1/embeddings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <你的API-Key>" \
  -d '{"model":"jina-embeddings-v5-text-small","input":"用户问题","task":"retrieval.query"}'

# 文档向量（document）
curl -X POST http://localhost:8888/v1/embeddings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <你的API-Key>" \
  -d '{"model":"jina-embeddings-v5-text-small","input":["文档1","文档2"],"task":"retrieval.passage"}'
```

### 7.4 Python（OpenAI SDK）

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8888/v1",
    api_key="<你的API-Key>",
)

resp = client.embeddings.create(
    model="jina-embeddings-v5-text-small",
    input=["文本1", "文本2"],
    extra_body={"task": "text-matching", "dimensions": 512},  # 部分 SDK 支持 extra_body
)
for item in resp.data:
    print(item.embedding[:5])  # 前 5 维
```

若 SDK 不支持 `extra_body`，可直连 8004：`base_url="http://127.0.0.1:8004/v1"`，并在服务端支持的情况下通过请求体传 `task`、`dimensions`。

---

## 八、实现要点（供开发参考）

- **加载**：`serve_embedding.py` 使用 `transformers` 的 `AutoModel` / `AutoTokenizer`，支持 MPS（Apple Silicon）、CUDA、CPU。
- **多任务**：模型内置多 LoRA adapter，通过 `set_adapter(task)` 切换 `task`（如 text-matching、retrieval、classification、clustering）。
- **维度**：支持 Matryoshka 式维度截断（`dimensions` 32～1024），在 `encode()` 内对 `pooled` 做切片后 L2 归一化。
- **并发**：`EmbeddingModel.encode` 用线程锁串行化推理；HTTP 端使用 `ThreadingMixIn` 多线程处理请求。
- **PID 文件**：与对话模型一致，在 `run/jina-embed.pid` 中写入 PID、端口、alias，供 `manage.sh status` 和 `serve-ui` 发现并路由。

---

## 九、常见问题

1. **未找到模型目录**  
   先执行：`./manage.sh download jina-embed` 或 `python3 download_jina_embeddings.py`。

2. **401 Unauthorized**  
   在项目根目录配置 `.api-key`，或去掉该文件关闭认证；请求头带 `Authorization: Bearer <key>`。

3. **503 No running embedding models**  
   通过代理 8888 调用时，需至少有一个 embedding 实例在跑；先 `./manage.sh start jina-embed`，再 `./manage.sh status` 确认。

4. **Unknown task**  
   `task` 必须是模型支持的任务之一（如 text-matching、retrieval、classification、clustering），或使用文档中的别名（如 retrieval.query）。

5. **依赖**  
   `serve_embedding.py` 依赖 `torch`、`transformers`；若主项目 `requirements.txt` 未包含，需在 `.venv` 或 `.venv-embed` 中单独安装。

---

## 十、相关文件速查

| 文件 | 作用 |
|------|------|
| `models.json` | 注册 `jina-embed`，类型 embedding、端口 8004、alias |
| `download_jina_embeddings.py` | 从 ModelScope 下载 safetensors 到 `models/jinaai-jina-embeddings-v5-text-small` |
| `serve_embedding.py` | 加载 Jina 模型，提供 `/v1/embeddings` 与 `/health` |
| `manage.sh` | `download` / `start` / `stop` / `status` / `logs` 统一入口 |
| `serve-ui.py` | 多模型代理，将 `/v1/embeddings` 按 `model` 转发到 Jina（8004） |
| `run/jina-embed.pid` | 运行时的 PID、端口、alias |
| `logs/jina-embed.log` | 服务日志 |
