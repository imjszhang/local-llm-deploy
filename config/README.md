# 配置样例

`examples/` 只放可入库的模板，真实部署配置继续保存在项目根目录，避免影响现有服务、PID 和 LaunchAgent。

| 模板 | 实际部署文件 | 用途 |
| --- | --- | --- |
| `examples/models.json.example` | `models.json` | 模型注册表；用 `./manage.sh registry init` 初始化 |
| `examples/engines.json.example` | `engines.json` | 引擎档案格式；正常使用 `./manage.sh engine ...` 管理 |
| `examples/api-key.example` | `.api-key` | 网关默认凭据的配置说明 |
| `examples/hf-env.example` | `.hf-env` | 下载来源与环境配置说明 |

`registry init` 默认使用本目录中的模型模板；仍接受旧部署目录根部的 `models.json.example`。已有 `models.json` 时初始化会拒绝覆盖，除非明确传入 `--force`。
