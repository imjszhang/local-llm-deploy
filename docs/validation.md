# 重构验收记录

日期：2026-09-13。基线代码：`ecfe929c68ffc7b3b4f9d0e02dfaa2573f573e03`。实现分支：`codex/refactor-maintainability`。

本轮验收区分自动化契约、真实模型调用和实际服务切换。常驻服务沿用原 PID；权重、`models.json`、API Key、已有 LaunchAgent 和引擎构建保持原位。本轮没有进行常驻服务上线切换。

## 自动化契约

普通测试使用临时目录、临时端口、假后端和自有子进程，不加载模型，不向真实服务发请求。覆盖：

- 旧注册表映射、参数类型/优先级、冲突、无效刷新保留快照、原子并发更新。
- 默认/显式/登记路径、多量化多安装、嵌套 GGUF 与 Safetensors 分片、共享/配置路径保护。
- CLI 与旧包装入口、不同 cwd、独立模型实例、端口归属、ready 后发布、重复操作、PID 复用、失败清理。
- JSON、SSE、multipart、Chat/Embedding/Rerank/ASR、Ollama、知识库代理、方法限制和认证。
- 排队/预算、未知模型/默认模型、超时/断开、有界缓冲、未确认后端占用。
- 三个推理服务的 HTTP 契约、锁、错误、模型 ID、FFmpeg 入口。
- 引擎干净 Git 根目录、不可变名称、程序摘要、重复选择幂等与回退。

复跑入口：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv-test/bin/python -m unittest discover -s tests -v
.venv-test/bin/python -m ruff check src tests
```

本机覆盖 Python 3.9.6 和 3.14.4。CI 已配置 Ubuntu/macOS、Python 3.9/3.14、固定版本 Ruff、pip check、compileall 与无模型测试；尚未推送，因此不宣称远端 CI 已通过。

最终两种 Python 的普通发现均为 152 项：148 项通过，4 项明确启用的 launchd 测试跳过；这 4 项另在专用 macOS 验收中通过。Ruff、16 个 Shell 入口语法及监控 JavaScript 语法检查通过。

旧版网关的 5 个保留契约也可复跑：设置下面的精确 commit 时，测试从本仓库提取旧入口到临时目录并注入假注册表、假后端和假知识库；未设置时相同断言测试新包。旧版基线 5 项通过。

```bash
LOCAL_LLM_TEST_BASELINE_COMMIT=ecfe929c68ffc7b3b4f9d0e02dfaa2573f573e03 \
  .venv-test/bin/python -m unittest discover -s tests -p test_gateway_contract.py -v
```

## 干净源码与依赖环境

`tests/smoke/clean_install.py` 从当前 Git 文件清单建立临时源码副本，排除忽略的配置、密钥、权重、环境和运行状态；构建 non-editable wheel 并安装到新的虚拟环境。从另一个 cwd 检查 installed CLI、旧入口帮助、样例注册表与轻量导入。检查确认包来自新环境的 `site-packages`，其中没有安装/导入 Torch、MLX、Whisper、Transformers 或 Hugging Face 库。

```bash
.venv-test/bin/python tests/smoke/clean_install.py \
  --wheelhouse work_dir/refactor-wheelhouse \
  --report work_dir/refactor-clean-install.json --run-tests
```

该轮安装/命令检查及完整无模型测试通过。报告记录精确 argv、cwd、源码 SHA256、wheel SHA256 和测试输出。该脚本要求本地 wheelhouse；正常联网安装流程见 [DEPLOY.md](../DEPLOY.md)。

最终还通过 `--run-tests --launchd-tests` 对已安装 wheel 的模块入口完成相同 148 项常规测试与 4 项真实 launchd 测试，详见本机 `work_dir/refactor-clean-launchd-install.json`。首次安装模式验收暴露端口预检误判 TIME_WAIT、导致 KeepAlive 延迟重启；现已加入确定性回归并修正预检，继续拒绝活跃监听器。修复后 4 项平台测试共 13.153 秒全部通过。

| 新环境 | Python | 验证 |
| --- | --- | --- |
| `.venv-embed-next` | 3.9.6 | 固定约束安装、pip check、帮助入口、真实推理通过 |
| `.venv-rerank-next` | 3.14.4 | 固定约束安装、pip check、帮助入口、真实推理通过 |
| `.venv-whisper-next` | 3.14.4 | 固定约束安装、pip check、帮助入口、真实推理通过 |
| `.venv-download-next` | 3.9.6 | 固定约束安装、pip check、两个下载 SDK 导入、无写入计划生成通过 |

三个环境从空 venv 安装完整 wheel，未复制原 `site-packages`。由于网络较慢，复用了 pip 缓存中的原始 wheel；本机 `work_dir/refactor-wheelhouse/cache-provenance.json` 记录其来源及 SHA256。约束基于对应 extra 的传递依赖闭包，避免混入原环境中无关包。原 `.venv-rerank` 中既存的无关 `mlx-whisper` 依赖缺失没有被带入新 Rerank 环境。

`scripts/snapshot-dependencies.py` 已分别从新环境再生成闭包，与入库 Download 30、Embedding 27、Rerank 34、Whisper 36 个固定版本完全一致。

下载组补齐了缓存中缺少的精确版本 wheel，再离线安装候选环境；未下载模型。系统 Python 3.9.6 的 SSL 为 LibreSSL 2.8.3，导入时出现 urllib3 的 `NotOpenSSLWarning`，这是当前解释器与既存依赖组合的限制。本轮未修改系统 Python，包安装/SDK 导入通过不代表所有远端 TLS 下载路径已验收；升级下载解释器时应重新生成对应约束并验证实际来源。

核心现有 Python 3.9 环境的 pip 已显式更新到 25.3，并安装轻量 editable 包；三个原推理环境保留。`tools/ffmpeg` 从约 49 MB 平台二进制改为调用当前 Whisper 环境 `imageio-ffmpeg` 的 launcher；原和候选 Whisper 环境均通过 `-version`（FFmpeg 7.1）与参数/exec 行为检查。

## 真实模型

`tests/smoke/real_stack.py` 使用私有临时项目根、独立注册表/PID/日志和备用端口，分别启动三个候选服务，再启动新网关。Chat 串行连接现有 Qwen 后端；运行前检查原网关无活动或等待请求。未镜像用户流量。

```bash
.venv-test/bin/python tests/smoke/real_stack.py --run \
  --embedding-python .venv-embed-next/bin/python \
  --rerank-python .venv-rerank-next/bin/python \
  --whisper-python .venv-whisper-next/bin/python \
  --chat-model qwen3.8-27b-aggressive \
  --report work_dir/refactor-validation/real-stack.json
```

16 项检查通过：Embedding health、三个任务与维度、发现和网关 Embedding；直接/网关 Rerank；直接/网关 Whisper 的 json、text、verbose_json；Chat JSON 与 SSE。音频由系统 `say` 生成短句。报告 `exit_code=0`、`cleanup_complete=true`；临时进程已退出。测试验证协议、维度、字段及完成情况，不代替模型质量或性能 benchmark。

首次清理遇到 process 与另一 checkout 同名 LaunchAgent 的判断问题，安全检查拒绝误停。修复后重跑全部 16 项与正常清理通过；早先临时进程也按精确 PID/命令身份清理。原服务 PID 未改变。

## 真实升级与回退

`tests/smoke/real_upgrade.py` 从指定旧 commit 读取 Embedding 源码，串行执行：

1. 旧代码 + 原 `.venv-embed`。
2. 新包 + `.venv-embed-next`。
3. 旧代码 + 原 `.venv-embed`，恢复验证。

三个阶段使用同一备用端口及原权重绝对路径。每次先结束自己的进程、确认端口释放，再开始下一阶段。text-matching、retrieval.query、retrieval.passage 的维度、数值有效性和 token 字段全部通过；新版与回退后的向量相对基线最小 cosine 为 1.0。

```bash
.venv-test/bin/python tests/smoke/real_upgrade.py --run \
  --baseline-commit ecfe929c68ffc7b3b4f9d0e02dfaa2573f573e03 \
  --old-python .venv-embed/bin/python \
  --new-python .venv-embed-next/bin/python \
  --report work_dir/refactor-validation/real-upgrade.json
```

这验证真实代码/环境替换和回退，不是 llama.cpp 版本升级。引擎档案切换和回退用独立 Git 夹具验证。本机 Qwen 使用没有独立 Git 元数据的 `work_dir/llama.cpp-qwen38`，根目录 `llama.cpp` 另有既存修改，二者不能被登记为可信 commit 的干净构建。本轮没有重编译、重写或替换它们。迁移时保留明确 `CPP_DIR`；确认源版本、建立新构建并完成真实推理后才能切换具名档案。

## macOS 生命周期与边界

专用测试通过 UUID label 创建并清理测试 LaunchAgent，验证 bootstrap/RunAtLoad、ready、SIGKILL 后 KeepAlive 恢复、旧包装入口、安全停止和卸载；覆盖没有 PID 文件时的自动启动，以及持删除锁时禁止开始模型加载/发布 PID。源码入口与已安装 wheel 的模块入口均通过。它等价验证本进程可触发的 launchd 行为，但没有让用户注销或重启机器。

```bash
LOCAL_LLM_TEST_LAUNCHD=1 .venv-test/bin/python -m unittest discover \
  -s tests -p test_launchd_smoke.py -v
```

知识库通过假后端验证路径、Authorization、多 Cookie 和 HTTP 行为，未使用真实知识库账号。当前系统的 Ollama 只读发现正常；没有升级 Ollama/DS4。生产长请求、负载压测和实际用户登录后的系统恢复仍按迁移手册单独验收。

后端计算是否停止不能仅通过客户端断开推断。无法确认的请求保留预算并显示 uncertain，恢复顺序是确认/停止后端再恢复网关。调度状态只属于一个网关进程，异常进程退出后的状态恢复不是本轮引入的跨进程协调能力。

本机原服务最终检查均就绪：Embedding 15895、Rerank 15965、Whisper 2127、Qwen 23358、网关 2100；Ollama 外部模型可发现。这里只记录核对时状态，不把这些 PID 当作将来可直接发送信号的授权依据。
