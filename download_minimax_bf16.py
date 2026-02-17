#!/usr/bin/env python3
"""
从 ModelScope 魔搭社区下载 MiniMax-M2.5-GGUF BF16 版本
用法: python3 download_minimax_bf16.py
"""
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(SCRIPT_DIR, "models")
TARGET_DIR = os.path.join(MODELS_DIR, "MiniMax-M2.5-GGUF")

def main():
    try:
        from modelscope import snapshot_download
    except ImportError:
        print("正在安装 modelscope...")
        os.system(f"{sys.executable} -m pip install -q modelscope")
        from modelscope import snapshot_download

    print("=" * 50)
    print("MiniMax-M2.5-GGUF BF16 下载 (ModelScope)")
    print("=" * 50)
    print(f"目标目录: {TARGET_DIR}")
    print("大小约 457GB，请确保磁盘空间充足")
    print("=" * 50)

    os.makedirs(TARGET_DIR, exist_ok=True)

    snapshot_download(
        "unsloth/MiniMax-M2.5-GGUF",
        cache_dir=os.path.join(MODELS_DIR, ".cache"),
        local_dir=TARGET_DIR,
        allow_patterns=["BF16/*"],
    )

    bf16_dir = os.path.join(TARGET_DIR, "BF16")
    if os.path.isdir(bf16_dir):
        gguf_count = sum(1 for f in os.listdir(bf16_dir) if f.endswith(".gguf"))
        print("")
        print("=" * 50)
        print(f"下载完成！共 {gguf_count} 个 GGUF 文件")
        print(f"路径: {bf16_dir}")
        print("")
        print("启动命令: ./manage.sh start minimax")
        print("=" * 50)
    else:
        print("警告: 未找到 BF16 目录，请检查下载是否完整")

if __name__ == "__main__":
    main()
