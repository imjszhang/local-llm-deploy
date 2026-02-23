# Local LLM Deploy

本地多模型部署，提供 OpenAI 兼容 API。支持 LLM 对话模型（llama.cpp）和 Embedding 向量模型（sentence-transformers），可同时运行多个模型或按需启动。

## 已支持模型

### 对话模型（Chat）

| 模型 | 默认端口 | 默认量化 | 磁盘占用 |
|------|----------|----------|----------|
| GLM-5 | 8001 | UD-IQ2_XXS (2-bit) | ~241GB |
| Qwen3.5-397B-A17B | 8002 | MXFP4_MOE (4-bit) | ~214GB |
| MiniMax-M2.5 | 8003 | BF16 | ~457GB |

### Embedding 模型

| 模型 | 默认端口 | 格式 | 磁盘占用 |
|------|----------|------|----------|
| jina-embeddings-v5-text-small | 8004 | safetensors | ~1.4GB |

新增模型只需在 `models.json` 中添加配置（`type: "embedding"` 区分类型）。

## 快速开始

```bash
# 0. 克隆 llama.cpp 并编译（对话模型需要）
./init_llamacpp.sh
./setup_llamacpp.sh

# 1. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 下载模型
./manage.sh download glm-5                  # 对话模型
./manage.sh download jina-embed             # Embedding 模型

# 3. 启动模型
./manage.sh start glm-5                     # 对话模型
./manage.sh start jina-embed                # Embedding 模型

# 4. 查看状态
./manage.sh status

# 5. 启动前端（聊天 + 监控 + API 代理）
./serve-ui.sh
# 访问 http://localhost:8888/monitor.html
```

## 管理命令

```bash
./manage.sh list                            # 列出所有已注册模型
./manage.sh download <模型名> [--quant X]   # 下载模型
./manage.sh start <模型名> [--port P]       # 启动模型
./manage.sh stop <模型名>                   # 停止模型
./manage.sh stop --all                      # 停止所有模型
./manage.sh status                          # 查看运行中实例
./manage.sh logs <模型名>                   # 查看模型日志
```

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

## 多模型同时运行

```bash
./manage.sh start glm-5                     # 对话，端口 8001
./manage.sh start qwen3.5                   # 对话，端口 8002
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

## 文档

- [DEPLOY.md](DEPLOY.md) — 完整部署指南
- [docs/architecture.md](docs/architecture.md) — 架构说明
