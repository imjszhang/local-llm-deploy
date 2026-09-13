# 本地模型工作台

Vue 3 + TypeScript + Vite，通过同源网关提供运行监控和模型对话。生产环境由 Python 网关提供 `static/`，不需要 Node、前端开发服务器或 CDN。

## 开发

使用 `.node-version` 指定的 Node 24 和锁文件：

```bash
cd frontend
npm ci
npm run dev
```

打开 `http://127.0.0.1:5174/monitor.html`。开发服务器将 API 请求代理到本机 `8888`。`/monitor.html?demo=1` 使用固定演示数据并显示标识，方便不连接模型进行 UI 开发；演示入口在生产构建中删除。

通过网关本机地址（localhost、127.0.0.1、::1）打开页面时自动获取 8 小时有效的控制台会话，网关在服务端读取 `.api-key`，根 Key 不传入浏览器。通过局域网、反向代理或远程地址访问时仍手动输入 Key。凭据仅存于当前页面内存，刷新后本机自动重连、远程重新输入；仅发给同源 `/monitor-api/v1/`、`/chat-api/v1/sessions` 和 `/v1/chat/completions`。清除凭据会取消请求并清除模型清单、详情、高级输出与趋势，以及内存中的对话会话。未认证或旧网关提供有限公开摘要；公开接口不保证采集有效性，不应据此判断模型健康。

## 代码组织

| 目录 | 职责 |
| --- | --- |
| `src/app/`、`src/App.vue` | 工作区导航、共享监控实例和内存凭据上下文 |
| `src/features/monitor/` | 监控 DTO/验证、轮询、趋势、模型清单与详情 |
| `src/features/chat/api/` | 同源 POST、SSE 增量解析、错误和响应边界 |
| `src/features/chat/domain/` | 会话类型、有效上下文、参数兼容与导出 |
| `src/features/chat/composables/` | 会话及生成请求所有权、停止、迟到响应隔离 |
| `src/features/chat/components/` | 输入、会话栏、参数面板、Markdown 与统计 |
| `src/shared/components/`、`src/shared/styles/` | 凭据弹窗、焦点管理、格式化、主题与通用样式 |
| `tests/fixtures/` | 脱离真实模型的固定数据和浏览器 API 模拟 |
| `tests/unit/`、`tests/components/`、`tests/e2e/` | 数据规则、关键交互、浏览器验收 |
| `scripts/` | 有界静态发布和产物一致性检查 |

新增指标应先明确来源、单位、缺失/过期语义和支持的后端，再修改 Python DTO、TypeScript 类型/验证和对应测试。不直接在 Vue 组件内请求模型端口，运行监控保持只读；推理操作只放在模型对话工作区，不加入模型启停功能。

## 检查与候选构建

```bash
npm run typecheck
npm run lint
npm test
npm run bundle
npx playwright install chromium webkit
npm run test:e2e
```

`bundle` 只写入 `frontend/dist/`；`preview` 在 `127.0.0.1:4174` 提供该目录，不影响运行中的静态页。本机已安装 Chrome 时，可用 `MONITOR_TEST_CHROME=1 npm run test:e2e -- --project=chromium` 验证真实 Chrome；CI 使用 Playwright 的固定 Chromium 和 WebKit。

候选发布可以指定独立目录，执行后在项目根运行实际 Python 静态服务测试：

```bash
node scripts/export-static.mjs --target ../work_dir/monitor-validation/candidate-static
cd ..
LOCAL_LLM_TEST_STATIC_DIR=work_dir/monitor-validation/candidate-static python -m unittest tests.gateway.test_monitor_static -v
```

## 发布

完整实施、验收和回退约束见 [监控实施计划](../docs/monitor-ui-plan.md)。确认后端候选及页面验收通过后执行：

```bash
cd frontend
npm run build
npm run build:check
```

构建脚本在资源预算检查通过后，只同步 `monitor-assets/`、`monitor-manifest.json`、`monitor.html`，最后原子替换 HTML，不清空 `static/`。预算为 gzip JavaScript ≤200 KB、CSS ≤40 KB。hash 资源保留旧文件，以便已打开的页面完成请求；在下一次维护窗口按保留版本清单清理，不在普通构建时删除。

源码、锁文件和 `static/` 产物一起提交。`build:check` 重建并校验字节及 manifest，CI 不接受缺失或过期产物。部署单位是 Python 包加项目目录，单独安装 wheel 不包含整个 UI 与部署配置。

## 模型对话

入口为 `/monitor.html#/chat`。监控模型详情内的“进入对话”可预选模型。聊天模块懒加载，工作区导航不刷新页面，凭据和会话保持在当前应用内存中。

- 每页同时生成一个请求；切换会话或工作区不停止生成。停止接收使用浏览器取消，后端是否结束仍由网关判断。
- 温度、Top P、最大输出 token 留空时不发送。Seed 和流式 usage 开关仅为已验证的 llama.cpp/Ollama 开放。
- 思考开关、强度和预算由模型注册表 `chat_controls` 显式声明，缺失时禁用控制。两种后端的字段转换见 [思考控制](../docs/chat-reasoning.md)。切换模型会重置思考设置，已有请求快照保持不变。
- 多轮请求仅携带选中的完整答案，停止/失败部分答案默认排除，可明确采用。重新生成保留版本，编辑最后用户消息移除其旧答案。
- 模型与 System Prompt、参数及上下文在每次发送时冻结。JSON 导出带 schema_version，包含请求快照和答案版本；Markdown 导出当前选中答案。
- Markdown 禁止原始 HTML，不加载远程图片；代码高亮覆盖 JavaScript、Python、JSON、Bash，其他语言以安全纯文本显示。代码块有键盘可操作的复制按钮。
- 单请求/响应页面上限 8 MiB，单 SSE 事件 1 MiB，单次生成页面时限 15 分钟。超限明确失败，不自动重试。
- 首内容延迟包括排队、加载、网络与模型处理。端到端 token/s 使用后端输出 token 数除以完整请求耗时，不代表解码速度。没有 usage 时显示“未提供”。

完整边界与验收见 [对话实施计划](../docs/chat-ui-plan.md)。会话自动保存到网关本机 SQLite，刷新后恢复；保存状态、访问范围及备份见 [会话存储](../docs/chat-history.md)。凭据不持久化。

### 本机自动连接

`POST /console-api/v1/session` 签发仅限监控与对话的临时内存令牌。服务器严格校验 socket 来源为 loopback、Host 与服务端监听端口、同源 Origin、`X-Local-Console: 1`，并拒绝转发头和跨站请求。监控 GET 的无 Origin 情况需要 `Sec-Fetch-Site: same-origin`。所有返回禁止缓存；令牌不写 localStorage、sessionStorage、Cookie 或静态文件。

会话只允许监控 API v1 GET/HEAD、`/v1/chat/completions` POST 和历史 API 的限定 GET/PUT/DELETE，不能用于其他模型 API 或知识库代理。根 Key 修改、网关重启或 8 小时到期都会使会话失效；刷新页面或访问设置中的“自动连接本机”可重新连接。清除凭据后本页不会自行重连。开发服务器代理端口不同，不符合直连检查，开发时仍可手动输入 Key。

聊天视觉使用局部灰白/靛蓝主题，不影响监控数据读取与原 API 行为。
