# Whisper ASR

服务使用 `mlx-whisper`，源码位于 `src/local_llm_deploy/services/whisper.py`。当前已安装版本的 Python 3.14 / Apple Silicon 约束见 `constraints/whisper-macos-py314.txt`；重建结果见 [验证记录](validation.md)。

## 安装、下载与启动

按 [部署指南](deployment.md) 创建独立 `.venv-whisper`。音频解码通过 `imageio-ffmpeg` 提供的程序，项目 `tools/ffmpeg` 为兼容启动入口。

```bash
./manage.sh download whisper-large-v3
./manage.sh plan whisper-large-v3
./manage.sh start whisper-large-v3
./manage.sh status whisper-large-v3 --probe
```

模型注册条目使用 `type: "asr"`，可配置 alias、default_port、repo_id、repo_name，以及 params.language / task / response_format。下载模式应包含 `*.json`、`*.npz`、`*.safetensors` 等实际权重文件；示例见 [模型注册表模板](../config/examples/models.json.example)。

## API

```bash
curl http://localhost:8888/v1/audio/transcriptions \
  -H 'Authorization: Bearer <Key>' \
  -F file=@short.wav -F model=whisper-large-v3 \
  -F language=zh -F response_format=verbose_json
```

`response_format` 支持 text、json 和 verbose_json。multipart 中的音频始终按二进制处理，临时文件在成功和失败路径均会删除；verbose_json 可包含分段信息。

ASR 在网关中使用独立 lane，默认并发 1；服务内也有推理锁。模型加载和端口绑定成功后才发布就绪状态。若选择其他解释器，通过 `runtime.python` 设置并检查启动 plan。

## 验证

```bash
./scripts/test-services.sh --proxy 8888 --jina 8004 --whisper 8007 --audio /path/to/short.wav
./manage.sh logs whisper-large-v3
./manage.sh stop whisper-large-v3
```

401 检查 Key，503 检查已注册模型与服务就绪状态；`ffmpeg` 错误检查当前 Whisper 环境的 imageio-ffmpeg 安装。执行依赖升级时保留旧环境并使用候选目录，见 [升级指南](upgrade.md)。
