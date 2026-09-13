# 依赖约束

Core 运行时仅用标准库。`core-macos-py39.txt` 因而没有第三方运行依赖；`dev.txt` 固定本轮验证的检查工具。

其余文件分别记录下载、Embedding、Rerank、Whisper 依赖的传递闭包。版本来自本机重构前已安装元数据，并依据实际 Python 版本与环境 marker 选择依赖。没有把某个环境中无关的历史包一起冻结。

| 文件 | 平台 / Python | 安装入口 |
| --- | --- | --- |
| download-macos-py39.txt | macOS arm64 / 3.9 | `.[download]` |
| embedding-macos-py39.txt | macOS arm64 / 3.9 | `.[embedding]` |
| rerank-macos-py314.txt | macOS arm64 / 3.14 | `.[rerank]` |
| whisper-macos-py314.txt | macOS arm64 / 3.14 | `.[whisper]` |

在对应解释器的新虚拟环境执行 `python -m pip install -e '.[分组]' -c constraints/对应文件`，然后 `python -m pip check`。Python 3.9 editable 安装先显式安装 `pip==25.3`。其他平台需要单独解析验证，不能直接宣称适用这些平台约束。

升级流程和实际安装、推理验证范围分别见 [upgrade.md](../docs/upgrade.md)、[validation.md](../docs/validation.md)。约束固定版本，完整性由安装源和 wheel 校验负责；本轮离线缓存安装额外保存了每个 wheel 的来源与 SHA256。

已显式安装项目对应 extra、且 `pip check` 通过后，可以从该环境生成候选约束：

```bash
.venv-embed-next/bin/python scripts/snapshot-dependencies.py --extra embedding > /tmp/embedding-candidate.txt
```

脚本依据 installed metadata、环境 marker 和 extra 递归收集依赖，检查已安装版本是否符合要求，不读取整个环境的无关包。它需要该环境中的 `packaging`。输出先保存在候选文件，按该文件再建新环境并验收通过后，才替换入库约束。
