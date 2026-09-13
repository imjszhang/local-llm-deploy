# 依赖安装入口

依赖分组定义在根目录 `pyproject.toml`，已验证版本位于 `constraints/`。本目录只引用对应 extra，不重复声明依赖和版本。

从项目根目录执行：

```bash
python -m pip install -r requirements/embedding.txt -c constraints/embedding-macos-py39.txt
```

可选入口为 `download.txt`、`embedding.txt`、`rerank.txt`、`whisper.txt`；也可以直接使用 `pip install -e '.[embedding]'` 等标准 extra。

根目录的重复转发文件已移除，已有安装脚本应替换为以下路径：

| 旧路径 | 当前路径 |
| --- | --- |
| `requirements.txt` | `requirements/download.txt` |
| `requirements-embedding.txt` | `requirements/embedding.txt` |
| `requirements-rerank.txt` | `requirements/rerank.txt` |
| `requirements-whisper.txt` | `requirements/whisper.txt` |

运行环境说明见 [部署指南](../docs/deployment.md)。
