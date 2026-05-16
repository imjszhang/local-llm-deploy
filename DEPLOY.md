# Local LLM Deploy — 部署指南

本文档包含通过 llama.cpp 部署本地 LLM 模型的完整流程。支持多模型管理，通过 `models.json` 配置注册模型，通过 `manage.sh` 统一管理。

---

## 目录结构

```
local-llm-deploy/
├── manage.sh              # 统一管理入口
├── models.json.example    # 注册表示例（提交仓库）
├── registry_cli.py        # manage.sh registry 子命令实现
├── deploy.sh              # 通用部署脚本
├── download.sh            # 下载入口（调用 download_model.py）
├── download_model.py      # 统一下载实现（GGUF / embedding / 双源）
├── model_paths.py         # 本地路径与权重检测（与下载 / 清单共用）
├── model_inventory.py     # 模型磁盘清单、remove、register、manifest
├── serve-ui.py            # 前端 + 多模型 API 代理
├── serve-ui.sh            # 前端启动器
├── setup_llamacpp.sh      # llama.cpp 编译脚本
├── init_llamacpp.sh       # llama.cpp 克隆脚本
├── monitor.sh             # 命令行监控
├── requirements.txt       # Python 依赖
├── static/
│   ├── index.html         # 入口页
│   └── monitor.html       # 监控面板（多模型切换）
├── docs/
│   └── architecture.md    # 架构说明
├── run/                   # PID 文件（自动创建）
├── logs/                  # 日志文件（自动创建）
├── models/                # 模型目录（需下载）
└── llama.cpp/             # 推理引擎（需克隆编译）

说明：`models.json` 为本地配置，由 `./manage.sh registry init` 从 `models.json.example` 生成，默认不提交（见 `.gitignore`）。
```

---

## 一、前置条件

### 1. 编译 llama.cpp

```bash
# 克隆
./init_llamacpp.sh

# 编译
./setup_llamacpp.sh
```

### 2. 创建 Python 虚拟环境（用于模型下载）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 初始化模型注册表

```bash
./manage.sh registry init   # 从 models.json.example 生成本地 models.json
```

维护：`./manage.sh registry list`、`show`、`merge <补丁.json>`、`remove <键名>`。

---

## 二、模型下载

```bash
# 下载 GLM-5（默认 UD-IQ2_XXS，~241GB）
./manage.sh download glm-5

# 下载 Qwen3.5-397B-A17B（配置键 qwen3.5；默认 MXFP4_MOE，~214GB）
./manage.sh download qwen3.5

# 并行下载新版、不覆盖当前在用的目录（路径相对项目下 models/，或写绝对路径）
./manage.sh download qwen3.5 --quant MXFP4_MOE --to unsloth-Qwen3.5-397B-A17B-GGUF/MXFP4_MOE-next

# 指定量化版本
./manage.sh download glm-5 --quant UD-TQ1_0
./manage.sh download qwen3.5 --quant UD-Q2_K_XL   # 仍为 Qwen3.5-397B-A17B，更小体积量化
```

下载完成后，**默认**仍使用原来的 `models/<repo>/<quant>/`；新版在 `--to` 指定目录。切换时：

```bash
./manage.sh stop qwen3.5
./manage.sh start qwen3.5 --model-dir "$PWD/models/unsloth-Qwen3.5-397B-A17B-GGUF/MXFP4_MOE-next"
```

`--model-dir` 会覆盖 `models.json` 里的默认路径，其余参数（模板、`mmproj`、`extra_args` 等）仍从 `qwen3.5` 读取。多模态用的 `mmproj-F16.gguf` 仍在仓库根目录 `models/unsloth-Qwen3.5-397B-A17B-GGUF/`；若官方更新了 mmproj，请在同一目录替换或单独下载该文件。

**说明**：`manage.sh` 按配置键管理 `run/<键名>.pid`，同名模型同时只能登记一个进程。要在验证新版时**暂时双开**旧版与新版，需要在 `models.json` 里复制一条不同键名的配置（不同 `default_port`），或自行处理第二个进程的 PID/端口，避免互相覆盖。

并行目录可通过 `./manage.sh models` 查看；下载脚本会自动写入 `models/.manifest.json`，也可用 `./manage.sh register` 手动登记。

---

## 二点五、本地权重清单与删除

```bash
# 各量化是否就绪、估算体积、manifest 路径
./manage.sh models

# 删除某一量化目录（须先 stop；路径限定在 models/ 下）
./manage.sh remove qwen3.5 --quant UD-Q2_K_XL

# 删除 models.json 中声明的全部量化子目录
./manage.sh remove qwen3.5 --all

# Embedding：删除整个 embedding 目录
./manage.sh remove jina-embed

# 进程仍在运行时会拒绝删除；仅脚本自动化可加 --force

# 手动登记并行目录
./manage.sh register qwen3.5 --path models/unsloth-Qwen3.5-397B-A17B-GGUF/MXFP4_MOE-next --quant MXFP4_MOE-next
```

`./manage.sh list` 中的「已下载」与上述检测逻辑一致（默认量化 / embedding）。

---

## 三、服务部署

### 基本用法

```bash
# 启动单个模型
./manage.sh start glm-5

# 启动指定端口
./manage.sh start qwen3.5 --port 8003   # Qwen3.5-397B-A17B

# 启动指定量化版本
./manage.sh start qwen3.5 --quant UD-Q2_K_XL
```

### 多模型同时运行

```bash
./manage.sh start glm-5      # 端口 8001
./manage.sh start qwen3.5    # Qwen3.5-397B-A17B，端口 8002
./manage.sh status            # 查看所有实例
```

> **注意**：同时运行多个大模型需要足够的内存。256GB Mac 通常只够运行一个大模型。

### 认证

```bash
# 单 Key 认证
./manage.sh start glm-5 --api-key "sk-your-secret-key"

# 多 Key（密钥文件，每行一个）
./manage.sh start glm-5 --api-key-file .api-key
```

### 高级用法（手动指定目录）

```bash
./deploy.sh --model-dir ./models/custom-model/ --cpp-dir ./llama.cpp --port 8001
```

### OpenAI 接口调用

外部通过 OpenAI 兼容 API 调用时，请求体中的 `model` 字段应使用 `alias`（如 `unsloth/GLM-5`），而非内部名称（如 `glm-5`）：

```bash
# 示例：curl 调用 chat completions
curl -X POST http://localhost:8888/api/glm-5/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "unsloth/GLM-5", "messages": [{"role": "user", "content": "你好"}]}'
```

`/api/models` 会返回每个实例的 `model` 字段，即应使用的模型名。

---

## 四、前端与监控

```bash
# 启动前端（自动代理到所有运行中的模型）
./serve-ui.sh

# 访问
# 监控面板: http://localhost:8888/monitor.html
# 支持在页面顶部下拉框切换监控不同模型
```

### 命令行监控

```bash
./monitor.sh health                  # 默认端口 8001
./monitor.sh metrics --model glm-5   # 指定模型
./monitor.sh slots --model qwen3.5   # Qwen3.5-397B-A17B
```

---

## 五、管理命令速查

```bash
./manage.sh list          # 列出已注册模型
./manage.sh status        # 运行中的实例
./manage.sh start <name>  # 启动模型
./manage.sh stop <name>   # 停止模型
./manage.sh stop --all    # 停止全部
./manage.sh logs <name>   # 查看日志
./manage.sh download <name>  # 下载模型
```

---

## 六、添加新模型

编辑 `models.json` 添加新条目：

```json
{
  "new-model": {
    "repo_id": "unsloth/New-Model-GGUF",
    "default_quant": "Q4_K_M",
    "quants": {
      "Q4_K_M": { "pattern": "*Q4_K_M*", "size_gb": 50 }
    },
    "alias": "unsloth/New-Model",
    "default_port": 8003,
    "params": {
      "temp": 0.7, "top_p": 0.9, "ctx_size": 8192,
      "n_predict": 4096, "repeat_penalty": 1.0,
      "extra_args": ["--jinja"]
    }
  }
}
```

然后：

```bash
./manage.sh download new-model
./manage.sh start new-model
```

---

## 七、硬件建议（Apple Silicon）

| 内存 | 推荐方案 |
|------|----------|
| 512GB Apple Studio | 同时运行 GLM-5 (241GB) + Qwen3.5-397B-A17B (214GB) |
| 256GB Mac | 运行一个大模型：GLM-5 UD-IQ2_XXS 或 Qwen3.5-397B-A17B MXFP4_MOE |
| 192GB | 小量化版本：GLM-5 UD-TQ1_0 (176GB) 或 Qwen3.5-397B-A17B UD-Q2_K_XL (120GB) |

---

## 八、故障排查

| 问题 | 解决方案 |
|------|----------|
| 模型加载超时 | 大模型需数分钟，查看日志 `./manage.sh logs <name>` |
| 端口被占用 | 使用 `--port` 指定其他端口 |
| 内存不足 | 选择更小的量化版本，或使用 `--quant` 参数 |
| 编译失败 | 确保 `./setup_llamacpp.sh` 成功执行 |
