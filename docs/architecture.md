# 架构与模块职责

更新：2026-09-13。

## 仓库目录

| 路径 | 内容与维护规则 |
| --- | --- |
| `src/local_llm_deploy/` | 唯一的业务实现位置，按下文模块边界组织 |
| `config/examples/` | 模型、引擎及凭据的可入库样例；真实配置仍在部署根目录 |
| `requirements/` / `constraints/` | 前者引用 `pyproject.toml` 的 extra，后者记录已验证版本，避免多处声明依赖 |
| `tests/core/` | 配置、注册表、权重安装与引擎测试 |
| `tests/gateway/` | 网关发现、路由、调度、转发及断开行为测试 |
| `tests/lifecycle/` | CLI、进程与显式启用的 launchd 测试 |
| `tests/services/` / `tests/smoke/` | 前者使用假模型验证服务契约，后者提供显式运行的真实服务、安装及升级验收 |
| `static/` / `templates/` | 监控前端资源与模型提示模板，各自独立维护 |
| `scripts/` / `tools/` | 维护脚本与运行辅助入口，不承载模型业务实现 |
| `scripts/compat/` | 从根目录迁出的手动命令包装；保留参数用法，不再保留旧根路径 |
| `docs/` / `journal/` | 项目文档与实验记录模板；临时验收产物写入已忽略的 `work_dir/` |

根目录只保留 11 个受版本控制的文件：`README.md`、`pyproject.toml`、`.gitignore`、`.cursorignore`、`manage.sh`、`llm.py`、`bootstrap.py`、`serve-ui.py`、`serve_embedding.py`、`serve_rerank.py`、`serve_whisper.py`。新增业务代码、文档、配置样例和维护脚本均放入对应子目录。

13 个手动入口归入 `scripts/compat/`；4 个根依赖转发文件和 `DEPLOY.md` 已移除，分别直接使用 `requirements/` 与 `docs/deployment.md`。已有 launchd 配置与旧 PID 身份检查引用绝对脚本路径，所以四个 Python 服务入口及 `scripts/start-*.sh` 继续保留原址；后续退役这些入口必须先迁移对应 job 并验证进程归属。

`models.json`、`engines.json`、密钥、权重、PID、日志、虚拟环境与引擎源码/构建是本地部署数据，不是包源码。此次归类只移动可入库模板，不迁移这些运行路径。初始化注册表优先读取 `config/examples/models.json.example`，兼容旧部署根目录的同名模板。

## 运行结构

```text
客户端 / static/monitor.html
             │
             ▼
       gateway :8888
   配置快照、发现、路由
      调度、认证、转发
             │
     ┌───────┼──────────────┐
     ▼       ▼              ▼
 llama.cpp / Ollama    Python 模型服务
                     Embedding / Rerank / ASR
                           │
                           ▼
                      已登记的本地权重
```

网关是单个多线程进程；全局 lane 和每模型 KV 预算均在该进程内生效。多个网关进程之间不共享预算，不应将多 worker 当成无成本扩容方式。网关和 CLI 不导入 Torch、MLX 或模型代码。

## 三种对象

- `ModelSpec`：配置中的逻辑模型，描述 key、alias、capabilities、backend、management 及后端参数。
- `ModelInstallation`：权重的实际安装路径、量化、revision 和完整性。由同一个解析器服务于启动、下载和清单。
- `InstanceObservation`：服务实例的只读观测，包含进程身份、地址、运行方式、健康与状态。启动命令使用 `ServiceSpec`，不把模型定义当作进程状态。

旧 `type` 在 `config.normalize_models` 中映射成独立的能力与后端。`embedding/rerank/asr` 是能力，`ollama/external` 则表示旧配置中的实现或管理方式。所有调用方使用这个统一转换。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `config.py` / `domain.py` | 项目路径、模型校验、共享数据结构 |
| `registry.py` / `storage.py` | 原子注册表更新、缓存、文件锁 |
| `artifacts/paths.py` | 默认路径、安装选择、量化与 GGUF 分片检查 |
| `artifacts/manifest.py` | 安装登记，保留 version=1 清单格式 |
| `artifacts/download.py` / `inventory.py` | 下载计划、执行、安装列表及受保护的删除 |
| `backends/builders.py` | 从模型配置构造 argv、环境、解释器和就绪检查 |
| `lifecycle/` | process / launchd 执行、身份校验、就绪发布、停止、reconcile |
| `gateway/discovery.py` / `routing.py` | 在线后端发现、能力与端点匹配、别名和默认模型 |
| `gateway/scheduling.py` | 原子分配 lane 与模型预算，排队优先级、占用释放 |
| `gateway/transport.py` | HTTP、SSE、保活、有界缓冲、断开与不确定状态 |
| `gateway/auth.py` / `knowledge.py` | 模型 Key 策略与独立知识库凭据转发 |
| `gateway/monitoring.py` / `observability.py` | 资源观测、状态字段、请求 ID、有限日志采集 |
| `services/` | 共享 HTTP 边界及三种独立模型适配器 |
| `engines.py` | 固定版本构建档案、验证、切换与回退 |
| `cli.py` | 对外命令、错误呈现、应用编排 |

根目录的 Python 入口及 `scripts/compat/` 都是薄包装，业务实现位于 `src/local_llm_deploy/`。迁后的手动脚本仍可从任意工作目录调用，Python 包装复用根 `bootstrap.py` 激活本 checkout；安装后的 `local-llm` 命令通过 `--project-root` 或 `LOCAL_LLM_ROOT` 指定部署目录。

## 路由与请求生命周期

1. 校验模型 API 凭据、请求大小与协议。
2. 将路径映射为能力，按 key / alias / 后端 ID 选择明确后端。
3. 验证后端在线、能力及端点声明。
4. 原子申请对应 lane 与模型预算；等待期间保留顺序和流式优先级。
5. 转发请求、保活并返回上游结果，资源只释放一次。

Chat 默认并发 1；Embedding 为 2；Rerank 和 ASR 各为 1。Embedding 内部仍以锁保护 adapter 切换和计算，Whisper/Rerank 也保留模型推理锁。KV 是估算的 token 预算，不是实测显存上限。`queue_depth` 为在途请求数，包含活动请求；监控另行呈现等待数。

下游断开后不等于上游计算结束。传输层会尝试取消/完成清理，无法确认完成的任务保留占用并标记 uncertain。恢复时先确认后端空闲或重启后端，再重启网关；单独重启网关不能证明上游已停止。异常退出后的跨进程占用恢复仍需这个操作顺序。

## 配置和状态

注册表约 30 秒重新校验，成功后原子替换；无效更新保留上一有效配置。已存在模型的并发/KV 上限及网关运行环境采用重启生效，避免活动请求释放到另一份预算对象；新模型可在刷新后注册。

`run/<model>.pid` 前三行仍是 PID、port、alias。新增可选第四行 JSON，记录进程启动时间、命令摘要、管理方式和就绪信息。旧格式以保守身份检查兼容。查询不清理记录；启动成功才发布 ready；停止前确认归属。launchd 的 PID 变化会重新核对其命令身份。

新的 LaunchAgent 启动轻量 `lifecycle.runner`，它复用前台管理器，按安装锁、实例锁的顺序启动后端并独立发布 ready。由登录自启触发时同样服从删除锁；外层 start 只等待对应启动 token 和 supervisor 身份，避免父子争用锁。plist 保存启动配置，权限 0600；运行记录仍只有 PID 文件。无 PID 时可从当前项目拥有的已加载 job 只读观察 starting 状态。

模型权重保留在 `models/`，manifest 明确选择安装路径。同量化多安装不任意选取，需 `--model-dir`。下载、启动和删除使用协调锁，清单更新使用独立读改写锁。外部服务与模型 API 代理的生命周期归属分开。

`/knowledge/` 仅反向代理外部知识库，转发客户端 Authorization；它不是本仓库的 RAG 或知识库实现。
