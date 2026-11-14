# 独立 MCP Hub

MCP Hub 为本机及局域网设备提供统一的 MCP 服务入口，默认监听 `8899`。管理页面位于现有网关的 [MCP Hub](http://127.0.0.1:8888/mcp-hub/)，与模型监控、模型对话并列。Hub 有自己的 Python 环境、进程、配置和设备凭据，不作为模型参与推理调度。

`8888` 负责配置、启动、停止、设备授权与日志；`8899` 负责 MCP 调用、任务与文件传输。Hub 停止时管理页面仍可使用；停止或重启 `serve-ui` 不会停止已经运行的 Hub。

在 `8888` 打开 `/api/guide` 可查看当前 MCP 服务目录；客户端带 `Accept: application/json` 可读取同一份说明中的 `mcp_hub` 字段。目录列出各服务与聚合入口的 Streamable HTTP 地址、运行可用性、设备令牌认证方式和权限说明。地址来自 `public_url`，未配置时按访问主机名与 Hub 端口生成。目录不返回令牌、上游 ComfyUI 地址或启动参数；看到服务仍需另行获得该设备的授权。未启用、停止和等待重启的服务会明确标记，不当作可用服务。可用状态不代表生成模型或模板已就绪，字段含义见 [API 说明](api-guide.md#guide-中的-mcp-目录)。

Guide 同时提供令牌创建步骤，以及 Claude Code、Cursor、Codex 的可复制连接配置。示例按当前启用的服务与聚合入口生成，使用动态连接地址和设备令牌占位符；禁用服务或没有有效地址时不生成示例。配置完成后的验证方法也在同一页面；客户端配置只填写设备令牌。

首个服务为 ComfyUI。Hub 管理连接 ComfyUI 的 MCP 子进程，远端 ComfyUI 的启动、停止、节点安装和模型安装仍由其原有管理方式负责。停止 Hub 不代表远端生成任务停止。

## 安装独立环境

在本项目根目录执行。当前部署使用 macOS 与 Python 3.14，FastMCP 固定为 `4.2.0`；该版本最低要求 Python 3.11。使用独立环境和已验证的依赖约束安装：

```bash
python3.14 -m venv .venv-mcp-hub
.venv-mcp-hub/bin/python -m pip install -r requirements/mcp-hub.txt \
  -c constraints/mcp-hub-macos-py314.txt
```

约束文件对应本项目验证过的 macOS / Python 3.14 环境；更换平台或 Python 版本时，需要验证相应依赖组合。依赖入口同时安装本仓包，供独立 Python 环境使用。默认解释器为 `.venv-mcp-hub/bin/python`，需要覆盖时设置 `MCP_HUB_PYTHON`。既有 `manage.sh` 和 `8888` 控制层仍只需原来的环境。

打开管理页面，输入现有 `.api-key` 中的网关管理员密钥。MCP 管理必须使用真实配置的根密钥，本机监控的自动会话不能代替管理员登录；未配置根密钥时，管理接口不会匿名开放。不要把根密钥发给普通客户端设备。

## 配置第一个 ComfyUI 服务

在管理页面添加服务，服务 ID 使用 `comfyui`，类型为 `comfyui`，填写 Hub 主机上已安装的 `comfy-mcp` 可执行文件路径，并配置它连接的 ComfyUI 地址。以下仅展示配置结构，其中路径和地址需替换成本机实际值：

```json
{
  "schema_version": 1,
  "revision": 0,
  "listen": {
    "host": "0.0.0.0",
    "port": 8899,
    "public_url": ""
  },
  "allowed_origins": [],
  "services": [
    {
      "id": "comfyui",
      "name": "ComfyUI",
      "kind": "comfyui",
      "enabled": true,
      "command": "/absolute/path/to/comfy-mcp",
      "args": [],
      "env": {},
      "url": "http://<ComfyUI主机IP>:8188",
      "allowed_tools": []
    }
  ]
}
```

`command` 在 Hub 主机上执行，`env` 也由该主机传给 MCP 子进程。ComfyUI 类型的 `url` 指向实际 ComfyUI 服务，Hub 将它传入子进程的 `COMFYUI_URL`；不填写 MCP Hub 地址，也不要把现有 `/services/comfyui/` 网关挂载路径当成该地址。客户端只需要连接 Hub，不需要安装这个可执行文件或持有上游凭据。

`allowed_tools` 留空表示使用 Hub 默认工具集合，再按设备的 `read` / `generate` 权限过滤；填写列表后进一步限制允许使用的工具。ComfyUI 包装服务只提供它声明的工具，不会把上游的服务重启、节点安装等管理工具一并开放。

保存配置后启动 Hub，查看服务探测结果和工具列表。`public_url` 用于生成客户端可访问的地址；留空时管理页面按当前浏览器使用的主机名与 Hub 端口生成。局域网设备需要服务器的局域网地址，不能使用服务器的 `127.0.0.1`。

浏览器来源许可与设备授权分开：普通桌面 MCP 客户端通常不发送 `Origin`；浏览器调用需按实际来源设置 `allowed_origins`。管理页面始终要求与 `8888` 同源。

## 接入其他 MCP 服务

管理页面还支持 `stdio` 和 `http` 类型。`stdio` 使用 Hub 主机上的 `command`、`args`、`env` 启动服务；`http` 使用上游 Streamable HTTP 的 `url`。首版 HTTP 上游仅配置 URL，暂不支持自定义请求头、Bearer 密钥或 OAuth；设备访问 Hub 的 `Authorization` 不会转发给上游。HTTP 上游直连，不使用宿主机的代理环境变量。

通用 `stdio` 服务每次请求使用新子进程，不支持依赖进程内存的跨调用会话，例如先打开浏览器、再在下一次调用中操作同一浏览器。ComfyUI 使用专用的持久桥接，不受此限制。通用服务的工作目录按服务保存，磁盘文件可能在请求和设备之间共享；它不是文件系统隔离边界。工具中的本地路径指向 Hub 主机，工具本身的文件访问与状态语义由管理员登记的上游服务决定。

`allowed_tools` 留空时不增加工具白名单限制；填写后限制可见与可调用的工具。上游工具明确声明 `readOnlyHint: true` 才按 `read` 权限开放，其他工具要求 `generate`；资源和提示词要求 `read`。设备仍必须获授对应服务。

## 启停与查看状态

管理页面与 CLI 使用同一套生命周期控制，配置在 Hub 重启后生效。

```bash
./manage.sh plan mcp-hub
./manage.sh start mcp-hub
./manage.sh status mcp-hub --json
./manage.sh logs mcp-hub --no-follow --lines 100
./manage.sh restart mcp-hub
./manage.sh stop mcp-hub
```

需要直接观察进程时使用 `./manage.sh foreground mcp-hub`。macOS 默认通过本用户的 LaunchAgent 管理，`start` 会准备 LaunchAgent 并启动 Hub。只需安装或卸载 LaunchAgent 时，先停止 Hub，再执行相应操作：

```bash
./manage.sh stop mcp-hub
./manage.sh daemon mcp-hub install
```

卸载时使用：

```bash
./manage.sh stop mcp-hub
./manage.sh daemon mcp-hub uninstall
```

`daemon install` 只准备后台服务配置，随后用 `start` 启动。实例键为 `mcp-hub`，LaunchAgent 名称为 `com.local-llm-deploy.mcp-hub`。Hub 使用独立启停命令，不随模型的 `stop --all` 停止。

本地数据位置：

| 路径 | 内容 |
| --- | --- |
| `data/mcp-hub/config.json` | 带修订号的 Hub 配置与上游环境变量 |
| `data/mcp-hub/state.sqlite3` | 设备授权、任务、文件登记 |
| `data/mcp-hub/assets/` | 上传文件与取回的生成结果 |
| `data/mcp-hub/workspaces/` | 按设备与服务分开的 ComfyUI 文件处理目录 |
| `data/mcp-hub/bridges/` | 按服务分开的 MCP 桥接工作目录 |
| `data/mcp-hub/internal.key` | 网关访问 Hub 内部接口的密钥 |
| `run/mcp-hub.pid` | 本项目管理的 Hub 实例记录 |
| `logs/mcp-hub.log` | Hub 日志；页面与 CLI 展示时脱敏 |

这些是私有部署数据，不提交 Git。需要备份时先停止 Hub，再备份 `data/mcp-hub/`；设备令牌无法从数据库还原。ComfyUI 输出取回还依赖运行 Hub 的同一用户下，`comfy-cli` 自己保存的 `jobs` 状态，其中记录原任务的远端地址。迁移主机或更换运行用户时也要保留这部分状态，不能只复制 Hub 数据库。

## 给客户端设备授权

普通客户端向 Hub 管理员领取独立设备令牌，不需要获得管理员密钥。管理员可在本机或局域网打开 `8888/mcp-hub/`；管理操作始终要求已配置的管理员密钥，浏览器请求须与管理页面同源。本机监控的自动会话、普通设备令牌均不能用于管理。

1. 若显示“连接管理控制台”，在“管理员 API Key”输入管理员凭据，点击“连接管理”。
2. 进入“设备与连接”，在“添加设备 / 项目”填写“名称”，例如项目名或设备名，并勾选“允许访问的服务”。
3. 根据用途设置“允许生成与提交任务”。此项默认勾选；只读设备应取消勾选。读取权限始终包含。`generate` 还允许上传、修改工作流、提交生成和取回输出。
4. 点击“创建设备令牌”。在新令牌区域点击“复制令牌”，当场保存到该设备的私有配置或权限受限的本地文件。
5. 在右侧“连接 MCP 客户端”选择对应“服务”，查看“连接地址”并点击“复制连接配置”。这里复制的是通用 JSON，Codex 应使用下方 TOML 示例。刚创建的令牌获授该服务时，复制的配置会包含它；否则仍需替换 `<设备令牌>` 占位符。不同客户端的完整格式见下文。
6. 保存完成后点击“已保存，关闭”。**令牌只在本次创建后显示，刷新、退出或关闭后不能再次查看。**

每台设备或每个项目使用自己的令牌。需要撤销时，在设备列表点击“撤销”，再点击“确认撤销”；后续请求将被拒绝，但已提交到远端的生成任务可能继续运行。当前没有原设备内轮换令牌的按钮，丢失时需撤销旧设备并重新创建。新设备不会自动获得旧设备的任务或文件。

HTTP 局域网页面的剪贴板功能可能受浏览器限制；出现“浏览器未允许复制，请选择文本手动复制”时，选择文本手动复制即可。

### 配置客户端并验证

优先从 `/api/guide` 复制对应客户端和服务的示例，再把 `<设备令牌>` 替换为管理员为当前设备签发的令牌，不填写管理员根密钥。将服务条目合并到现有配置，保留其他服务与设置；不要用示例整体覆盖已有文件。含令牌的文件应加入 Git 忽略并限制为当前用户可读写，例如权限 `0600`。

以下示例中，`hub-host` 替换为客户端可达的 Hub 主机名或地址；同机客户端可以使用 `127.0.0.1`，局域网另一台电脑必须使用服务器的局域网地址。`/mcp/comfyui` 是 MCP 连接地址，`8888/mcp-hub/` 是管理页面，两者不能互换。若客户端使用 HTTP 代理，将 Hub 主机名或地址加入 `NO_PROXY` / `no_proxy`，或为该地址设置直连。

Guide 为客户端连接使用 `hub-<服务标识>` 名称，例如 `hub-comfyui`；它是客户端连接名，URL 中的实际服务标识仍为 `comfyui`。

Claude Code 的项目配置 `.mcp.json`：

```json
{
  "mcpServers": {
    "hub-comfyui": {
      "type": "http",
      "url": "http://hub-host:8899/mcp/comfyui",
      "headers": {
        "Authorization": "Bearer <设备令牌>"
      }
    }
  }
}
```

Cursor 的项目配置 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "hub-comfyui": {
      "url": "http://hub-host:8899/mcp/comfyui",
      "headers": {
        "Authorization": "Bearer <设备令牌>"
      }
    }
  }
}
```

Codex 的项目配置 `.codex/config.toml`：

```toml
[mcp_servers.hub-comfyui]
url = "http://hub-host:8899/mcp/comfyui"
http_headers = { Authorization = "Bearer <设备令牌>" }
startup_timeout_sec = 30
tool_timeout_sec = 400
```

要通过一个连接使用多个服务，将 MCP 地址改为 `/mcp/default`。聚合入口只显示当前设备获授的服务，工具名称增加 `<服务ID>_` 前缀，例如 `comfyui_run_workflow`；单服务入口 `/mcp/comfyui` 中仍叫 `run_workflow`。聚合入口使用同一套设备令牌与权限，不会额外授予服务访问权；`default` 是保留的服务 ID。

保存后重新连接或重开客户端，按客户端提示启用连接并确认工具目录可见。ComfyUI 单服务入口可调用只读工具 `server_info` 检查桥接及 `remote.online`；聚合入口按服务标识增加前缀，例如 `comfyui_server_info`。工具目录仍按当前设备的服务范围、权限和服务白名单过滤。此验证不提交生成任务，也不代表模板、节点或模型已经就绪；连接失败时先核对地址、令牌、服务状态和权限。

## 上传输入、运行工作流、下载结果

客户端电脑的路径不等于 Hub 主机路径。ComfyUI 包装工具使用 `asset_id` / `workflow_id` 引用已上传文件，不接收客户端电脑的绝对路径作为远程文件。上传工作流后先调用 `validate_workflow(workflow_id=...)`；界面格式工作流还可通过 `list_workflow_slots` 检查参数槽，能否执行以模板、节点和远端模型校验结果为准。

客户端附带文件传输脚本，只用 Python 标准库。先将设备令牌保存到本地文件，文件中只存这一条令牌；脚本通过 `MCP_HUB_TOKEN_FILE` 读取，也支持 `MCP_HUB_TOKEN` 环境变量。

```bash
export MCP_HUB_TOKEN_FILE="$HOME/.config/mcp-hub/device-token"

python3 scripts/mcp-hub-client.py --url http://<Hub局域网IP>:8899 \
  upload --service comfyui ./workflow.json
```

上传响应的 `id` 就是该工作流的 `workflow_id`。图片、音频或视频输入同样先上传，使用响应 `id` 作为 `asset_id`；文件留在客户端原位置，Hub 存储自己的上传副本。

生成任务需要参考素材时，再调用 `upload_file(asset_ids=[...])` 将这些 Hub 文件上传到远端 ComfyUI 的输入目录，取得远端文件名。使用该文件名填写模板参数或工作流输入。`set_workflow_slot(workflow_id, overrides)` 会返回新的 `workflow_id`，保留原工作流。也可以用 `fetch_template(name)` 将已有模板登记为当前设备的工作流。

接着在 MCP 客户端调用工作流工具：

```json
{
  "name": "run_workflow",
  "arguments": {
    "workflow_id": "<上传工作流返回的id>",
    "request_id": "project-shot-01-attempt-01"
  }
}
```

上面是工具调用参数示意，不是直接向 HTTP 地址提交的完整 MCP 报文。成功提交时返回 `{ "task": { "id": "...", "prompt_id": "...", "status": "submitted" }, "reused": false }` 形式的持久任务记录；记录 `task.id`，之后用 `job(task_id=...)` 查询，用 `fetch_outputs(task_id=...)` 取回结果。`fetch_outputs` 返回的 `assets` 数组中，每个文件的 `id` 可用于下载：

```bash
python3 scripts/mcp-hub-client.py --url http://<Hub局域网IP>:8899 \
  download <输出文件id> --out ./output.mp4
```

使用 `video-remake` 时，在运行该项目的客户端电脑下载结果，再执行其 `media:land` 登记素材；Hub 不会替远端项目写入制作目录。

`run_template` 与 `generate_image` 也使用这套持久任务流程。工作流或模板含付费 API 节点时，`confirm_spend` 默认关闭；用户明确授权对应调用后才启用。

### video-remake 接入

`video-remake` 的 `comfy-mcp` provider 已支持 Hub 检查与项目连接配置，生成仍由 Agent 直接调用 MCP。先在管理页为该项目创建设备令牌，授予 `comfyui` 服务的 `read` / `generate` 权限，再在客户端项目的私有 `video-remake.json` 中配置：

```json
{
  "media": {
    "video": {
      "provider": "comfy-mcp",
      "hubUrl": "http://hub-host:8899/mcp/comfyui",
      "hubTokenFile": ".secrets/comfy-hub.token"
    }
  }
}
```

`hub-host` 替换为 Hub 主机地址，使用服务专用的 `/mcp/comfyui` 入口，不用聚合入口 `/mcp/default`。令牌保存到项目内、被 Git 忽略且权限为 `0600` 的普通文件，不能使用符号链接。环境变量覆盖分别为 `COMFY_MCP_HUB_URL` 与 `COMFY_MCP_HUB_TOKEN_FILE`。

以下命令在 **video-remake 项目根目录** 执行：

```bash
node cli/remake.js media:doctor --task video --json
node scripts/configure-mcp-hub.mjs
```

doctor 会真实执行 MCP 初始化、读取工具目录、调用 `server_info`，检查当前设备可用的生成工具以及桥接和远端状态；不提交生成，也不保证生产模板的节点或模型齐全。Hub 两个字段必须配对，配置错误或检查失败不会回退直连。原有 `media.video.url` / `apiKeyFile` 及其环境变量仍用于未配置 Hub 时的 ComfyUI REST 探活，不能把 MCP 地址填进 `url` / `COMFYUI_URL`。

连接配置脚本读取相同的 Hub 地址和设备令牌，生成本项目私有的 `.codex/config.toml`、`.mcp.json` 与 `.cursor/mcp.json`，服务名为 `comfy-hub`。文件须被 Git 忽略，以 `0600` 权限保存，不写全局配置。其他 JSON 服务与设置保留；已有非本脚本生成的非空 Codex TOML 会保留并报错，需要手动合并。脚本支持 `--workspace DIR`、`--url HTTP_MCP_URL`、`--token-file PATH`，本身不连接 Hub。它用于直接打开项目的 Codex、Claude Code 或 Cursor；重开客户端后确认工具可用。

`video-remake` 的 `npm run agent:run` 有独立的显式开关：在本机 `agent-runtime.json` 顶层设置 `"mcp": { "projectHub": true }`，Codex、Claude Code、DeepSeek Harness 任务便只从项目 `media.video.hubUrl` / `hubTokenFile` 加载 `comfy-hub`。默认关闭，不读取客户端连接文件或继承全局 MCP，也无需先运行注册脚本。`read-only` 目标只提供 9 个查询工具，其他任务权限最多 17 个工具，仍受既有设备令牌限制。`agent:plan` 不读令牌、不连接 Hub，也不写 MCP 文件；`agent:doctor` / `agent:run` 检查令牌与远端状态。执行凭据由环境传入，临时 MCP 配置只含环境变量引用并在退出时清理，日志与运行记录经过双层脱敏。

该项目还提供文件传输脚本，默认读取上述配置，使用同一设备令牌：

```bash
node scripts/mcp-hub-files.js upload ./reference.png
node scripts/mcp-hub-files.js download <输出素材id> --out ./output.mp4 --sha256 <返回的sha256>
node cli/remake.js media:land <slug> --kind video --from ./output.mp4 \
  --intent "<素材用途>" --provider comfy.run_workflow
```

传输脚本只上传、下载文件，不调用生成；同样支持 `--workspace`、`--url` 和 `--token-file`。上传返回的 `id` 用于 MCP 的 `asset_ids` 或 `workflow_id`；参考素材还需经 `upload_file(asset_ids=[...])` 上传到远端 ComfyUI。生成通过 `run_workflow` / `run_template` 提交并保存 `task.id`，重试复用 `request_id`，查询和取回使用 `task_id`。下载目录须已存在，文件不覆盖；校验文件长度与可选的 SHA-256，缺少响应长度时必须提供 `--sha256`。最终 `media:land` 读取的是客户端下载成功的文件，不是 Hub 路径或素材 ID。

已使用 64×64、8 帧的最小 MP4 工作流完成实机链路验收：上传参考素材与工作流、验证并提交、相同 `request_id` 去重、查询任务、取回输出、按 SHA-256 下载、重新建立 MCP 连接后恢复查询，以及写入 `media:land` 台账。重新验收时可沿用这一小工作流方法；实际生成需要对应授权，生产模板的节点、模型和画质仍需另行检查。私有连接、令牌与验收产物不写入通用文档。

### 文件 HTTP 接口

| 请求 | 请求体 / 结果 |
| --- | --- |
| `PUT /assets?service=comfyui&filename=workflow.json` | 原始文件字节，返回包含 `id`、`download_path` 的文件信息 |
| `GET /assets/{id}` | 返回该设备有权读取的文件字节 |

两个接口都使用 `Authorization: Bearer <设备令牌>`。文件上传不是 multipart；`filename` 只传文件名，非 ASCII 或含特殊字符时进行 URL 编码。大文件走这些 HTTP 接口，MCP 中只传文件标识与结果信息。不超过 8 MiB 的文件也可用 `put_asset(filename, content_base64)` 上传。

HTTP 单文件上限为 512 MiB。客户端下载时拒绝覆盖已有文件，先写临时文件，核对响应长度后再发布到 `--out`；不会把未完成下载当成成片。

### 超时、断线与恢复

- 提交前选定一个 `request_id`，同一次生成重连时继续使用它。新一轮生成才换新的 ID，不要因为 HTTP 超时就换 ID 重提。
- 已取得任务 ID 时继续查询原任务。Hub 重启后任务记录仍在，可以继续查询上游状态或重新取回已有输出。
- 提交阶段断线、尚未拿到 `prompt_id` 的任务可能处于不确定状态。先核对远端队列；Hub 不会自动重提，以免重复生成。
- 关闭页面、停止客户端等待或重启 Hub，不等于取消 ComfyUI 上的生成。首版不通过全局中断去取消其他设备的任务。

## 常见情况

| 情况 | 检查 |
| --- | --- |
| 管理页要求密钥，本机监控却可自动连接 | 管理页需要 `.api-key` 的根密钥，不能使用监控临时会话 |
| Hub 离线 | 页面仍可编辑配置；查看日志，核对独立 Python 环境后启动 |
| Hub 已运行，ComfyUI 服务探测失败 | 核对 Hub 主机上的可执行文件路径、服务 `url` 与远端原服务状态 |
| 另一台电脑连接失败 | 使用 Hub 局域网 IP 与 `8899` 端口，核对设备服务权限和令牌 |
| 工具提示找不到输入文件 | 先上传文件并使用返回的资产 ID，不能直接传客户端本地路径 |
| 保存提示配置已更新 | 重新加载最新配置后再编辑，修订号用于避免互相覆盖 |
| 文件或任务属于别的设备 | 使用创建它的设备授权；新令牌不继承旧设备文件权限 |

管理 API 位于 `/mcp-admin-api/v1`，只接受管理员根密钥与同源浏览器请求。Hub 内部状态接口只供本机网关使用，不作为客户端配置入口。
