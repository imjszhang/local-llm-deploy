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

模型下载需要下载依赖，推理依赖按需安装，见 [部署指南](DEPLOY.md)。

```bash
.venv/bin/python -m pip install -e '.[download]'
./manage.sh download <模型键> --dry-run
./manage.sh download <模型键>
./manage.sh plan <模型键>
./manage.sh start <模型键>
./manage.sh status --probe
./serve-ui.sh start
```

访问 [监控页面](http://localhost:8888/monitor.html)。如果配置了 `.api-key`，在页面中填写 API Key 后读取模型详情；Key 只保存在当前页面内存中。

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

## 文档

- [架构与模块职责](docs/architecture.md)
- [部署与环境安装](DEPLOY.md)
- [API 与认证](docs/api-guide.md)
- [开发和测试](docs/development.md)
- [迁移说明](docs/migration.md)
- [兼容契约](docs/compatibility.md)
- [升级与回退](docs/upgrade.md)
- [验证记录](docs/validation.md)
- [重构计划](docs/refactoring-plan.md)
