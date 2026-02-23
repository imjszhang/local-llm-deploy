#!/usr/bin/env python3
"""
从 ModelScope 魔搭社区下载 jina-embeddings-v5-text-small 嵌入模型
用法: python3 download_jina_embeddings.py

注意：此为 embedding 模型（safetensors 格式），非 GGUF 对话模型。
下载后需用 transformers / sentence-transformers 加载，或配合 embedding 服务使用。
"""
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(SCRIPT_DIR, "models")
REPO_ID = "jinaai/jina-embeddings-v5-text-small"
TARGET_DIR = os.path.join(MODELS_DIR, "jinaai-jina-embeddings-v5-text-small")


def main():
    try:
        from modelscope import snapshot_download
    except ImportError:
        print("正在安装 modelscope...")
        os.system(f"{sys.executable} -m pip install -q modelscope")
        from modelscope import snapshot_download

    print("=" * 50)
    print("jina-embeddings-v5-text-small 下载 (ModelScope)")
    print("=" * 50)
    print(f"仓库:     {REPO_ID}")
    print(f"目标目录: {TARGET_DIR}")
    print("大小约 1.4GB (safetensors)")
    print("=" * 50)

    os.makedirs(TARGET_DIR, exist_ok=True)

    snapshot_download(
        REPO_ID,
        cache_dir=os.path.join(MODELS_DIR, ".cache"),
        local_dir=TARGET_DIR,
    )

    st_count = sum(1 for f in os.listdir(TARGET_DIR) if f.endswith(".safetensors"))
    print("")
    print("=" * 50)
    print(f"下载完成！共 {st_count} 个 safetensors 文件")
    print(f"路径: {TARGET_DIR}")
    print("")
    print("使用示例 (Python):")
    print("  from sentence_transformers import SentenceTransformer")
    print(f'  model = SentenceTransformer("{TARGET_DIR}")')
    print("  embeddings = model.encode(['文本1', '文本2'])")
    print("=" * 50)


if __name__ == "__main__":
    main()
