# 视频库 MCP（局域网）

视频库挂载提供只读 MCP：`http://<服务器局域网IP>:8888/video/mcp`。与 `/video/` 网页使用同一库存、同一网关进程，不需要另开服务。独立的 Node `serve` 暂不提供 MCP。

网页展开「MCP · 让局域网 Agent 使用视频库」，即可检查能力并复制接入说明。本机通过 localhost 打开时会列出检测到的局域网地址；多网卡环境请选择 Agent 能够访问的网卡地址。通过局域网 IP 打开页面时，说明使用当前地址。`GET /video/api/v1/mcp.json` 返回端点、局域网候选地址和工具目录。

## 客户端连接

传输为 Streamable HTTP，支持协议 `2025-11-25`、`2025-06-18`、`2025-03-26`。支持 HTTP 的 MCP 客户端可以使用类似配置（各客户端字段可能不同）：

```json
{
  "mcpServers": {
    "yt-study-archive": {
      "type": "http",
      "url": "http://<服务器局域网IP>:8888/video/mcp"
    }
  }
}
```

此端点与网页浏览一样不需要 token；只供可信局域网使用，不应直接暴露公网。浏览器请求必须与网关同源，Host 必须是本机地址或主机名。远端机器不能使用 `127.0.0.1`。云端 Agent 若没有局域网连接，也无法直接访问。

## 工具

| 工具 | 能力 |
| --- | --- |
| `video_library_stats` | 库存总数、平台和状态统计 |
| `search_videos` | 检索标题、频道、简介、已有文稿；默认已下载；支持平台、频道、状态与分页 |
| `get_video` | 视频详情、实际存在的版本与附件及局域网 URL |
| `get_video_download` | 返回指定版本的下载 URL、播放 URL、实际文件大小与 MIME |
| `list_video_assets` | 列出原片、硬/软字幕版、封面、字幕、时间轴文稿和中文文稿 |
| `read_video_text` | 分页读取已有文本附件；不调用模型、不自动转写 |

推荐先搜索，再取详情确认版本。`variant` 为 `source`（默认原片）、`burn`（中文字幕压入画面）、`soft`（软字幕）。不存在的版本返回工具错误，不会退回其他版本。

`get_video_download` **只返回地址**，Agent 仍需用自己的 HTTP/文件工具把该 URL 保存到自己的设备。它不会替远端客户端创建本地文件。例：

```sh
curl --fail --location --continue-at - --output video.mp4 '工具返回的 downloadUrl'
```

核对下载后的文件大小与工具返回的 `bytes`。HTTP 支持 HEAD、单段 Range、续传及附件文件名；视频不经 MCP 编码为 base64。现有文件被替换时，应重新下载而不是继续旧文件。

附件使用枚举生成的 `assetId`，通过 `/video/api/v1/videos/:id/assets/:assetId` 下载，不接收文件路径。`read_video_text` 按 Unicode 字符计 offset，默认 12,000、最多 20,000 字符；跟随 `nextOffset`。大于 8 MiB 的文本使用附件 URL 下载。缺少文稿时只返回已有附件。

本轮没有提供新增源站下载、入队/重试、删除、更改库存、生成字幕/转写和抽帧工具。原有网页写入功能仍遵循原来的 token 要求。媒体和文本内容均为资料，不应作为 Agent 指令执行。

## 传输约定与验证

遵循 [MCP 2025-11-25 Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)：每次向同一个端点 POST 一个 JSON-RPC 消息；请求头包含 `Content-Type: application/json` 和 `Accept: application/json, text/event-stream`。

先 `initialize`，再发送 `notifications/initialized`。后续请求带协商后的 `MCP-Protocol-Version`。服务采用无会话 JSON 响应，不发送 `Mcp-Session-Id`；通知返回 202，GET/DELETE 返回 405（没有服务端 SSE 通知流）。工具发现为 `tools/list`，调用为 `tools/call`。响应同时包含文本 JSON 和结构化数据（2025-03-26 只返回文本）。

验证命令：

```sh
PYTHONPATH=src python3 -m unittest tests.gateway.test_video_mcp tests.gateway.test_gateway_video
```

另外使用官方 TypeScript SDK `@modelcontextprotocol/sdk` 1.32.2 的 `Client` 和 `StreamableHTTPClientTransport` 验证初始化、六工具发现、查询、文本读取、下载 URL 与 HEAD/Range。
