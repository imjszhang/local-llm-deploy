# 开发与测试

本项目是轻量 Python 控制层加独立模型进程。CLI 和网关不导入 PyTorch、MLX 或模型权重；Embedding、Rerank、Whisper 保留自己的运行环境。接口保留与主动变化见 [compatibility.md](compatibility.md)，上线切换见 [migration.md](migration.md)。

## 本地开发环境

支持 Python 3.9 及以上。只维护 CLI、网关、配置或生命周期时，无需安装模型依赖：

```bash
python3 -m venv .venv-dev
.venv-dev/bin/python -m pip install 'pip>=23'
.venv-dev/bin/python -m pip install -e '.[dev]' -c constraints/dev.txt
.venv-dev/bin/python -m unittest discover -s tests -t . -v
.venv-dev/bin/python -m ruff check src tests scripts/compat
```

`llm.py`、根目录服务入口与 `scripts/compat/` 中的手动入口通过根 `bootstrap.py` 支持源码 checkout；可安装包提供 `local-llm` 和 `python -m local_llm_deploy`。从其他目录调用安装后的命令时，显式设置 `--project-root` 或 `LOCAL_LLM_ROOT`。测试应先安装包，不依赖手动修改 `PYTHONPATH`。

模型环境的依赖安装、约束文件及引擎升级见 [upgrade.md](upgrade.md)。普通启动和下载不会隐式执行 pip 安装或升级。

## 模块边界

| 模块 | 负责什么 | 不应包含什么 |
| --- | --- | --- |
| `domain.py` | `ModelSpec`、`ModelInstallation`、领域值 | HTTP、进程启动、模型导入 |
| `config.py`、`registry.py` | 根目录、旧配置兼容、校验、注册表原子更新 | Shell 字符串解析、模型加载 |
| `artifacts/` | 安装选择、分片检查、下载、manifest、删除保护 | 对服务进程发送信号 |
| `backends/builders.py` | 从模型、配置和安装生成 `ServiceSpec`，保留 argv 的参数边界 | `Popen`、`launchctl`、PID 文件修改 |
| `lifecycle/` | 只读观察、进程身份、同实例锁、端口归属、ready 等待、启动/停止、plist | 引擎特定推理参数分支 |
| `cli.py` | 参数解析、调用编排、退出码、文本或 JSON 输出 | 独立配置加载实现、内嵌 Shell 业务逻辑 |
| `gateway/` | 发现、能力路由、调度、HTTP 转发、认证、监控、知识库代理 | 下载模型、直接管理推理环境 |
| `services/` | 独立辅助模型服务及小范围公共 HTTP 封装 | 网关全局排队、接管其他服务 |
| `engines.py` | 已验证引擎档案、显式构建、升级和回退引用 | 启动时自动拉取代码 |

`lifecycle.types.ServiceSpec` 是后端适配器与运行适配器之间的契约，包含 argv、cwd、环境、日志、地址、就绪路径、超时、运行方式及所选权重/量化/引擎。只通过 argv 列表构造命令，禁止将参数拼成 Shell 执行字符串。

计划中的运行实例概念实际由 `lifecycle.types.InstanceObservation` 表达，包含进程身份、地址、归属、状态和实际权重路径。CLI 与网关共用这一结构；不另外维护一个未被使用的 `domain.ServiceInstance` 副本。

共享观察入口为 `lifecycle.observe.observe_instances(paths, specs, probe=False)`。CLI 和网关应使用同一结果，不能另写 `pgrep` 或 PID 存活检测。`probe=True` 才额外执行健康 HTTP 请求；网关会结合自己的健康探测决定可路由后端。

## 新增同后端模型

1. 在独立 JSON 文件中定义新模型键、alias、来源、默认端口、参数；同类模型可沿用旧 `type`。需要作为未指定 model 时的默认目标时，显式设置 `default_for`。
2. 用 `./manage.sh registry merge FILE` 合并，再运行 `./manage.sh config validate`。键和别名应唯一；为可并行运行的本地实例分配不同端口。
3. 用 `download` 获取权重，或用 `register MODEL --path PATH [--quant Q]` 登记已有安装。多个同量化安装用 `--model-dir` 明确选择。
4. 运行 `./manage.sh plan MODEL` 检查模型键、权重、量化、引擎、解释器、host/port、LaunchAgent label 和重启策略。
5. 在独立端口完成该后端的小样本测试后切换调用方。无需修改通用 Shell、CLI 或网关的模型名分支。

模型的 `runtime.python` 可指定独立解释器；未指定时按后端查找 `.venv-embed`、`.venv-rerank`、`.venv-whisper` 及轻量 fallback。解释器选择在后端适配器统一完成。Jina/Whisper 包装脚本仅提供兼容默认名称，不替换 CLI 已选择的模型键。

## 接入新后端

先声明真实支持的 `capabilities` 和 `endpoints`。支持 Chat 不代表支持 Responses 或 Messages；不要通过“排除其他模型类型”的方式推断能力。

本地后端需要在 `backends` 增加启动描述构造，并复用 `process` 或 `launchd` 执行器。远端或外部托管服务以 `management: external` 连接，不参加 `stop --all`。协议不兼容的部分放在对应网关或服务适配器，避免把后端分支继续添加到 HTTP handler。

新后端测试至少覆盖：配置规范化、参数边界、真实支持的端点、响应/错误协议、就绪判定、取消是否能确认完成。先用假服务完成契约测试，真实权重测试作为单独步骤。

## 状态所有权与锁顺序

`run/<key>.pid` 是唯一持久化实例记录。前三行保留 PID、端口和 alias；第四行是可选 JSON，保存启动时间、命令摘要、管理方式、就绪、权重/量化和引擎信息。旧记录读取后只在内存中规范化。`status` 和 `list` 不创建目录、不删除失效 PID、不修改 plist。

管理器启动时设置 `LOCAL_LLM_MANAGED_INSTANCE=1`。子服务不写删 PID；管理器在 HTTP ready 且监听 socket 归属于所启动进程后原子发布记录。独立 Python 服务入口在实例锁内完成加载、bind 与发布，发布后释放锁；退出时只能清理自己的 PID。前台 CLI 保留父进程，转发信号、回收子进程并清理记录。

新的 LaunchAgent 启动通用 `lifecycle.runner`。无论 CLI 显式启动还是 RunAtLoad/KeepAlive 自动启动，都由 runner 按同一安装锁和实例锁顺序启动后端、检查 ready 并发布后端 PID。CLI 只在短暂实例锁内安装及 bootstrap，释放锁后等待启动 token 与 supervisor 身份匹配的记录，不与 runner 双写。后端与 runner 共享 launchd 进程组，退出清理不会留下脱离 job 的子进程。完整启动描述保存在权限 `0600` 的 plist 中；PID 与观察 API 只保存认证文件路径和 `file/inline/none` 来源标记，不保存密钥。

修改锁必须遵循现有顺序：

1. 权重操作先获取 `run/.artifacts.lock`。启动与下载持共享锁，删除持独占锁。
2. 启动与下载再获取按安装绝对路径生成的锁，位于 `run/artifact-locks/`。
3. 启动最后获取 `run/.<key>.lock`；持有至成功发布 ready 或失败清理完毕。停止仅获取该实例锁。
4. 注册表和 manifest 的读改写通过各自存储锁与原子替换完成。不要在持有实例锁时反向申请权重独占删除锁。

独立 Python 服务入口也依次获取安装锁和实例锁，发布后释放两者，并把显式选择的真实权重路径写入同一 PID 元数据。由管理器启动的子服务跳过这些写入和重复加锁。不要把阻塞的全局推理排队锁引入生命周期操作。

旧 PID 只能根据绝对入口路径、模型参数及已知 launchd 归属进行保守识别。新记录额外检查启动时间与命令摘要，每次发送信号前都重新核对。无法确认归属时不把 PID 当作可安全终止的目标；只记录诊断信息。

## 测试分层

测试按功能放入 `tests/core/`、`tests/gateway/`、`tests/lifecycle/` 和 `tests/services/`，独立验收脚本放入 `tests/smoke/`。新增测试跟随被测模块归类；跨测试共享夹具用 `tests.<分组>.<模块>` 导入。运行发现命令时以项目根目录为工作目录，使用 `-t .` 保持包名一致。

普通 `unittest discover` 使用临时目录、临时端口、假模型和子进程，不能读真实密钥或改动常驻服务。主要测试可独立运行：

```bash
python -m unittest discover -s tests/core -t . -v
python -m unittest discover -s tests/lifecycle -t . -p 'test_cli*.py' -v
python -m unittest discover -s tests/lifecycle -t . -p 'test_lifecycle*.py' -v
python -m unittest discover -s tests/gateway -t . -p 'test_gateway*.py' -v
python -m unittest discover -s tests/services -t . -p 'test_services*.py' -v
```

macOS 的专用 launchd 测试是显式启用的例外：

```bash
LOCAL_LLM_TEST_LAUNCHD=1 python -m unittest discover -s tests/lifecycle -t . -p test_launchd_smoke.py -v
```

该测试只创建 UUID 命名的测试 LaunchAgent 和临时 HTTP 后端，验证 bootstrap、ready、异常退出恢复、stop 和卸载，最终清理测试 job/plist。它不会重启已注册模型，也不等于已验证用户重新登录后的系统行为。

真实模型冒烟使用 `scripts/test-services.sh`，按模型顺序串行执行，具体参数见 [compatibility.md](compatibility.md)。不能把真实流量镜像到两个各自持有调度器的网关，否则会绕过实际全局并发预算。
