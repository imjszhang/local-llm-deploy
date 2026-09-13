# 升级与回退

把模型权重、推理引擎和 Python 环境分别升级，每次只切换一个可验证的引用。准备新版本时，现有进程继续使用原版本。

## 更新模型权重

1. 在模型配置中记录明确的 Hub revision，或下载时传 `--revision`。
2. 使用 `download --to <新目录>` 下载到另一安装目录。
3. 检查 `models` 清单、完整性和量化；`register` 也可登记已有目录。
4. 停止该模型后，通过 `--model-dir` 选择新安装，验证 API。
5. 失败时停止新实例，以原目录重新启动。

`--to` 相对 `models/`，`--model-dir` 相对项目根。manifest 对同量化多安装不自动猜测默认值。模型内加载的 Python 代码（如 Jina reranker 的 `rerank.py`）随模型 revision 一起记录。

## 更新 llama.cpp

```bash
# 先登记当前已验证构建，路径按实际部署填写
./manage.sh engine register stable --directory /path/to/current/llama.cpp --default

# 在 work_dir/llama.cpp-candidate 创建独立构建；不覆盖 stable
./manage.sh engine update candidate --revision <40位commit>
./manage.sh engine verify candidate

# 查看启动描述，安排受控模型切换和真实小样本测试
./manage.sh plan <模型键> --engine candidate
./manage.sh stop <模型键>
./manage.sh start <模型键> --engine candidate

# 记录实际验证报告，再选择默认引擎
./manage.sh engine record-validation candidate --report /path/to/report.md
./manage.sh engine use candidate
```

档案包含仓库、精确 commit、构建参数、可执行文件校验摘要和验证报告引用。`verify` 检查源码状态、程序摘要和 `--version`，它不代替推理测试。`record-validation` 只关联已有报告，不自动宣称测试通过。

登记目录必须是独立 Git 工作树根目录；无 Git 元数据的源码快照或已有源码修改不能获得可信的 commit 记录。此时先保留原 `CPP_DIR` 路径，另行确认源码并创建候选构建；不要清理现有修改来让登记通过。重复 `engine use` 当前档案不会覆盖上一回退目标。

如果模型设置了 `engine_profile`，它优先于默认档案；`--cpp-dir` / `CPP_DIR` 显式覆盖。`engine use` 和 `rollback` 仅改变下次启动的默认选择，不重启运行服务。

```bash
./manage.sh stop <模型键>
./manage.sh engine rollback
./manage.sh start <模型键>
```

初次没有上一默认档案时，使用 `engine use stable`。特定模型仍绑定 candidate 时要将其 `engine_profile` 改回 stable，或启动时显式 `--engine stable`。

## 更新 Python 依赖

保持各推理环境独立。`constraints/` 是对应平台/Python 的已解析依赖集合，不把所有模型库装进控制层。

```bash
# 示例：建立候选环境，原 .venv-rerank 保留
python3.14 -m venv .venv-rerank-next
.venv-rerank-next/bin/python -m pip install -e '.[rerank]' -c constraints/rerank-macos-py314.txt
.venv-rerank-next/bin/python -m pip check
.venv-rerank-next/bin/python serve_rerank.py --help
```

按后端实际配置指定候选解释器（`runtime.python`），检查 `manage.sh plan` 的 argv。使用备用端口或受控停启做小样本；验证通过后保留新解释器路径。失败时将配置恢复到原解释器并重启对应服务。

更新版本时，在新环境中显式解析/安装，重新生成该组依赖的传递闭包约束，再提交约束和验证记录。不要用普通服务启动或模型下载顺便升级 pip/模型库。

对于慢网络，可先生成 wheelhouse，再用 `--no-index --find-links <目录>` 从相同版本的完整 wheel 安装；保留 wheel 来源和 SHA256。不要从已经安装的 site-packages 复制文件来代替依赖重建。

## 切换前后的验证

- 核心：`python -m unittest discover -s tests -v`。
- 生命周期：干净临时实例 start → ready → stop → restart，macOS 另测 launchd。
- 真实模型：Chat 普通/流式；Embedding task/维度；Rerank 返回字段；Whisper 三种格式。
- 监控：认证、模型详情、队列状态；知识库凭据独立。

避免同时用两个网关向同一真实后端发推理请求，因为调度预算不跨进程共享。停止网关前先确认请求结束；如果它已异常退出或显示 uncertain，先确认后端空闲或重启后端，再恢复网关。
