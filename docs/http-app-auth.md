# HTTP 应用的统一认证

自行注册的 `kind=http` 应用可显式声明 `auth: {"gateway":"console","upstream":"signed-v1"}`。未声明的应用保持原来的透明转发方式。需要配置网关 API Key；缺少密钥时失败关闭。

挂载路径下的 `/api/v1/gateway-session` 支持 GET 查询模式、POST 创建会话、DELETE 撤销当前会话。本机自动会话复用 `ConsoleSessions.local_origin` 的实际 loopback、Host、Origin、显式请求头和无反向代理边界。远程用户可用网关 Bearer 凭据创建会话。无效显式凭据不会降级为本机自动认证。

浏览器会话仅限当前应用和访问来源，HttpOnly / SameSite=Strict，路径限定在挂载目录，12 小时后失效。网关重启、密钥轮换或退出都会使对应会话失效；不授予模型或其他应用权限。写请求须带 `X-Gateway-App-Request: 1`（或显式 Authorization），并通过同源检查。

网关不透传 Authorization、Cookie 或调用方的 `X-Gateway-App-Authorization`，仅对已经认证的请求签发上游授权。静态入口 `/`、`/index.html`、`/app.js`、`/style.css` 可匿名加载；业务接口与媒体必须认证。前端自行通过 session API 启动认证。

上游验证密钥为 HMAC-SHA256(root gateway key, UTF-8 的 `js-eyes-mounted-app-v1\n<app key>\n<prefix>\n<upstream origin>`)，使用原始 32 字节结果。上游只需保存应用专用密钥，无需保存网关管理密钥。注册响应返回 auth 能力；轮换网关密钥后需重新关联以更新上游验证密钥。

请求头格式为 `X-Gateway-App-Authorization: v1.<unix seconds>.<32 hex nonce>.<64 hex signature>`。签名消息由 `v1`、应用 key、prefix、HTTP 方法、上游完整 path/query、时间、nonce、原始请求体 SHA-256 十六进制摘要用换行连接。使用应用专用密钥 HMAC-SHA256 签名。上游必须严格校验时间窗、重复 nonce、应用绑定、方法、路径和请求体；JS Ad Collector 实现 ±30 秒时间窗和拒绝重放。凭据不得写入日志或公开 API。
