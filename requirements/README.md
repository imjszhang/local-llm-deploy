# 依赖安装入口

依赖分组定义在根目录 `pyproject.toml`，已验证版本位于 `constraints/`。本目录只引用对应 extra，不重复声明依赖和版本。

从项目根目录执行：

```bash
python -m pip install -r requirements/embedding.txt -c constraints/embedding-macos-py39.txt
```

可选入口为 `download.txt`、`embedding.txt`、`rerank.txt`、`whisper.txt`；也可以直接使用 `pip install -e '.[embedding]'` 等标准 extra。

根目录 `requirements.txt`、`requirements-*.txt` 是旧路径的转发文件，继续兼容已有安装命令。运行环境说明见 [部署指南](../docs/deployment.md)。
