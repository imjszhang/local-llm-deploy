# API 与认证

统一网关默认 `http://localhost:8888`，`UI_PORT` 可覆盖。API 以模型注册表与实际后端能力为准。

## 认证边界

配置 `.api-key` 后，以下入口统一要求 `Authorization: Bearer <Key>`：

- `/v1/*`。
- `/api/<模型键>/*`，包括模型详情代理。
- 默认后端 `/api/*` 转发。
- `/api/ollama/*` 原生推理及操作代理。
- `/monitor-api/v1/*` 完整监控快照和模型详情。
- `/agents-api/v1/snapshot` 本机探员会话快照。
- `/services/<key>/*` 外部 HTTP 服务反代。

仅 `GET/HEAD /api/models`、`/api/system` 和 `/api/guide` 作为明确的只读公开例外。`/api/guide` 说明当前应用、外部服务、MCP 服务和模型接口；浏览器打开是页面，请求 `application/json` 时返回同一份内容。它公开 MCP Hub 的客户端连接地址，但不包含上游地址、上游端口、进程、启动参数或凭据。监控和对话页手动输入的 API Key 保存在该浏览器来源的本地存储中，刷新后优先读取，可再修改或清除；不写入静态资源或 URL。本机自动连接使用的临时会话不会写入这份存储。

`type: app` 条目登记网关应用入口，与 `type: proxy` 的 `/services/<key>/` 分开。未登记的前缀不挂载。`/knowledge/` 保持独立凭据语义：转发知识库客户端 Authorization，不用模型 Key 覆盖。

`/video/` 在网关进程内直接读写本机 `yt-study-archive`，不另开 HTTP 端口，也不占用模型 Key。浏览免认证；入队/重试使用视频库自己的 Bearer token。控制台 session 不能当视频库凭证。旧路径 `/archive/` 仅在 video 声明 `legacy_prefixes` 时 301 到 `/video/`。

运行监控是内置应用 `kind: monitor`，默认入口 `/monitor/`，页面里再进入模型对话。AI 探员总部是内置应用 `kind: agents`，默认入口 `/agents/`。这两项未在 `models.json` 声明时网关仍会挂上；声明后可以改别名和前缀。旧地址 `/monitor.html`、`/agents.html` 在对应应用存在时 301 到它的入口，并保留查询参数。本机打开页面时自动领取控制台会话，再用 `Authorization: Bearer local-console…` 和 `X-Local-Console: 1` 读取各自的 API。

应用也可以自行登记一个和知识库同类的 HTTP 入口。`PUT /gateway-api/v1/apps/<键>` 使用模型 API Key，正文为 `alias`、`prefix`、`upstream`；网关把 `/prefix/` 反代到该上游并转发客户端 Authorization。记录写在被忽略的 `data/gateway-apps.json`，不进入 Git。`DELETE` 同一路径即卸下。`./manage.sh apps list|remove` 管理这份本机登记。`kind` 里 `knowledge`、`video`、`monitor`、`agents` 使用各自的专用实现；自行注册固定为 `http`，不能覆盖 `models.json` 里已有的键或路径。

`/services/<key>/` 使用模型 API Key。`auth.upstream` 决定上游 `Authorization`：`none` 不发送，`forward_client` 原样转发客户端带来的值，`bearer` 改送该服务自己的 `auth.bearer`。控制台 session 不能当业务凭证。`home_query` 只拼在首页链接上，不参与上游认证。`websocket: true` 开启 WebSocket 双向透传，否则升级请求返回 501。默认未探活返回 503；`health_check: false` 跳过转发前探活，直接访问上游。未知键或未允许路径返回 404。这些键不出现在 `GET /v1/models`。

网关入口 Key 与后端 Key 可以不同。转发和健康探测按“运行实例记录的 `api_key_file` → 模型 `runtime.api_key_file` → 项目 `.api-key`”读取后端凭据；PID 只记录文件引用和来源。通过 `--api-key` / `API_KEY` 启动的内联密钥不能由另一个网关进程可靠恢复，直连仍可使用，代理会返回不可用诊断；需要代理时改用 `--api-key-file` / `API_KEY_FILE`，或配置 `runtime.api_key_file` 后重新启动。缺失凭据文件也会显示在监控 `unavailable_backends` 中，不向客户端暴露密钥。

## 接口

| 路径 | 用途 | 模型能力 |
| --- | --- | --- |
| `GET /api/guide` | 当前应用、外部服务、MCP 服务和模型接口说明 | 不属于模型 API |
| `GET /v1/models` | 当前可路由模型键与别名 | 所有在线模型 |
| `POST /v1/chat/completions` | Chat JSON / SSE | chat |
| `POST /v1/completions` | 文本补全 | chat |
| `POST /v1/embeddings` | 文本向量 | embedding |
| `POST /v1/rerank` | 文档重排序，Jina 格式 | rerank |
| `POST /v1/audio/transcriptions` | multipart 音频转写 | asr |
| `POST /v1/audio/speech` | 参考音频克隆，JSON 输入 / WAV 输出 | tts |
| `/api/<模型键>/*` | 按路径选择已注册后端 | 依端点校验 |
| `/api/ollama/*` | Ollama 原生接口 | 原生推理也经过调度 |
| `/knowledge/*` | `type: app` / `kind: knowledge`，外部知识库反代 | 不属于模型 API |
| `/video/*` | `type: app` / `kind: video`，本机视频学习库 | 不属于模型 API |
| `/monitor/` | `type: app` / `kind: monitor`，运行监控与模型对话 | 不属于模型 API |
| `/agents/` | `type: app` / `kind: agents`，本机 AI 探员 | 不属于模型 API |
| `/gateway-api/v1/apps` | 自行注册或卸下 `kind: http` 入口 | 不属于模型 API，要模型 Key |
| `/services/<key>/*` | 登记为 `type: proxy` 的外部 HTTP 反代 | 不属于模型 API，不占 lane |

Responses / Messages 仅在模型 `endpoints` 显式包含 `/v1/responses`、`/v1/messages` 时开放。网关提供透传与相应流式错误封装，实际模型后端必须支持对应协议。

模型代理对路径解码一次，拒绝畸形/残余编码、点段和歧义分隔符。llama 原生 `/completion`、`/infill`、`/embedding`、`/reranking`、`/v1/reranking` 按对应标准能力和端点声明校验，并经过相同调度器。仅明确的元数据读取、tokenize/detokenize/apply-template 和 Ollama 管理路径允许无推理调度透传；未知代理端点返回 404，错误方法返回 405。

### Guide 中的 MCP 目录

`GET /api/guide` 的 `mcp_hub` 字段按当前 Hub 配置和运行状态生成。`services` 包含服务的 `id`、`name`、`kind`、`enabled`、`available`、`status`、客户端 `url`、`transport`、`tool_count` 和 `remote_online`；`aggregate` 给出 `/mcp/default` 聚合入口。连接协议为 Streamable HTTP。地址优先使用配置的 `listen.public_url`，否则使用当前请求主机名与 Hub 的实际监听端口；仅监听回环地址时明确标为本机专用。无法确定地址时 `url` 为 `null`。

`authentication` 说明独立设备令牌的 `Authorization: Bearer <设备令牌>` 用法，`permissions` 说明 `read` 和 `generate`。令牌须由管理员在 `/mcp-hub/` 为设备或项目创建，不能拿网关管理员密钥或控制台会话代替。公开目录不授予访问权限；实际工具目录仍由设备的服务范围、权限和服务白名单共同决定。

同一份 JSON 和浏览器页面还提供令牌申请步骤与可复制的客户端配置：

| 字段 | 内容 |
| --- | --- |
| `token_setup` | `title`、`management_path` 和 `steps`；每个步骤包含 `title`、`detail` |
| `connection_examples` | 启用服务及 `default` 聚合入口的示例列表；每项包含 `service_id`、`name`、`url`、`available`、`configs` |
| `connection_examples[].configs` | 每个客户端的 `client`、`config_file`、`format` 和可复制文本 `content` |
| `connection_notes` | 字符串数组，说明地址选择、合并配置、令牌保存及设备权限 |
| `verification` | 字符串数组，说明重新连接客户端、核对工具目录与 ComfyUI 状态的验证方法 |

示例地址随 Hub 配置和访问主机名生成；禁用服务或没有有效连接地址时不生成对应示例。示例存在不等于服务已运行，仍需检查 `available`。Claude Code 示例用于 `.mcp.json`，包含 `mcpServers` 和 `type: "http"`；Cursor 示例用于 `.cursor/mcp.json`，不包含 `type`；Codex 示例用于 `.codex/config.toml`，使用 `http_headers.Authorization`，启动超时为 30 秒、工具超时为 400 秒。将所需服务片段合并到已有配置并保留其他服务和设置；不要用整个示例覆盖已有文件。公开示例只包含 `<设备令牌>` 占位符，须在客户端私下替换；含令牌的配置应限制文件权限并加入 Git 忽略。

令牌步骤对应管理页的实际入口：由管理员使用“管理员 API Key”点击“连接管理”，进入“设备与连接”，在“添加设备 / 项目”填写“名称”、选择“允许访问的服务”，再点击“创建设备令牌”。“允许生成与提交任务”默认勾选，只读设备应取消勾选；读取权限始终包含。成功后用“复制令牌”保存，再点“已保存，关闭”。原令牌只在创建成功时显示，不能从后续快照找回。普通局域网用户应向管理员领取设备令牌；本机自动会话和设备令牌都不能创建其他令牌。管理员在本机或局域网管理均须提供真正的管理员密钥，浏览器请求须同源。完整步骤与三种客户端示例见 [MCP Hub](mcp-hub.md#给客户端设备授权)。

`available` 表示 Hub 已挂载服务，并不保证模型、模板或远端生成能力就绪。`remote_online` 仅反映最近已有的远端探测结果，未探测时为 `null`；guide 不触发生成或额外的上游探活。Hub 停止、内部状态不可读或最近探测显示远端离线时，服务标为不可用。配置保存后尚未重启应用时，`requires_restart` 为 `true`，服务状态为 `pending_restart`，暂不标为可用。停用条目保留在目录中并标为 `disabled`；未配置服务时返回空列表。读取 guide 不会启动 Hub，也不会创建数据库或内部凭据。

## 监控 API v1

| 接口（GET/HEAD） | 数据 |
| --- | --- |
| `/monitor-api/v1/snapshot` | 系统资源、全部注册及发现模型、可选 `services` 外部服务、独立 `apps` 应用入口、网关通道和诊断 |
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

## TTS 语音合成

`POST /v1/audio/speech` 接收 `model`、`input`、`ref_audio_base64`、`ref_text`、`language`、`response_format: "wav"`。字段约束和调用脚本见 [Qwen3-TTS 部署](qwen3-tts-deployment.md)。这是项目扩展，首版不支持音频流，`stream:true` 返回 400。默认模型按注册表 `default_for:["tts"]` 或 `DEFAULT_TTS_MODEL` 选择；独立 TTS lane 默认并发 1，可通过 `TTS_LANE_CONCURRENT` 设置。

## 服务代理

已在 `models.json` 注册的 `type: proxy` 服务通过 `/services/<key>/` 访问。未注册的服务不挂载。网关按配置进行鉴权、路径和方法检查，并转发 HTTP 请求；`websocket: true` 启用 WebSocket，`health_check: false` 允许直接转发而不以探活结果作为前置条件。

`browser: true` 提供浏览器登录和仅限该服务路径的签名 cookie；程序客户端使用 Bearer Key。浏览器登录用户名任意，密码为网关 API Key；浏览器登录凭据和网关会话 cookie 不转发给上游。网页服务须兼容子路径部署；服务代理不会改写前端脚本。

请求体默认受 `MAX_REQUEST_BODY_BYTES`（64 MiB）限制，可通过服务的 `max_body_bytes` 单独设置。`timeout` 用于 HTTP 请求及 WebSocket 握手，升级成功后的空闲 WebSocket 不按 HTTP 超时关闭。
