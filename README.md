# Local LLM Deploy

本机多模型推理的部署管理与 API 网关。支持 llama.cpp、Ollama、Transformers Embedding、MLX Rerank 和 mlx-whisper；当前主要验证平台为 macOS / Apple Silicon。

控制层使用 Python 标准库，不需要安装模型推理依赖。模型服务运行在各自的虚拟环境中。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'pip==25.3'
.venv/bin/python -m pip install -e .
./manage.sh registry init
./manage.sh config validate
./manage.sh list
```

已有 `models.json` 时跳过 `registry init`；重构不要求迁移已有权重或重写注册表。

模型下载需要下载依赖，推理依赖按需安装，见 [部署指南](docs/deployment.md)。

```bash
.venv/bin/python -m pip install -e '.[download]'
./manage.sh download <模型键> --dry-run
./manage.sh download <模型键>
./manage.sh plan <模型键>
./manage.sh start <模型键>
./manage.sh status --probe
./manage.sh start serve-ui
```

访问 [监控页面](http://localhost:8888/monitor.html)，查看系统趋势、全部模型、通道活动和后端详情。如果配置了 `.api-key`，通过“访问设置”输入后读取完整状态；Key 只保存在当前页面内存中。页面为只读控制台，启动无需 Node。前端开发和构建见 [frontend/README.md](frontend/README.md)。

## 日常管理

```bash
./manage.sh registry list
./manage.sh registry merge patch.json
./manage.sh config show --resolved
./manage.sh models --json
./manage.sh register <模型键> --path models/<安装目录> --quant <量化>
./manage.sh start <模型键> --port 8002 --model-dir models/<安装目录>
./manage.sh stop <模型键>
./manage.sh stop --all
./manage.sh logs <模型键>
./manage.sh remove <模型键> --quant <量化> --dry-run
```

`list/status/models` 为只读查询，失效 PID 记录由 `reconcile` 显式清理。Ollama 与外部服务默认只连接，不由 `stop --all` 停止。

## 统一 API

```bash
curl http://localhost:8888/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <API-Key>' \
  -d '{"model":"<模型键或别名>","messages":[{"role":"user","content":"你好"}]}'
```

网关提供 Chat、Embedding、Rerank 和 Whisper 路由。显式未知模型返回 404，离线模型返回 503。省略 `model` 时必须配置明确的默认模型；不存在默认配置时返回 400。

同一模型可通过注册表 `default_for: ["chat"]` 或 `DEFAULT_CHAT_MODEL` 等环境变量成为默认模型。模型的能力和具体后端分别声明，新增同类模型通常只需要修改注册表。

## 引擎更新

llama.cpp 的源码与模型权重分开管理。新构建采用明确 commit，旧构建保留用于回退。

```bash
./manage.sh engine register stable --directory /path/to/verified/llama.cpp --default
./manage.sh engine update candidate --revision <40位commit>
./manage.sh engine verify candidate
# 按 upgrade.md 完成真实模型验证后再选择默认版本
./manage.sh engine use candidate
./manage.sh engine rollback
```

模型可设置 `engine_profile` 绑定特定构建，`CPP_DIR` / `--cpp-dir` 仍可覆盖。详见 [升级指南](docs/upgrade.md)。

## 目录导航

```text
src/local_llm_deploy/  核心实现：CLI、模型管理、网关、服务
config/examples/      可入库的配置模板
frontend/             监控前端源码、构建配置与浏览器测试
static/               已构建的监控页面及前端资源
templates/            模型推理提示模板
requirements/         分环境的依赖安装入口
constraints/          已验证的依赖版本
tests/                core / gateway / lifecycle / services / smoke
scripts/              维护工具及已有 launchd 启动入口
scripts/compat/       已迁移的旧手动命令
tools/                ffmpeg 等运行辅助入口
docs/                 部署、开发、架构、升级与验收文档
```

日常操作统一使用 `./manage.sh`；已安装包也提供 `local-llm`。原根目录的手动脚本已移至 `scripts/compat/`，参数用法保留，旧根路径不再提供。新旧命令对照见 [旧命令迁移](scripts/compat/README.md)。新增业务代码放入 `src/`，文件放置规则见 [仓库目录说明](docs/architecture.md#仓库目录)。

根目录受版本控制的文件由 29 个减少到 11 个：README、`pyproject.toml`、两份忽略规则、`manage.sh` / `llm.py` / `bootstrap.py`，以及现有服务依赖的四个 Python 启动入口。依赖安装直接使用 `requirements/`，部署说明直接使用 `docs/deployment.md`。

真实配置 `models.json` / `engines.json` / `.api-key` / `.hf-env` 与 `models/`、`run/`、`logs/`、虚拟环境及引擎构建属于本地部署数据。它们继续沿用现有路径，不随源码目录整理迁移；配置模板说明见 [config/README.md](config/README.md)。

## 文档

- [架构与模块职责](docs/architecture.md)
- [部署与环境安装](docs/deployment.md)
- [API 与认证](docs/api-guide.md)
- [开发和测试](docs/development.md)
- [迁移说明](docs/migration.md)
- [兼容契约](docs/compatibility.md)
- [升级与回退](docs/upgrade.md)
- [验证记录](docs/validation.md)
- [重构计划](docs/refactoring-plan.md)
- [监控控制台实施与验收](docs/monitor-ui-plan.md)
