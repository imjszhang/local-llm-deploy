# 本机 Qwen3-TTS 部署

模型服务监听 `127.0.0.1:8008`，已接入统一网关 `127.0.0.1:8888`，监控页提供语音合成通道。

## 环境与模型

- 模型：`mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16`
- 固定 revision：`a6eb4f68e4b056f1215157bb696209bc82a6db48`
- 路径：`models/mlx-community-Qwen3-TTS-12Hz-1.7B-Base-bf16/`
- 推理环境：`.venv-tts`，Python 3.14，`mlx-audio==0.5.4`、`mlx==0.32.0`。
- 主权重约 3.86 GB，speech tokenizer 约 0.68 GB，合计约 4.54 GB。
- 实测 HF 国内镜像目录 API 可访问，但此仓库的 resolve 链接返回 HTTP 308 跳回官方源，不能提供独立镜像下载。本次改用 HF 官方源。Python 依赖使用本机 pip 缓存与清华 PyPI 镜像；阿里云源也测试过，但本次未用它完成安装。

## 安装与管理

```bash
python3.14 -m venv .venv-tts
.venv-tts/bin/python -m pip install -r requirements/tts.txt -c constraints/tts-macos-py314.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
./manage.sh registry merge config/examples/qwen3-tts.patch.json
HF_ENDPOINT=https://huggingface.co HF_HUB_DISABLE_XET=1 ./manage.sh download qwen3-tts-1.7b-base --source huggingface
./manage.sh plan qwen3-tts-1.7b-base
./manage.sh start qwen3-tts-1.7b-base
./manage.sh status --probe
./manage.sh logs qwen3-tts-1.7b-base
./manage.sh stop qwen3-tts-1.7b-base
```

下载命令需控制环境已安装 download 依赖；与项目现有下载流程一致。服务通过 launchd 管理；该模型 `run_at_load:false`，需显式启动，不在每次登录时自动加载。

## 首版接口

`POST http://127.0.0.1:8888/v1/audio/speech`，Bearer 凭据沿用项目 `.api-key`。

```json
{
  "model": "qwen3-tts",
  "input": "This is a local speech synthesis test.",
  "ref_audio_base64": "<WAV 文件的 base64>",
  "ref_text": "参考人声对应的准确文字",
  "language": "English",
  "response_format": "wav"
}
```

这是项目自定义的声音克隆扩展。参考为 1–30 秒、8–48 kHz、单/双声道 16-bit PCM WAV；输入和参考原文各不超过 2000 字符。仅支持完整 WAV 输出，`stream:true`、MP3 等格式会明确拒绝。模型生成使用服务内串行锁，全部输出片段按序拼接。请求临时参考文件会清理；服务不记录请求正文。

`GET /health` 返回模型/编码器成功加载后的健康状态。该接口无需 Key，与现有服务一致。下载完整性检查包含 speech tokenizer 和 safetensors 索引分片。

部署验证及试听文件存于已忽略的 `work_dir/tts/`；参考录音不提交仓库。测试语音应作为合成音频使用，不代表原说话者的新发言。

## 调用示例

无需在命令行中填写 Key（脚本从项目 .api-key 读取）：

```bash
python3 scripts/test-tts.py --reference work_dir/tts/reference.wav \
  --reference-text "参考音频的准确文字" \
  --text "This is a local speech synthesis test." \
  --output work_dir/tts/test.wav
```

替换参考录音和原文即可使用其他音色；更改 `--language` 可测试其他语言。`--language` 只是语言选择，不保证跨语言音色效果，需要单独试听。

## 本次验收（2026-09-20）

- 14 个仓库文件大小检查通过，两份权重的 SHA-256 与上游 LFS oid 一致。
- Python 3.14.4、mlx-audio 0.5.4、MLX/Metal 0.32.0、Transformers 5.17.0；`pip check` 通过；精确依赖见 `constraints/tts-macos-py314.txt`。
- 服务在 `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` 下成功加载；实际启动、停止和重启验证。
- 参考使用原视频开头完整短句（3.1 秒），生成 `work_dir/tts/voice-clone-test-v2.wav`：24 kHz、单声道、4.48 秒；本次已加载模型的请求用时 1.531 秒，不作为通用性能保证。
- 新文案：`This is a local speech synthesis test. The model is now running on this Mac.`；现有 Whisper 服务回转写与文案一致。
- 初次使用 6.4 秒参考时带出了句尾 RLHF，切换完整短句后未出现；保留两次验证记录。参考选段会影响效果，转写一致不等于音色相似度已完成人工验收。
- core 45 项通过；lifecycle 37 项中 4 项按现有配置跳过，其余通过；services 首轮 29 项通过，新增部署契约后 TTS 专项 5 项通过。

统一网关已重启生效，原有模型服务继续运行。TTS 使用独立队列，默认并发 1（TTS_LANE_CONCURRENT）。此模型已登记为默认 TTS，省略 model 时可使用；也可通过 DEFAULT_TTS_MODEL 覆盖。语音请求和响应正文不写入网关内容日志。

通过 8888 实测生成 4.64 秒 WAV，请求耗时 1.605 秒，Whisper 转写与文案一致。网关专项测试、前端 78 项测试、类型检查和构建通过；真实浏览器已确认 TTS 通道显示。
