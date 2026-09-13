# 模型监控控制台

Vue 3 + TypeScript + Vite，读取同源网关的只读监控 API。生产环境由 Python 网关提供 `static/`，不需要 Node、前端开发服务器或 CDN。

## 开发

使用 `.node-version` 指定的 Node 24 和锁文件：

```bash
cd frontend
npm ci
npm run dev
```

打开 `http://127.0.0.1:5174/monitor.html`。开发服务器将 API 请求代理到本机 `8888`。`/monitor.html?demo=1` 使用固定演示数据并显示标识，方便不连接模型进行 UI 开发；演示入口在生产构建中删除。

访问凭据仅存于当前页面内存，刷新页面后重新输入；仅发给同源 `/monitor-api/v1/`。清除凭据会取消请求并清除模型清单、详情、高级输出与趋势。未认证或旧网关提供有限公开摘要；公开接口不保证采集有效性，不应据此判断模型健康。

## 代码组织

| 目录 | 职责 |
| --- | --- |
| `src/api/` | DTO、允许访问的 URL、认证及响应边界 |
| `src/domain/` | 响应验证、五分钟趋势的数据规则 |
| `src/composables/` | 轮询、取消、凭据与连接状态 |
| `src/features/` | 模型清单和模型详情 |
| `src/components/` | 对话框、趋势线、状态标记、输出阅读等共享组件 |
| `src/styles/` | 主题、间距、排版和响应式布局 |
| `tests/fixtures/` | 脱离真实模型的固定数据和浏览器 API 模拟 |
| `tests/unit/`、`tests/components/`、`tests/e2e/` | 数据规则、关键交互、浏览器验收 |
| `scripts/` | 有界静态发布和产物一致性检查 |

新增指标应先明确来源、单位、缺失/过期语义和支持的后端，再修改 Python DTO、TypeScript 类型/验证和对应测试。不直接在 Vue 组件内请求模型端口，也不把服务启动、停止或推理操作加入这个只读控制台。

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
