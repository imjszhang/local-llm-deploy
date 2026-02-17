#!/bin/bash
# llama.cpp 编译脚本
# 某些模型可能需要特定 PR（如 GLM-5 需 PR 19460），已合入 main 则无需额外操作
# 若网络较慢，可分别执行各步骤

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CPP_DIR="${CPP_DIR:-$SCRIPT_DIR/llama.cpp}"

echo "========================================"
echo "llama.cpp 编译"
echo "========================================"
echo "CPP_DIR: $CPP_DIR"
echo ""

cd "$CPP_DIR"

# 1. 尝试应用 PR 19460（部分模型需要，如 GLM-5；若已合入 main 会自动跳过）
echo "[1/3] 检查 PR 19460..."
if git fetch origin pull/19460/head:MASTER 2>/dev/null; then
    git checkout MASTER
    echo "  已切换到 PR 19460 分支"
else
    echo "  PR 19460 已合入 main 或获取失败，使用当前分支继续"
fi

# 2. 配置
echo "[2/3] CMake 配置..."
cmake -B build -DCMAKE_BUILD_TYPE=Release

# 3. 编译
echo "[3/3] 编译 llama-server..."
cmake --build build --target llama-server llama-cli -j

echo ""
echo "完成！llama-server: $CPP_DIR/build/bin/llama-server"
