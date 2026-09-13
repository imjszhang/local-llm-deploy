# 迁移与受控切换

本轮替换内部实现，保留部署目录里的 `models.json`、模型键、alias、默认端口、权重目录、日志入口及原脚本路径。完整协议和主动行为变化见 [compatibility.md](compatibility.md)，日常开发见 [development.md](development.md)。修改磁盘代码不会使已经运行的 Python 进程自动切换到新实现；新实现从相应进程下一次启动生效。

## 切换前核对

先检查轻量包与配置，不执行全量服务重启：

```bash
python llm.py config validate
python llm.py list --json
python llm.py status --json
python llm.py plan MODEL
```

`list/status/plan` 不会启动模型、下载权重或重写 LaunchAgent。`status --probe` 会增加只读健康请求。确认 plan 的量化、权重路径、解释器、监听地址及引擎后，才对该实例执行 stop/start。模型环境需要可以导入本项目的轻量包；依赖环境的维护安装按 [upgrade.md](upgrade.md) 操作，不在普通启动中自动安装依赖。

旧入口继续有效：`manage.sh`、`deploy.sh`、`serve-ui.sh`、`jina.sh`、`whisper.sh`、`ds4.sh`、`monitor.sh` 和 `scripts/start-*.sh` 均将参数转给 Python CLI。自动化解析应使用 `list --json`、`status --json` 和 `plan`，不要依赖旧彩色表格或错误文案。拼错的参数现在明确报错，不再被静默忽略。

## 保存现用引擎引用

历史上 `CPP_DIR` 可以指向临时或实验构建，运行进程未必使用根目录 `llama.cpp`。本轮只读核对发现当前 Qwen 实例使用 `work_dir/llama.cpp-qwen38/build/bin/llama-server`；这是本机核对结果，不是所有 checkout 的默认值。普通 plan 不会从运行进程反向猜测要使用的引擎。

保持现用构建的最直接方式是显式传目录：

```bash
./manage.sh plan qwen3.8-27b-aggressive --cpp-dir work_dir/llama.cpp-qwen38
```

需要建立可维护引用时，先核对该目录的 Git 状态与版本，再登记已有构建：

```bash
python llm.py engine register current --directory /path/to/clean-git-checkout
python llm.py engine verify current
```

登记要求目录本身是干净 Git checkout、可执行文件存在，记录源码 commit 和程序摘要；不自动清理未提交修改。随后在模型配置中设置 `"engine_profile": "current"`，或在 plan/start 使用 `--engine current`。也可以用 `engine use NAME` 切换全局默认，但模型自己的档案和显式 `CPP_DIR`/`--cpp-dir` 覆盖仍优先。不要仅凭 `engine use` 成功就认为下一次所有模型都会使用该引擎，应再次检查各模型 plan。

本机当前 `work_dir/llama.cpp-qwen38` 没有独立 Git 元数据，根目录 `llama.cpp` 另有既存未提交改动；不能把祖先仓库的 commit 当成引擎版本，也不能直接登记它们为可复现构建。现有 Qwen 保留 `--cpp-dir work_dir/llama.cpp-qwen38`；待确认其源版本后，在新目录按固定 commit 构建、实际推理验证再登记。此轮未改动上述源码与构建目录。

引擎源码有本地修改时，保留原目录并继续显式 `--cpp-dir` 使用；先将变更整理成可追溯版本，再登记档案。不要为了通过登记检查删除用户未提交的修改。

## 逐实例切换

1. 保存当前代码版本、配置副本、依赖环境和现用引擎引用；如果将更新 plist，同时保存既有定义。保留旧权重和旧构建。
2. 对模型生成 plan 并检查旧客户端是否显式传 model。没有 model 的请求现在需要 `default_for` 或相应 `DEFAULT_<CAPABILITY>_MODEL`，未知模型会返回 404；不再任意回退。
3. 等待当前推理结束，明确停止目标实例，再按已审核的参数启动。同一实例一次只由一个 supervisor 管理。
4. 用 `status MODEL --probe` 检查 ready，再执行相应的一个真实小样本；确认 Chat SSE、Embedding 维度/task、Rerank 排序或 Whisper 输出格式。
5. 模型后端逐项完成后，受控重启网关，验证 `/v1/models`、监控页、认证与知识库调用。真实模型测试串行，避免两个网关同时对相同硬件独立分配并发。

不要执行“覆盖 PID 文件然后继续”的切换。若端口仍被占用，启动会报错并保留占用者；查明实例归属、确认服务退出后重试。停止操作核对 PID、启动时间及命令身份；旧记录无法确认归属时，应核对原启动方式和进程，不用宽泛的 `pgrep`/`kill` 替代诊断。

## launchd 与前台运行

既有默认 label 保留：

| 服务 | label | 原有策略 |
| --- | --- | --- |
| Jina Embedding | `com.local-llm-deploy.jina-embed` | 登录加载；异常退出自动恢复 |
| Jina Rerank | `com.local-llm-deploy.jina-rerank` | 登录加载；异常退出自动恢复 |
| Whisper | `com.local-llm-deploy.whisper` | 登录加载；不自动重启 |
| 网关 | `com.local-llm-deploy.serve-ui` | 登录加载；不自动重启 |
| DS4 | `com.local-llm-deploy.ds4` | 显式启动；不自动重启 |

新增同类模型使用 `com.local-llm-deploy.model.<key>`，不会复用 Jina/Whisper 默认实例的 label。`runtime.run_at_load` 与 `runtime.keep_alive` 可显式配置策略。`status` 不重装 plist；已加载服务必须先 stop，才能更新其定义。安装新定义时，已有 plist 保存为同目录 `.plist.bak`。

新定义通过通用 Python runner 启动后端，保留原 label、RunAtLoad 和 KeepAlive 策略。runner 在加载前取得安装锁，ready 后才写 PID，因此登录自动启动也遵循删除保护。已安装的旧定义不会自动重写；受控 stop 后再 install/start 才启用新 runner。安装和卸载只接受属于当前项目的 plist；其他 checkout 使用同名 label 时会明确报错。plist 与备份权限为 `0600`。

`foreground`、`fg` 和旧 `scripts/start-*.sh` 由前台 Python 管理进程传递信号与清理子进程。`nohup` 仍选择普通后台进程；macOS 默认服务保持原 launchd 方式。其他平台使用 `--management process`，调用 launchd 命令会给出明确错误。

DS4 仍只在用户显式调用 `ds4.sh` 时管理；注册表中的 external/Ollama 条目不参加 `stop --all`。DS4 启动现在要求显式 `DS4_ROOT`，不再猜测某位开发者的绝对个人目录。

## 实例状态和清理

`running` 表示归属可确认且进程存活；旧三行 PID 通常先显示这个状态。`ready` 表示新启动已通过就绪门槛，或本次 `--probe` 确认服务可用。`stale` 表示 PID 记录无效、进程消失或身份不匹配。配置中的外部服务没有本项目 PID，普通 status 显示 `external`，`--probe` 可检查其可用性。

拥有当前项目描述的 launchd job 在尚未发布后端 PID 时可只读显示 `starting`，包含实际安装路径；不会因为自动启动暂时缺少 PID 文件而被当作已停止。独立后端凭据使用 `API_KEY_FILE` 或 `runtime.api_key_file`，其路径会传给共享观察与网关；瞬时 `API_KEY` 只记录 `inline` 标记，网关无法恢复该秘密时拒绝路由并提示改用文件。

新启动在模型加载和绑定期间不发布 ready PID；网关通过同一观察结果及健康检查决定可路由后端。后台进程日志可在等待期间查看。读取状态不修改文件，失效记录通过显式 `reconcile` 或相应 stop 操作清理。新记录的第四行 JSON 不影响旧版只读取前三行的消费者。

## 取消状态不确定时

客户端断开不证明模型计算结束。若网关无法确认取消已经完成，会保留占用并把实例标记为 `uncertain`，阻止该实例继续接收新推理。

此时先通过后端支持的槽位/任务状态确认空闲；不支持可靠查询时，在受控窗口停止并重启该后端。确认后端已空闲或重启完成后，再重启网关恢复调度。当前没有面向客户端的免认证“清除不确定占用”接口，`llm reconcile` 仅清理失效 PID，不会解除网关的推理占用。不要只重启网关绕过还在计算的后端。

## 回退

结构变更失败时，先停止新实例并确认端口释放，再恢复对应旧代码入口、配置和必要的 plist 定义，使用原依赖环境与引擎重新启动。若新旧代码的 PID 记录格式不同，不能仅复制 PID 文件来伪造进程归属。

引擎默认档案可用 `engine rollback` 恢复上一引用；模型显式配置了 `engine_profile` 或使用 `CPP_DIR` 时，需要恢复对应模型字段或启动参数。用旧引擎 plan 验证后再重启目标模型；不搬迁或重新下载权重。安装环境升级应保留原虚拟环境，切回其解释器后验证小样本。

认证、未知模型错误及取消保护属于有意改变的行为。回退它们需要单独评估客户端与调用方影响；不要为了临时恢复一个调用而撤销整个结构重构。所有实际切换、测试结果和回退原因应记录到本机验证报告，不能把测试方法当作已完成线上验证。
