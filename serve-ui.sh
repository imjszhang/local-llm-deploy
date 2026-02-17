#!/bin/bash
# 启动前端静态服务（监控面板 + 多模型 API 代理）
# 内置 /api 代理，自动读取 .api-key，支持多模型路由

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
exec python3 serve-ui.py
