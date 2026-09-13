# 模型对话的思考控制

思考开关、推理强度和思考 token 预算是不同的能力，必须以实际模型、聊天模板和后端版本为准。模型注册表中的 `chat_controls` 只声明已验证的能力和默认值，供对话页生成请求；它不会改变模型启动参数或后端默认生成设置。

## 配置声明

在 `models.json` 的单个模型顶层增加以下对象。下面是已核对的本地 Qwen3.8 llama.cpp 模型示例：

```json
{
  "qwen3.8-27b-aggressive": {
    "backend": "llama_cpp",
    "chat_controls": {
      "thinking": true,
      "reasoning_efforts": ["low", "medium", "xhigh"],
      "reasoning_budget": true,
      "default_thinking": false,
      "default_effort": "xhigh"
    }
  }
}
```

将 `chat_controls` 合并进已有模型条目，保留原来的模型、路径、下载和运行配置。不要用上面的片段整体替换现有注册表。

| 字段 | 含义 |
| --- | --- |
| `thinking` | 是否支持按请求开启或关闭思考；布尔值 |
| `reasoning_efforts` | 当前模板接受的强度标识，按页面展示顺序排列；空数组代表未声明强度控制 |
| `reasoning_budget` | 是否支持按请求限制思考 token；布尔值，Ollama 必须为 `false` |
| `default_thinking` | 后端默认是否思考；无法确认时填 `null` |
| `default_effort` | 开启思考时模板默认强度；无法确认时填 `null`，非空值必须出现在强度列表中 |

整个 `chat_controls` 可省略，表示能力未知，不自动启用控制项。一旦声明，五个字段必须齐全，未知字段和类型错误会被注册表校验拒绝。强度列表最多 8 个唯一标识，每项长度 1–24，以小写字母开头，后续只允许小写字母、数字、下划线和连字符。`none` 不能作为强度，关闭思考使用独立开关。

修改注册表后运行 `./manage.sh config validate`；声明本身不会启动或重启模型。

只支持开关的 Ollama 模型可以声明：

```json
{
  "thinking": true,
  "reasoning_efforts": [],
  "reasoning_budget": false,
  "default_thinking": null,
  "default_effort": null
}
```

以上 Ollama 对象仍须建立在该模型实际支持思考开关的验证上，不能仅凭 `backend: "ollama"` 添加。其他后端和没有 `chat` 能力的模型暂不向页面开放这些控制。

## 监控接口

受保护的 `GET /monitor-api/v1/snapshot` 为每个模型返回 `chat_controls`：

- 已注册、具有 `chat` 能力且后端为 `llama_cpp` 或 `ollama`：返回配置声明的五个安全字段，并增加 `source: "configured"`。
- 没有声明、自动发现模型、其他后端或非对话模型：返回 `null`。

接口不会返回模型原始配置、模板、凭据或机器路径，也不会为发现这些能力发送推理请求。llama.cpp `/props.chat_template_caps.supports_reasoning_effort` 只说明模板使用了强度字段，不包含合法档位；不能据此猜测 `low/medium/high`。

## 本地 llama.cpp 请求参数

已验证的本地模板只接受 `low`、`medium`、`xhigh`，默认 `xhigh`；`high` 会被模板拒绝。示例请求字段：

```json
{
  "chat_template_kwargs": {"enable_thinking": true},
  "reasoning_effort": "medium",
  "reasoning_format": "deepseek",
  "reasoning_budget_tokens": 1024,
  "max_tokens": 2048
}
```

`enable_thinking` 可以覆盖启动时的 `--reasoning off` 默认值，不需要重启模型。`reasoning_format: "deepseek"` 让后端将思考输出放入独立 `reasoning_content` 字段；它控制输出解析，与思考开关独立。`--no-reasoning-preserve` 控制历史思考轨迹是否进入模板，与本次是否生成思考独立。

预算大于零时，后端在达到预算后结束思考；这不是推理质量保证，也不等于总输出上限。结束标签和补齐 UTF-8 字符可能多消耗 token，新的思考块会重新计数。`max_tokens` 仍限制整次生成，应给最终答案留出余量。预算 `0` 表示立即结束思考；请求值 `-1` 回落后端默认预算，当前本地服务未设置预算限制。

模型权重、模板或后端版本更新后，需要重新核对声明，尤其不要将某个模型的强度列表推广到所有同类后端。

## 页面使用

选择模型后打开“测试设置”，深度思考可选默认、开启、关闭。开启后才显示模型声明的推理强度和可选预算。切换模型会重置思考设置，历史回答保留发送时的参数与能力快照；刷新页面前可导出 JSON 保存。

- 默认：不覆盖思考开关和强度。已声明支持思考的 llama.cpp 会单独设置 `reasoning_format: "deepseek"`，以分离思考内容与正文。
- llama.cpp 开启：按需发送上面的字段；关闭：`chat_template_kwargs.enable_thinking: false`。
- Ollama 开启：通过 OpenAI 兼容接口发送 `reasoning_effort`；未选强度时使用 `medium` 来明确启用思考。关闭使用 `reasoning_effort: "none"`。页面不向这个接口发送原生 API 的 `think` 字段。
- 页面预算接受 1–131072 的整数，留空沿用后端默认。设置最大输出时，预算需小于最大输出，为最终回答预留空间。

当前本地 `qwen3.8:27b-mlx` 使用 Ollama 0.32.13 的 `qwen3.8` renderer，经核对可声明：

```json
{
  "thinking": true,
  "reasoning_efforts": ["low", "medium", "high"],
  "reasoning_budget": false,
  "default_thinking": null,
  "default_effort": null
}
```

这里 `high` 对应 renderer 中的深入思考指令，`max` 与它效果相同，因此页面只提供 `high`。llama.cpp 的同系列本地模板接受的是 `xhigh`，两者不能互换。档位控制思考详略，不保证固定计算量、token 数或回答质量。

依据：[Ollama 思考功能](https://docs.ollama.com/capabilities/thinking)、[0.32.13 OpenAI 参数转换](https://github.com/ollama/ollama/blob/v0.32.13/openai/openai.go)、[Qwen renderer](https://github.com/ollama/ollama/blob/v0.32.13/model/renderers/qwen35.go)。
