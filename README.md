# Local LLM Deploy

基于 llama.cpp 的本地 LLM 多模型部署，提供 OpenAI 兼容 API。支持同时运行多个模型或按需启动单个模型。

## 已支持模型

| 模型 | 默认端口 | 默认量化 | 磁盘占用 |
|------|----------|----------|----------|
| GLM-5 | 8001 | UD-IQ2_XXS (2-bit) | ~241GB |
| Qwen3.5-397B-A17B | 8002 | MXFP4_MOE (4-bit) | ~214GB |

新增模型只需在 `models.json` 中添加配置。

## 快速开始

```bash
# 0. 克隆 llama.cpp 并编译
./init_llamacpp.sh
./setup_llamacpp.sh

# 1. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 下载模型
./manage.sh download glm-5
# 或: ./manage.sh download qwen3.5

# 3. 启动模型
./manage.sh start glm-5
# 或: ./manage.sh start qwen3.5

# 4. 查看状态
./manage.sh status

# 5. 启动前端（聊天 + 监控，含多模型切换）
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

## 多模型同时运行

```bash
# 启动多个模型（需足够内存）
./manage.sh start glm-5                     # 端口 8001
./manage.sh start qwen3.5                   # 端口 8002

# 查看所有实例
./manage.sh status

# 停止全部
./manage.sh stop --all
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
