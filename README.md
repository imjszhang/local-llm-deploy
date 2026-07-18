# Local LLM Deploy

本地多模型部署，提供 OpenAI 兼容 API。支持 LLM 对话模型（llama.cpp）、Embedding 向量模型（sentence-transformers）、Rerank 与 Whisper ASR（mlx-whisper），可同时运行多个模型或按需启动。

## 已支持模型

### 对话模型（Chat）

| 模型（`manage.sh` 键名） | 完整型号 / 说明 | 默认端口 | 默认量化 | 磁盘占用 |
|------|----------|----------|----------|----------|
| `glm-5` | GLM-5 | 8001 | UD-IQ2_XXS (2-bit) | ~241GB |
| `qwen3.5` | **Qwen3.5-397B-A17B**（Unsloth GGUF：`unsloth/Qwen3.5-397B-A17B-GGUF`） | 8002 | MXFP4_MOE (4-bit) | ~214GB |
| `minimax` | MiniMax-M2.5 | 8003 | BF16 | ~457GB |

### Embedding 模型

| 模型 | 默认端口 | 格式 | 磁盘占用 |
|------|----------|------|----------|
| jina-embeddings-v5-text-small | 8004 | safetensors | ~1.4GB |

### ASR 模型（Whisper）

| 模型（`manage.sh` 键名） | 默认端口 | 格式 | 磁盘占用 |
|------|----------|------|----------|
| `whisper-large-v3` | 8007 | MLX (mlx-whisper) | ~3GB |

新增模型：编辑本地 `models.json`，或使用 `./manage.sh registry merge <补丁.json>` / `registry remove` 等命令。模板见仓库内 `models.json.example`（含 `whisper-large-v3` 示例）。

## 快速开始

```bash
# 0. 克隆 llama.cpp 并编译（对话模型需要）
./init_llamacpp.sh
./setup_llamacpp.sh

# 1. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 生成本地 models.json（自 models.json.example）
./manage.sh registry init

# 3. 下载模型（键名以你 registry 中的为准）
./manage.sh download glm-5                  # 对话模型
./manage.sh download jina-embed             # Embedding 模型
./manage.sh download whisper-large-v3       # Whisper ASR（需 ffmpeg + .venv-whisper）

# 4. 启动模型
./manage.sh start glm-5                     # 对话模型
./manage.sh start jina-embed                # Embedding 模型
./manage.sh start whisper-large-v3          # ASR 模型

# 5. 查看状态
./manage.sh status

# 6. 启动前端（聊天 + 监控 + API 代理）
./serve-ui.sh
# 访问 http://localhost:8888/monitor.html
```

## 管理命令

```bash
./manage.sh registry init                   # 首次从 models.json.example 生成 models.json
./manage.sh registry list                   # 已注册的模型键
./manage.sh registry merge patch.json       # 合并/覆盖顶层条目
./manage.sh registry remove <键名>
./manage.sh list                            # 已注册模型（简略）
./manage.sh models                          # 各量化目录、体积与 manifest
./manage.sh download <模型名> [--quant X]   # 下载模型
./manage.sh remove <模型名> --quant X       # 删除某一量化目录（需先 stop）
./manage.sh remove <模型名> --all           # 删除该模型全部已声明量化目录
./manage.sh register <模型名> --path models/... [--quant X]  # 登记并行路径到 manifest
./manage.sh start <模型名> [--port P]       # 启动模型
./manage.sh stop <模型名>                   # 停止模型
./manage.sh stop --all                      # 停止所有模型
./manage.sh status                          # 查看运行中实例
./manage.sh logs <模型名>                   # 查看模型日志
```

并行下载（`download --to`）会写入 `models/.manifest.json`（位于 `models/` 下，默认已被忽略）；`./manage.sh models` 会列出 manifest 条目。

## API 接口

统一通过 `serve-ui.py`（端口 8888）代理，自动按 `model` 字段路由到对应后端。

### 对话

```bash
curl http://localhost:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <你的API-Key>" \
  -d '{"model":"unsloth/GLM-5","messages":[{"role":"user","content":"你好"}]}'
```

### Embedding

```bash
curl http://localhost:8888/v1/embeddings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <你的API-Key>" \
  -d '{"model":"jina-embeddings-v5-text-small","input":["文本1","文本2"]}'
```

Embedding 接口额外支持：
- `task` — 切换 LoRA adapter：`text-matching`（默认）、`retrieval`、`classification`、`clustering`
- `dimensions` — Matryoshka 维度截断（32 ~ 1024）

### ASR（Whisper）

```bash
curl http://localhost:8888/v1/audio/transcriptions \
  -H "Authorization: Bearer <你的API-Key>" \
  -F file=@audio.mp3 \
  -F model=whisper-large-v3 \
  -F language=zh
```

详见 [docs/whisper-guide.md](docs/whisper-guide.md)。

## 多模型同时运行

```bash
./manage.sh start glm-5                     # 对话，端口 8001
./manage.sh start qwen3.5                   # Qwen3.5-397B-A17B，端口 8002
./manage.sh start jina-embed                # Embedding，端口 8004

./manage.sh status                          # 查看所有实例
./manage.sh stop --all                      # 停止全部
```

## 本地配置（可选）

启用 API Key 认证时，可复制模板并填入密钥：

```bash
cp .api-key.example .api-key
# 编辑 .api-key 填入实际 key（该文件已被 .gitignore 忽略，不会提交）
```

使用 `--source huggingface` 下载时，若未设置环境变量 `HF_ENDPOINT`，下载逻辑会默认使用 `https://hf-mirror.com`（由 `download_model.py` 处理）。需要官方 Hub 时执行 `export HF_ENDPOINT=https://huggingface.co`。可将 `cp .hf-env.example .hf-env` 后按需编辑，`download_model.py` 会读取 `.hf-env`。

## 文档

- [DEPLOY.md](DEPLOY.md) — 完整部署指南
- [docs/architecture.md](docs/architecture.md) — 架构说明
- [docs/whisper-guide.md](docs/whisper-guide.md) — Whisper ASR 使用指南
