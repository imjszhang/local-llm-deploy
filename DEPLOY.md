# Local LLM Deploy — 部署指南

本文档包含通过 llama.cpp 部署本地 LLM 模型的完整流程。支持多模型管理，通过 `models.json` 配置注册模型，通过 `manage.sh` 统一管理。

---

## 目录结构

```
local-llm-deploy/
├── manage.sh              # 统一管理入口
├── models.json            # 模型注册配置
├── deploy.sh              # 通用部署脚本
├── download.sh            # 通用下载脚本
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

---

## 二、模型下载

```bash
# 下载 GLM-5（默认 UD-IQ2_XXS，~241GB）
./manage.sh download glm-5

# 下载 Qwen3.5（默认 MXFP4_MOE，~214GB）
./manage.sh download qwen3.5

# 指定量化版本
./manage.sh download glm-5 --quant UD-TQ1_0
./manage.sh download qwen3.5 --quant UD-Q2_K_XL
```

---

## 三、服务部署

### 基本用法

```bash
# 启动单个模型
./manage.sh start glm-5

# 启动指定端口
./manage.sh start qwen3.5 --port 8003

# 启动指定量化版本
./manage.sh start qwen3.5 --quant UD-Q2_K_XL
```

### 多模型同时运行

```bash
./manage.sh start glm-5      # 端口 8001
./manage.sh start qwen3.5    # 端口 8002
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
./monitor.sh slots --model qwen3.5   # 指定模型
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
| 512GB Apple Studio | 同时运行 GLM-5 (241GB) + Qwen3.5 (214GB) |
| 256GB Mac | 运行一个大模型：GLM-5 UD-IQ2_XXS 或 Qwen3.5 MXFP4_MOE |
| 192GB | 小量化版本：GLM-5 UD-TQ1_0 (176GB) 或 Qwen3.5 UD-Q2_K_XL (120GB) |

---

## 八、故障排查

| 问题 | 解决方案 |
|------|----------|
| 模型加载超时 | 大模型需数分钟，查看日志 `./manage.sh logs <name>` |
| 端口被占用 | 使用 `--port` 指定其他端口 |
| 内存不足 | 选择更小的量化版本，或使用 `--quant` 参数 |
| 编译失败 | 确保 `./setup_llamacpp.sh` 成功执行 |
