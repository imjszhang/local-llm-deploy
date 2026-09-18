# API 与认证

统一网关默认 `http://localhost:8888`，`UI_PORT` 可覆盖。API 以模型注册表与实际后端能力为准。

## 认证边界

配置 `.api-key` 后，以下入口统一要求 `Authorization: Bearer <Key>`：

- `/v1/*`。
- `/api/<模型键>/*`，包括模型详情代理。
- 默认后端 `/api/*` 转发。
- `/api/ollama/*` 原生推理及操作代理。
- `/monitor-api/v1/*` 完整监控快照和模型详情。
- `/services/<key>/*` 外部 HTTP 服务反代。

仅 `GET/HEAD /api/models` 和 `/api/system` 作为明确的只读监控例外。监控页面支持在当前页面内存输入 Key；Key 不写入静态资源、URL 或浏览器持久存储。

`/knowledge/` 保持独立凭据语义：转发知识库客户端 Authorization，不用模型 Key 覆盖。

`/services/<key>/` 使用模型 API Key；默认不转发 `Authorization`（`auth.upstream=none`），控制台 session 不能当业务凭证。`Upgrade: websocket` 返回 501。未探活返回 503，未知键或未允许路径返回 404。这些键不出现在 `GET /v1/models`。

网关入口 Key 与后端 Key 可以不同。转发和健康探测按“运行实例记录的 `api_key_file` → 模型 `runtime.api_key_file` → 项目 `.api-key`”读取后端凭据；PID 只记录文件引用和来源。通过 `--api-key` / `API_KEY` 启动的内联密钥不能由另一个网关进程可靠恢复，直连仍可使用，代理会返回不可用诊断；需要代理时改用 `--api-key-file` / `API_KEY_FILE`，或配置 `runtime.api_key_file` 后重新启动。缺失凭据文件也会显示在监控 `unavailable_backends` 中，不向客户端暴露密钥。

## 接口

| 路径 | 用途 | 模型能力 |
| --- | --- | --- |
| `GET /v1/models` | 当前可路由模型键与别名 | 所有在线模型 |
| `POST /v1/chat/completions` | Chat JSON / SSE | chat |
| `POST /v1/completions` | 文本补全 | chat |
| `POST /v1/embeddings` | 文本向量 | embedding |
| `POST /v1/rerank` | 文档重排序，Jina 格式 | rerank |
| `POST /v1/audio/transcriptions` | multipart 音频转写 | asr |
| `/api/<模型键>/*` | 按路径选择已注册后端 | 依端点校验 |
| `/api/ollama/*` | Ollama 原生接口 | 原生推理也经过调度 |
| `/knowledge/*` | 外部知识库反代 | 不属于模型 API |
| `/services/<key>/*` | 登记为 `type: proxy` 的外部 HTTP 反代 | 不属于模型 API，不占 lane |

Responses / Messages 仅在模型 `endpoints` 显式包含 `/v1/responses`、`/v1/messages` 时开放。网关提供透传与相应流式错误封装，实际模型后端必须支持对应协议。

模型代理对路径解码一次，拒绝畸形/残余编码、点段和歧义分隔符。llama 原生 `/completion`、`/infill`、`/embedding`、`/reranking`、`/v1/reranking` 按对应标准能力和端点声明校验，并经过相同调度器。仅明确的元数据读取、tokenize/detokenize/apply-template 和 Ollama 管理路径允许无推理调度透传；未知代理端点返回 404，错误方法返回 405。

## 监控 API v1

| 接口（GET/HEAD） | 数据 |
| --- | --- |
| `/monitor-api/v1/snapshot` | 系统资源、全部注册及发现模型、可选 `services` 外部服务、网关通道和诊断 |
| `/monitor-api/v1/models/<编码后的模型键>` | 健康、指标、槽位、已确认归属的进程资源和 Ollama 信息 |
| 同上，`?include_output=1` | 显式读取源端已有的有界调试输出；默认不包含正文 |

其他方法返回 405，未知监控路径或模型返回 JSON 404，错误参数或有请求体返回 400。新路径先经过认证和专用路由，再处理静态文件；不会误落到目录服务。原公开 `/api/models`、`/api/system` 保持原契约，只用于有限公开摘要。

完整字段见 [TypeScript DTO](../frontend/src/features/monitor/api/types.ts)。时间戳为 Unix 毫秒；缺失值为 `null`，来源包含最后尝试/成功、过期阈值及错误。第一次请求可能触发异步采集并返回 `loading` 分区或空系统读数，客户端应按来源信息继续轮询。采集失败保留已知值并显示过期，不将失败替换为零。

模型的生命周期、可用性、可路由性、活动请求分别表达。未加载的 Ollama 模型可仍然可路由；缺失 PID 不等于确认停止；进程 RSS 是独立指标；预留 token 预算为网关估算。各能力通道的 `active` 和 `waiting` 分开计数，`queue_depth` 包含活动与等待。`uncertain` 占用仍计入活动，且不会由监控读取解除。

接口不返回环境变量、完整启动命令或凭据路径。元数据探测与输出大小有界；监控不写日志正文，不发推理请求。macOS 系统采集不可用时明确返回缺失/错误状态，不伪造其他平台指标。

## 模型选择与错误

请求 `model` 可匹配注册键、alias、后端模型 ID。Ollama tag 与显式 `backend_model` 在发送上游前正确转换。

- 显式未知名称：404，不回退到其他模型。
- 已知模型不可路由：503。
- 缺少 model 且没有明确默认模型：400。
- 能力或端点不匹配：明确的客户端错误。
- 无有效 Key：401。
- 队列已满：429，并包含 `Retry-After: 30`。
- 排队超时：504；已经发送 SSE 头时用流内错误表示。

默认模型通过 `default_for: ["chat"]` 等注册字段设置，或使用 `DEFAULT_CHAT_MODEL`、`DEFAULT_EMBEDDING_MODEL`、`DEFAULT_RERANK_MODEL`、`DEFAULT_ASR_MODEL`；环境设置优先。

```bash
curl http://localhost:8888/v1/embeddings \
  -H 'Authorization: Bearer <Key>' -H 'Content-Type: application/json' \
  -d '{"model":"jina-embed","input":["文本1","文本2"],"task":"retrieval.query","dimensions":256}'

curl http://localhost:8888/v1/rerank \
  -H 'Authorization: Bearer <Key>' -H 'Content-Type: application/json' \
  -d '{"model":"jina-rerank-mlx","query":"问题","documents":["段落1","段落2"],"top_n":1}'

curl http://localhost:8888/v1/audio/transcriptions \
  -H 'Authorization: Bearer <Key>' \
  -F file=@short.wav -F model=whisper-large-v3 -F language=zh -F response_format=json
```

Whisper 支持 `text`、`json`、`verbose_json`。文件作为二进制 multipart 传递；音频临时文件在成功和失败时都清理。

## 调度、流式与日志

Chat、Embedding、Rerank、ASR 使用独立 lane。Chat 还受每模型并发与估算 KV 预算限制。默认 lane 为 1/2/1/1；`MAX_QUEUE_DEPTH=12` 保留“活动+等待”的计数语义。

主要设置：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `API_PROXY_TIMEOUT` | 3600 秒 | 上游请求 |
| `QUEUE_TIMEOUT` | 同 API_PROXY_TIMEOUT | 排队总时限，含流式 |
| `QUEUE_KEEPALIVE_SEC` | 5 秒 | SSE 排队/首包等待保活 |
| `MAX_REQUEST_BODY_BYTES` | 64 MiB | 上传/JSON 大小上限 |
| `STREAM_BUFFER_CHUNKS` | 16 | 上游数据队列块数，每块最多 8192 字节 |
| `CANCEL_GRACE_SEC` | 10 秒 | 断开后的清理观察窗口 |
| `ACCESS_LOG_CAPTURE_BYTES` | 65536 | 可选请求/响应捕获上限 |

SSE 发送头前可返回 HTTP 错误；发送保活头后按对应协议返回流式错误。客户端断开时不能立即假定模型已停止；不确定实例会在监控中标明，恢复顺序见 [架构](architecture.md)。

非流式及 Ollama 等待首包时也检查连接重置。TCP 的 FIN 可能只是客户端关闭发送方向但仍等待响应，因此不能仅凭 FIN 取消合法请求；明确的 RST、写失败或请求超时才进入放弃与清理流程，未确认上游结束前不会提前释放占用。

`SERVE_UI_ACCESS_LOG` 启用 JSONL 请求元数据；仅 `SERVE_UI_LOG_BODY=1` 开启有界的请求/响应内容记录，ASR 音频不记录。日志不记录 Authorization。

### 本机控制台会话

静态工作台在直连网关的 loopback 地址上通过 `POST /console-api/v1/session` 自动授权，返回 `{token, expires_at}`（Unix 毫秒）。这不是根 `.api-key` 的读取接口，根 Key 始终留在服务端。响应为 `no-store`，内存会话有效 8 小时、最多保留 64 个，绑定签发 Origin 与当前根 Key。

签发要求空请求体、`X-Local-Console: 1`、精确同源 Origin、loopback socket、loopback Host 与实际监听端口；拒绝 Forwarded、X-Forwarded-*、X-Real-IP 和跨站 Fetch Metadata。令牌使用时仍验证这些边界；GET/HEAD 可以无 Origin，但必须有 `Sec-Fetch-Site: same-origin`。

令牌仅授权监控 API v1 的快照/模型详情 GET/HEAD、`/v1/chat/completions` POST，以及历史 API 的会话目录/详情 GET、单会话 PUT/DELETE。其他 API 和知识库不接受此令牌；局域网、代理、外部 API 客户端继续使用原 API Key。根 Key 变更或网关重启会撤销已有本机会话。

此检查拒绝可识别的转发请求；能够完全重写 Host/Origin 并删除转发元信息的本机代理无法仅靠 HTTP 元数据识别。本机进程已属于本部署的信任边界。

## 会话历史 API

`/chat-api/v1/sessions` 提供本机 SQLite 会话目录、读取、版本化写入和删除，见 [会话存储接口](chat-history.md#接口与维护)。历史 API 需要根 Key 精确匹配或本机控制台临时凭据，即使未配置根 Key 也不匿名开放。临时凭据仅新增这些限定 GET/PUT/DELETE 权限，不获得模型管理或其他代理权限。
