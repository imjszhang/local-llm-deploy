# 旧手动命令迁移

本目录集中原根目录的 13 个手动入口，原有参数用法与解释器环境变量保留。根目录只提供 `./manage.sh` 作为日常统一入口；旧的 `./jina.sh`、`./download.sh` 等路径已移除。

脚本仍可从任意工作目录通过绝对路径调用。例如从项目根目录执行 `./scripts/compat/jina.sh embed status`，也可以直接使用 `./manage.sh compat jina embed status`。

| 本目录旧入口 | 推荐用法（从项目根目录执行） |
| --- | --- |
| `deploy.sh` | `./manage.sh deploy ...` |
| `download.sh` / `download_model.py` | `./manage.sh download MODEL ...` |
| `jina.sh` | `./manage.sh compat jina {embed\|rerank\|all} {start\|stop\|status} ...` |
| `whisper.sh` | `./manage.sh compat whisper {start\|stop\|status} ...` |
| `serve-ui.sh` | `./manage.sh {start\|stop\|status} serve-ui` |
| `ds4.sh` | `./manage.sh compat ds4 {start\|stop\|status} ...` |
| `monitor.sh` | `./manage.sh monitor ...` |
| `init_llamacpp.sh` | `./manage.sh engine init --revision COMMIT ...` |
| `setup_llamacpp.sh` | `./manage.sh engine build ...` |
| `registry_cli.py` | `./manage.sh registry ...` |
| `model_inventory.py` | `./manage.sh models ...`、`register ...` 或 `remove ...` |
| `model_paths.py` | Python 代码改为导入 `local_llm_deploy.artifacts.paths` |

手动 Python 入口也可直接使用，例如 `python scripts/compat/registry_cli.py --help`。`_bootstrap.py` 仅负责定位仓库并调用根 `bootstrap.py`，业务实现位于 `src/local_llm_deploy/`。

现有服务仍依赖根目录 `serve-ui.py`、`serve_embedding.py`、`serve_rerank.py`、`serve_whisper.py` 和 `scripts/start-*.sh`；这些运行入口保留原址。此次迁移不更新 LaunchAgent、不重启服务。

根目录依赖转发文件与 `DEPLOY.md` 也已移除，分别使用 [依赖入口](../../requirements/README.md) 和 [部署指南](../../docs/deployment.md)。自动化中写死的旧手动路径需要按上表更新。
