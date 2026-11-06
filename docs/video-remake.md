# Video Remake 视频工作台

`video-remake` 的项目、成片和审阅页通过网关的 HTTP 应用注册机制挂载到
[http://127.0.0.1:8888/remake/](http://127.0.0.1:8888/remake/)。
网关首页会自动出现 **Video Remake** 入口。原始视频学习库仍位于 `/video/`。

## 安装与启动

需要 macOS、Node.js 22 及以上，以及已有 `cli/web.js` 的 `video-remake` checkout。
在当前网关目录运行：

```bash
python3 scripts/install-video-remake.py --repo /path/to/video-remake
```

默认使用 PATH 中的 Node、回环地址 `127.0.0.1:8786` 和本机网关 `8888`。
可用 `--node /absolute/path/to/node`、`--port 8786`、`--gateway http://127.0.0.1:8888` 覆盖。
脚本会先验证网关认证，安装专用 LaunchAgent，等待健康检查，然后热注册 `/remake/`。
无需重启网关或任何模型。重复运行相同配置会复用已启动的 job；配置不同或端口被其他进程占用时会停止并提示检查。

LaunchAgent 名为 `com.local-llm-deploy.video-remake`，登录时启动、异常退出时重启。
应用在 `video-remake` checkout 中运行，不需要构建，也不复制视频。标准输出与错误日志分别在
`logs/video-remake.stdout.log` 和 `logs/video-remake.stderr.log`。

本机注册记录位于忽略的 `data/gateway-apps.json`。`.api-key` 只在安装脚本内读取并放入注册请求头，
不会写入浏览器、LaunchAgent 或日志。此 HTTP 应用的浏览语义与已有应用一致，网关没有额外增加模型认证。

## 路由契约

| 网关 | 本地应用 | 用途 |
| --- | --- | --- |
| `/remake/` | `127.0.0.1:8786/` | 项目与视频工作台 |
| `/remake/api/health` | `127.0.0.1:8786/api/health` | 健康检查 |
| `/remake/api/projects` | `127.0.0.1:8786/api/projects` | 项目与视频列表 |

网关剥离 `/remake` 前缀，保留查询参数。页面资源和 API 使用相对 URL，因此直连与网关路径均可用。
媒体请求的 `Range`、`HEAD`、`Content-Range`、`Accept-Ranges` 和下载响应头透传，支持视频拖动和下载。
网关自身使用分块响应，不转发上游 `Content-Length`。

## 维护与卸下

```bash
launchctl print gui/$(id -u)/com.local-llm-deploy.video-remake
launchctl kickstart -k gui/$(id -u)/com.local-llm-deploy.video-remake
```

修改服务代码后可用第二条命令重启该应用。此 job 独立于模型生命周期，不参加 `manage.sh stop --all`。

彻底卸下时先删网关入口，再卸载本应用：

```bash
./manage.sh apps remove video-remake
launchctl bootout gui/$(id -u)/com.local-llm-deploy.video-remake
rm ~/Library/LaunchAgents/com.local-llm-deploy.video-remake.plist
```

CLI 删除入口后网关会在下一次注册表刷新时更新。也可以带模型 API Key 调用
`DELETE /gateway-api/v1/apps/video-remake` 立即刷新。
