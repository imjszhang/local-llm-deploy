# 部署指南

本文命令均从项目根目录执行。控制层支持 Python 3.9+；目前本机验证包含 Python 3.9.6 和 3.14.4。MLX 后端需 Apple Silicon。注册表和权重目录可沿用已有部署。

## 控制层

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'pip==25.3'
.venv/bin/python -m pip install -e .
./manage.sh registry init  # 已有 models.json 时跳过
./manage.sh config validate
```

控制层没有第三方运行依赖。旧 pip 21 不支持当前 editable 安装方式，需要先升级安装工具；这不升级模型库。

## 独立模型环境

下面列出的约束来自已安装依赖的传递闭包，版本验证结果见 [validation.md](validation.md)。Python 次版本和硬件平台必须匹配，其他组合应创建自己的约束基线。

```bash
# Embedding：已验证 Python 3.9 / macOS arm64
python3.9 -m venv .venv-embed
.venv-embed/bin/python -m pip install 'pip==25.3'
.venv-embed/bin/python -m pip install -e '.[embedding]' -c constraints/embedding-macos-py39.txt

# Rerank / Whisper：已验证 Python 3.14 / macOS arm64
python3.14 -m venv .venv-rerank
.venv-rerank/bin/python -m pip install -e '.[rerank]' -c constraints/rerank-macos-py314.txt
python3.14 -m venv .venv-whisper
.venv-whisper/bin/python -m pip install -e '.[whisper]' -c constraints/whisper-macos-py314.txt
```

实际解释器可使用完整路径；已有虚拟环境不要重复创建，升级采用新目录，见 [升级指南](upgrade.md)。Whisper 的启动描述会将项目 `tools/` 放入 PATH，`tools/ffmpeg` 使用安装的 imageio-ffmpeg。

也可通过 `requirements/` 中的依赖入口安装，例如 `pip install -r requirements/embedding.txt`。这些文件引用相应 extra；需要锁定版本时同时加 `-c constraints/...`。根目录依赖转发文件已移除，旧路径对应关系见 [依赖入口](../requirements/README.md)。

## 模型下载与安装选择

```bash
.venv/bin/python -m pip install -e '.[download]' -c constraints/download-macos-py39.txt
./manage.sh download <模型键> --dry-run
./manage.sh download <模型键> --quant <量化> --source huggingface --revision <revision>
./manage.sh models
```

下载源优先级为命令行 > 环境变量 > 模型配置 > ModelScope。`.hf-env` 仅提供未被环境覆盖的字面值，不执行 Shell。Hugging Face 未设置 endpoint 时沿用 hf-mirror；使用官方 Hub 可导出 `HF_ENDPOINT=https://huggingface.co`。

`--to` 相对 `models/`，目标必须位于其子目录。`register --path` 相对项目根。登记后的实际路径由所有管理命令共用；同量化存在多个登记时，启动必须指定 `--model-dir`。

## llama.cpp

首次下载源码需要明确 commit：

```bash
./manage.sh engine init --revision <40位commit>
./manage.sh engine build
./manage.sh engine register stable --directory "$PWD/llama.cpp" --default
```

已有构建可直接登记，无需重编。特定模型使用不同构建时，在其配置加 `engine_profile`，或继续使用 `CPP_DIR`：

```bash
CPP_DIR="$PWD/work_dir/llama.cpp-qwen38" ./manage.sh plan qwen3.8-27b-aggressive
CPP_DIR="$PWD/work_dir/llama.cpp-qwen38" ./manage.sh start qwen3.8-27b-aggressive
```

`plan` / `--dry-run` 展示脱敏 argv、路径和运行方式，不启动进程。

## 启动与停止

```bash
./manage.sh start <模型键>
./manage.sh start <模型键> --port 8002 --host 127.0.0.1
./manage.sh start serve-ui
./manage.sh status --probe
./manage.sh stop <模型键>
./manage.sh stop serve-ui
```

macOS 的辅助服务与网关沿用 launchd；GGUF 默认普通后台进程。可通过 `--management process|launchd` 显式选择。端口占用会报错，不停止无关进程。不同模型的自动启动/异常恢复策略保留原设定。

配置 `.api-key` 后，所有模型代理入口都要求 Bearer Key；两个只读监控概览接口仍公开。健康检查不等于完整推理验证。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
./scripts/test-services.sh --proxy 8888 --jina 8004 --rerank 8006 \
  --whisper 8007 --audio /path/to/short.wav --chat-model <模型键>
```

真实测试按顺序调用，会执行实际推理。省略可选参数时不测试对应后端。新旧网关连接同一真实模型时应串行验证，不镜像推理请求。
