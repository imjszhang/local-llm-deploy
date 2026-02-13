# GLM-5 部署项目

基于 llama.cpp 的 GLM-5 本地部署，提供 OpenAI 兼容 API。

## 快速开始

```bash
# 0. 克隆 llama.cpp（若尚未克隆）
./init_llamacpp.sh

# 1. 编译 llama.cpp（含 PR 19460）
./setup_llamacpp.sh

# 2. 创建环境并下载模型（约 241GB，需充足磁盘与内存）
source .venv/bin/activate
./download_models.sh

# 3. 启动服务
./deploy.sh --cpp-dir "$(pwd)/llama.cpp" --model-dir "$(pwd)/models/GLM-5-GGUF/UD-IQ2_XXS"

# 4. 启动前端（聊天 + 监控，启用认证时需单独运行）
./serve-ui.sh
# 访问 http://localhost:8888/ 聊天，http://localhost:8888/monitor.html 监控
```

## 硬件建议

| 内存 | 推荐量化版本 |
|------|--------------|
| 512GB Apple Studio | UD-IQ2_XXS (2-bit, ~241GB) |
| 256GB Mac | UD-IQ2_XXS 或 UD-TQ1_0 (1-bit, ~176GB) |

## 本地配置（可选）

启用 API Key 认证时，可复制模板并填入密钥：

```bash
cp .api-key.example .api-key
# 编辑 .api-key 填入实际 key（该文件已被 .gitignore 忽略，不会提交）
```

## 文档

- [DEPLOY.md](DEPLOY.md) - 完整部署指南
- [docs/architecture.md](docs/architecture.md) - 架构说明
