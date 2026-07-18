# Whisper ASR 模型使用指南

本文档介绍本项目中 **Whisper Large V3 (MLX)** 的配置、下载、启动与调用方式。该模型用于语音转文字（ASR），提供 OpenAI 兼容的 `/v1/audio/transcriptions` 接口。

---

## 一、在项目中的角色

| 项目内名称 | 模型 ID（API 用） | 类型 | 默认端口 | 格式 | 磁盘占用 |
|------------|------------------|------|----------|------|----------|
| `whisper-large-v3` | `whisper-large-v3` | asr | 8007 | MLX (npz/safetensors) | ~3GB |

- **推理框架**：`mlx-whisper`（Apple Silicon GPU / Metal）
- **权重来源**：HuggingFace [`mlx-community/whisper-large-v3-mlx`](https://huggingface.co/mlx-community/whisper-large-v3-mlx)（MLX 预转换，非 PyTorch 原版）
- **路由**：`serve-ui.py`（8888）将 `/v1/audio/transcriptions` 按 multipart 中的 `model` 字段转发到 ASR 后端

---

## 二、前置依赖

**Python 版本**：推荐 3.12 或 3.13。当前 Homebrew 默认 3.14 时，`numba` / `tiktoken` 等可能尚无预编译 wheel，需等待上游支持或自行安装 Python 3.12+ 创建 venv。

```bash
# 音频解码（mlx-whisper 必需）
brew install ffmpeg

# Python 虚拟环境
python3 -m venv .venv-whisper
source .venv-whisper/bin/activate
pip install -r requirements-whisper.txt
```

若 `tiktoken` 编译失败，可尝试仅安装 wheel：

```bash
pip install 'tiktoken>=0.10' --only-binary=:all:
```

---

## 三、配置（models.json）

首次使用可从模板注册：

```bash
./manage.sh registry init   # 若尚无 models.json
./manage.sh registry merge models.json.example
```

或手动添加/合并以下条目：

```json
"whisper-large-v3": {
  "type": "asr",
  "full_model_name": "Whisper Large V3 (MLX)",
  "download_source": "huggingface",
  "repo_id": "mlx-community/whisper-large-v3-mlx",
  "repo_name": "mlx-community-whisper-large-v3-mlx",
  "alias": "whisper-large-v3",
  "default_port": 8007,
  "download_allow_patterns": ["*.json", "*.npz", "*.safetensors", "*.txt", "tokenizer*"],
  "params": {
    "language": "zh",
    "task": "transcribe",
    "response_format": "json"
  }
}
```

- **language**：默认转写语言（可在 API 请求中覆盖）
- **task**：`transcribe`（同语言转写）；暂不支持 `translate`
- **response_format**：`json`（默认）、`text`、`verbose_json`

---

## 四、下载模型

```bash
./manage.sh download whisper-large-v3
```

- 从 HuggingFace 下载到 `models/mlx-community-whisper-large-v3-mlx/`
- 不支持 `--quant`
- 可用 `./manage.sh models` 查看目录状态

---

## 五、启动服务

```bash
./manage.sh start whisper-large-v3
./manage.sh status
```

- 使用 `serve_whisper.py`，默认 **127.0.0.1:8007**
- venv 优先级：`.venv-whisper` → `.venv-rerank` → `.venv-embed` → `.venv`
- 日志：`logs/whisper-large-v3.log`
- 首次启动会预热加载模型（约数秒）

可选参数：

```bash
./manage.sh start whisper-large-v3 --port 8007 --lan   # 监听 0.0.0.0
```

---

## 六、API 调用

### 6.1 直连后端

```bash
curl -X POST http://127.0.0.1:8007/v1/audio/transcriptions \
  -F file=@audio.mp3 \
  -F model=whisper-large-v3 \
  -F language=zh
```

### 6.2 经 serve-ui 代理（推荐）

```bash
curl -X POST http://localhost:8888/v1/audio/transcriptions \
  -H "Authorization: Bearer <你的API-Key>" \
  -F file=@audio.mp3 \
  -F model=whisper-large-v3 \
  -F language=zh
```

### 6.3 带 model-key 前缀

```bash
curl -X POST http://localhost:8888/api/whisper-large-v3/v1/audio/transcriptions \
  -F file=@audio.mp3
```

### 6.4 响应格式

| response_format | 返回 |
|-----------------|------|
| `json`（默认） | `{"text":"..."}` |
| `text` | 纯文本 |
| `verbose_json` | 含 `segments` 等详细信息 |

---

## 七、与现有模型并行

Whisper 占用约 3–8GB 内存，可与 ds4flash、jina-embed 等同时运行：

```bash
./manage.sh start whisper-large-v3
./manage.sh start jina-embed
./manage.sh status
```

---

## 八、故障排查

| 问题 | 处理 |
|------|------|
| `ffmpeg not found` | `brew install ffmpeg` |
| 模型目录不存在 | `./manage.sh download whisper-large-v3` |
| `No module named mlx_whisper` | 安装 `.venv-whisper` 依赖 |
| 503 No running ASR models | 先 `./manage.sh start whisper-large-v3` |
| 下载慢 | 检查 `.hf-env` 或 `HF_ENDPOINT` |

---

## 九、相关文件

| 文件 | 说明 |
|------|------|
| `serve_whisper.py` | ASR HTTP 服务 |
| `requirements-whisper.txt` | mlx-whisper 依赖 |
| `model_paths.py` | `dir_has_asr_weights()` 权重检测 |
| `docs/architecture.md` | 系统架构 |
