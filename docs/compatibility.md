# 兼容契约与主动变化

重构基线为 `ecfe929`。本文件区分保留的外部入口和主动修复的行为；不把历史缺陷作为永久兼容要求。接口验证使用临时目录、临时端口和假后端，真实模型验证单独执行。

## 命令行契约

根目录脚本继续可从其他工作目录调用；`bootstrap.py` 确定本 checkout，包也支持安装后通过 `local-llm` 或 `python -m local_llm_deploy` 使用。指定部署目录使用全局 `--project-root PATH`，或环境变量 `LOCAL_LLM_ROOT`。

核心支持 Python 3.9 及以上。editable 安装需要支持 PEP 660 的 pip；建议 pip >=23，本轮现有 Python 3.9 环境的维护安装使用 pip 25.3 验证。旧 pip 21 不能作为 editable 安装兼容基线；pip 升级是显式维护步骤，不在普通命令中自动执行。

| 旧入口 | 保留用法与含义 |
| --- | --- |
| `manage.sh list` / `status` | 查看注册模型和实例；无需 PyTorch、MLX 或下载依赖 |
| `manage.sh registry` | `init [--force]`、`list`、`show [key]`、`merge file.json`、`remove key` |
| `manage.sh download MODEL` | `--quant Q`、`--source huggingface\|modelscope`、`--to PATH`；已有权重保留原位置 |
| `manage.sh models` | 查看权重安装和 manifest；不触发下载 |
| `manage.sh register MODEL --path PATH [--quant Q]` | 登记本地安装；相对路径以部署目录为基准 |
| `manage.sh remove MODEL` | `--quant Q`、`--all`、`--force`；仍使用安装清单选择目录 |
| `manage.sh start MODEL` | `--port P`、`--host HOST`、`--lan`、`--quant Q`、`--model-dir PATH`；新增 `--dry-run` 查看脱敏启动描述 |
| `manage.sh stop MODEL` / `stop --all` | 停止本项目管理的实例；不接管 Ollama 或外部 HTTP 服务 |
| `manage.sh logs MODEL` | 默认追踪日志；`--no-follow --lines N` 用于一次性读取 |
| `deploy.sh` | 保留 `--model-name`、`--model-dir`、`--cpp-dir`、`--quant`、`--port`、`--api-key`、`--api-key-file`、`--host`、`--lan`、`--no-lan` |
| `jina.sh` / `whisper.sh` / `serve-ui.sh` / `ds4.sh` | 保留服务管理入口，由 Python CLI 解释运行参数与生成启动描述 |
| `serve_embedding.py` / `serve_rerank.py` / `serve_whisper.py` | 保留 `--model-name`、`--model-dir`、`--host`、`--port`；新增 `--project-root`；重型依赖仅在启动相应模型时导入 |
| `scripts/start-*.sh` | 保留旧 LaunchAgent 引用的入口路径 |

成功和帮助返回 `0`；配置、依赖、启动或运行操作错误返回非零（应用错误通常为 `1`，参数解析错误为 `2`）。中断由 CLI 返回 `130`；直接模型服务收到 SIGINT/SIGTERM 时清理后正常退出。旧的彩色表格、错误措辞和日志 banner 不作为可解析协议；自动化应使用 `list --json`、`status --json`、`plan MODEL`。

重复 start 返回明确错误，不再静默复用或覆盖实例；重复 stop 成功且说明已停止/未运行。启动成功意味着就绪探测已通过，端口冲突或加载失败返回错误。缺少依赖会给出安装入口，普通 start/download 不隐式执行 pip 升级。

## 配置、安装与状态

通用优先级为显式 CLI > 环境变量 > 模型配置 > 默认值。旧 `type` 映射到能力和后端；`_` 开头的顶层注释键忽略，模型中的未知扩展字段保留，冲突别名和无效参数整体拒绝。`models.json` 无需为了重构改成新格式。

| 输入 | 生效范围 |
| --- | --- |
| `LOCAL_LLM_ROOT` / `--project-root` | 配置、静态资源、密钥、日志、运行目录的共同根目录 |
| `CPP_DIR` / `--cpp-dir` | 显式覆盖 llama.cpp 引擎目录；未指定时使用模型配置的引擎档案 |
| `MODEL_DIR` / `--model-dir` | 覆盖安装选择；CLI 把已解析结果传给子进程，子进程不会重新任选安装 |
| `PORT` / `HOST` 及 Jina、Whisper 服务变量 | 覆盖监听设置；CLI 的显式参数优先 |
| `API_KEY` / `API_KEY_FILE` | 启动模型后端的显式认证配置；两种显式形式不能同时指定 |
| `.api-key` | 未显式覆盖时的本地后端密钥，以及网关模型 API 的 Bearer Key 来源 |
| `.hf-env` | 仅下载工具读取的字面配置；不作为 Shell 执行，已导出的环境变量优先 |

直接运行辅助 Python 服务还接受 `JINA_EMBED_*`、`JINA_RERANK_*` 或 `WHISPER_*` 的 `MODEL_NAME`、`MODEL_DIR`、`HOST`、`PORT`；Jina 继续接受通用 `JINA_*`。生命周期内部的 `LOCAL_LLM_MODEL_DIR` 是已经选择好的安装，不是第二份安装记录。

路径解析保留一层嵌套 GGUF 布局，显式模型目录和配置中的安装位置优先。登记了多个候选安装时必须通过 `--model-dir` 选择，不能依赖目录枚举顺序。manifest 与注册表原子更新并加锁；`models/` 根目录、越界符号链接目标以及仍被使用的权重不可删除，`--force` 不绕过这些约束。

下载源选择变为 CLI `--source` > 已导出环境变量 > 模型配置 > ModelScope 默认值；`.hf-env` 不覆盖已经导出的环境。`--to` 现在只允许 `models/` 子目录，旧版允许越界的绝对路径不再接受。默认启动也会考虑 manifest 中登记的安装；同一模型/量化存在多个安装时要求显式选择。删除检查覆盖共享权重使用者，`--force` 不再绕过运行和共享保护。

`init_llamacpp.sh` 的新构建要求 `--revision` 提供完整 commit，`setup_llamacpp.sh` 不再默认拉取并切换历史 PR。实验引擎在独立目录构建并保留旧构建，模型通过具名档案或 `CPP_DIR`/`--cpp-dir` 选择；普通启动不自动更新引擎。

PID 仍位于 `run/<key>.pid`，前三行格式保持：

```text
<pid>
<port>
<alias>
```

新记录在同一文件的第四行加入版本、进程身份、运行方式和就绪元数据；不会维护另一份同等权威的运行状态。读取兼容旧三行记录。进程归属检查不能只凭 PID；未知或已复用 PID 不可被停止操作误杀。状态查询只读，失效记录由 stop/reconcile 等生命周期操作清理。

独立模型服务在实例锁内加载、绑定成功后发布记录，发布后释放启动锁，退出时仅清除自己的记录。由生命周期管理器启动时，子服务不写删 PID，由管理器通过就绪探测后发布。LaunchAgent label 和已有入口路径继续使用；读取 status 不自动更新 plist。

## 网关 HTTP 契约

| 路径 | 请求与响应 |
| --- | --- |
| `GET /api/models` | 保留监控模型列表和队列信息，只读免认证 |
| `GET /api/system` | 保留系统资源监控，只读免认证 |
| `GET /v1/models` | OpenAI 格式模型列表；模型 API 认证生效 |
| `POST /v1/chat/completions` / `/v1/completions` | JSON 或 SSE；按模型能力和后端支持的端点路由 |
| `POST /v1/embeddings` | OpenAI 风格 `object/model/data/usage` |
| `POST /v1/rerank` | Jina 风格 `model/results/usage` |
| `POST /v1/audio/transcriptions` | multipart 原始音频；支持 `json`、`text`、`verbose_json` |
| `/api/<model-key>/...` | 指定模型后端代理；保留路径形式，统一认证 |
| 默认 `/api/...` | 显式默认模型代理；统一认证 |
| `/api/ollama/...` | Ollama 兼容代理；推理和操作请求均认证 |
| `/knowledge/` | 独立知识库反代；路径重写和客户端 Authorization 保留，不替换成模型 Key |
| `/monitor.html` | 保留监控页面 URL；需要后端访问时由页面提供会话内 Key 输入 |

只读 API 例外是精确的 `/api/models`、`/api/system`，不能通过后缀或其他代理路径推断免认证。静态页面和知识库使用各自的处理路径。`.api-key` 缺失或首行为空时，与原行为一致不启用本地模型 Key 校验。

以下是主动变化：

| 场景 | 基线问题 | 重构后契约 |
| --- | --- | --- |
| 显式未知模型 | 可能回退第一个可用后端 | `404`；不会向另一模型发送请求 |
| 已注册但未就绪模型 | 可能回退其他模型 | `503` |
| 缺失模型 | 依赖枚举顺序选后端 | 使用显式配置默认模型，否则 `400`；配置 `default_for` 或 `DEFAULT_<CAPABILITY>_MODEL` |
| 能力不匹配 | Chat 候选可能包括 Rerank | 明确拒绝；Rerank 有独立路由和默认并发 `1` |
| Responses / Messages | Chat 能力被混同为协议兼容 | 后端必须明确声明支持相应端点 |
| `/api/*` 模型代理 | 可能自动注入服务端 Key 绕过客户端认证 | 转发前先检查客户端 Bearer Key |
| 编码路径与原生推理别名 | 可能绕过能力与调度 | 路径统一解码校验，原生别名纳入相同预算；未知代理端点 404 |
| 独立后端密钥 | 默认总是注入网关 Key | 使用实例/模型的密钥文件引用；不可恢复的 inline Key 显示不可用诊断，需改用文件 |
| 路径模型与 body 模型矛盾 | 选择结果不明确 | `400` |
| 断开的推理请求 | 下游断开可能过早释放预算 | 及时关闭传输；完成/取消无法确认时保留占用并标记实例不确定，阻止新推理 |

`queue_depth` 保留“已进入调度范围的请求数”，包括活动请求，不等于纯等待数；纯等待使用新增的 `waiting`。Chat 默认全局并发 `1`，Embedding `2`，ASR `1`，Rerank `1`；这些设置及 KV 估算预算在进程启动时固定，改变后重启网关。注册表刷新成功后整体替换，活动请求继续使用原快照。

SSE 在响应头发出前使用正常 HTTP 错误码；排队保活已发送 200 后，后续错误必须使用相应端点的流事件表达。不能把 HTTP 200 或客户端连接关闭当作推理成功/结束的证明。`uncertain` 实例需要确认后端已空闲或重启后端，再恢复网关调度；不要仅重启网关绕过仍在计算的请求。

非流式和 Ollama 等待首包时也检测 RST。仅收到 TCP FIN 时可能仍是合法半关闭，不能据此判定客户端不再接收；该情况下仍由响应写失败或请求超时触发清理。明确元数据、tokenize/detokenize/apply-template 和 Ollama 运维接口继续可代理，其余未知代理接口不再任意透传。

## 三种辅助服务的协议

服务直连的 `/health` 不需 Key；已加载返回 `200 {"status":"ok"}`，未加载返回 `503`。各模型 POST 在启用密钥时要求 `Authorization: Bearer ...`。错误返回 JSON `{"error":{"message":"...","type":"..."}}`，未知路径返回 JSON 404。请求体上限为 64 MiB，超出返回 413；必须提供一个有效 Content-Length，暂不接受 chunked 请求体。

Embedding 保留字符串/字符串数组、批处理、task adapter 选择、查询/文档前缀、设备选择和 adapter 锁。`retrieval.query` 使用查询前缀；`retrieval.passage` 使用文档前缀。响应的 embedding 按输入顺序编号，usage 汇总批次 token 数。

Rerank 保留 query/documents/top_n、可选 document/embedding 字段以及原结果排序；模型适配器增加互斥锁，避免并发调用共享 MLX 模型。`max_documents` 来自模型配置，默认 64。

Whisper 以二进制解析 multipart，音频写入临时文件后调用模型，成功和失败都删除临时文件。`text` 返回 UTF-8 文本，`json` 返回 text 字段，`verbose_json` 保留 task/language/duration/text/segments；ASR 推理锁继续生效。

`tools/ffmpeg` 入口和可执行权限保留，原仓库中的平台二进制改为小型 launcher，使用当前 Whisper 环境里固定依赖 `imageio-ffmpeg` 提供的可执行文件。Whisper 在 warmup 前设置 `LOCAL_LLM_FFMPEG_PYTHON=sys.executable`，因此替换环境或 `runtime.python` 会使用对应环境的 FFmpeg；launcher 通过 exec 保留所有参数和信号，不依赖用户主目录。独立调用 launcher 时默认选择项目的 `.venv-whisper`，也可显式设置该解释器变量。

非法字段现在返回可恢复的 400：JSON 必须为对象；dimensions/top_n 必须是正整数（继续接受整数字符串），不能为布尔值或被截断的小数；documents/input 数组必须包含字符串；布尔选项必须为 JSON 布尔值。Embedding dimensions 不能超过模型宽度，当前仅支持 `encoding_format: "float"`。Whisper task 限定 transcribe/translate，response_format 限定上述三种，不再把不支持的格式静默当 JSON。

## 可重复验证

无模型测试：

```bash
python -m unittest discover -s tests -t . -v
```

该命令要求先安装轻量包，测试不会读取真实 `.api-key`、下载模型或操作常驻服务。服务封装契约位于 `tests/services/test_services_contract.py`；网关使用独立假后端测试。

真实冒烟只由明确执行的脚本触发，按顺序发送小样本：

```bash
./scripts/test-services.sh --jina 8004 --proxy 8888
./scripts/test-services.sh --jina 8004 --proxy 8888 --rerank 8006 \
  --whisper 8007 --audio /absolute/path/short.wav --chat-model <registered-model>
```

脚本验证结构、Embedding 维度、Rerank 字段、Whisper 三种格式、Chat JSON/SSE，不要求实际模型输出逐字一致。自定义模型使用 `--embedding-model`、`--rerank-model`、`--whisper-model`。Key 在进程内读取且不打印。省略 `--rerank`、`--whisper` 或 `--chat-model` 时不调用相应服务；`--help` 不发送请求。

这些命令说明验证方法，不是已完成真实模型、登录自启或引擎升级演练的声明；实际记录见对应验证报告。
