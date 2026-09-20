# Qwen3-TTS 接入方案

2026-09-20。状态：独立服务、下载、注册和部署已实施，见 [部署记录](qwen3-tts-deployment.md)。网关、调度和监控也已接入；实际接口以部署记录为准。

## 决策

在现有独立 Python 模型服务体系中增加 `tts` 能力和 `mlx_tts` 后端，复用统一认证、安装清单、生命周期、网关路由和监控。首个模型为 `mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16`，用于参考音频克隆。Base 没有预置 speaker；CustomVoice 和 VoiceDesign 是不同模型/方法，不混用其参数。

采用项目自己的薄 HTTP 适配器调用 mlx-audio Python API，不直接把上游完整多模型服务器作为受管服务。这样每个服务只加载注册表指定的本地模型，请求不能任意下载/切换模型。

推理运行在独立 `.venv-tts`，建议先用 Python 3.12 验证。mlx-audio 上游当前要求 Python >=3.10；控制层继续维持 Python >=3.9 和零推理依赖。选择实际可用的发行版本或固定 commit，完成真实推理后再生成 constraints，不能把 main 分支依赖声明直接当作已验证版本。

## 首版接口

统一入口 `POST /v1/audio/speech`，复用 Bearer Key。首版只支持完整 WAV 响应，不支持音频 streaming；请求 `stream:true` 必须明确拒绝，不能进入现有聊天 SSE 流程。

建议首版 JSON 扩展：

```json
{
  "model": "qwen3-tts-1.7b-base",
  "input": "Here is the new narration.",
  "ref_audio_base64": "<base64 encoded WAV bytes>",
  "ref_text": "Exact transcript of the reference recording.",
  "language": "English",
  "response_format": "wav"
}
```

这是沿用常见 speech 路径的项目扩展，并非声明完整 OpenAI 协议兼容。返回 `Content-Type: audio/wav`，由客户端存为文件。缺少参考音频/原文、格式不支持、字段错误返回 400；请求过大返回 413；未知/离线模型维持现有 404/503 语义。

`ref_audio_base64` 解码到服务自己的临时文件，结束或失败后清理。限制文本长度、请求体大小、解码后音频时长和生成长度；只接受首版验证过的 WAV 格式。不接受调用者传入服务器路径或远程 URL，避免客户端路径错位、服务器文件读取和任意网络请求。参考音频不写日志。

后续可增加受控 `voice` 档案 ID，保存经过登记的参考音频和原文，减少重复上传。管理档案的写接口需要独立设计，首版无需同时实现。

## 服务实现

新增 `src/local_llm_deploy/services/tts.py`：

- 复用 `ServiceHTTPHandler`、`configure_service`、`run_service` 和错误/认证边界。
- 服务启动时从 `LOCAL_LLM_MODEL_DIR` 加载一个模型，实际加载完成后才就绪；提供 `/health`。
- 延迟导入 mlx-audio，控制层和无权重测试不能导入 MLX。
- 包装 `load_model(local_dir)` 和 Base 的 `model.generate(text=..., ref_audio=..., ref_text=..., language=...)`；以选定版本的源码签名为准。
- 生成迭代器可能产生多段，按顺序拼接所有结果，读取实际采样率并验证一致性，不能只拿 `results[0]` 导致长文截断。
- 使用服务级推理锁；首版并发 1。输出合法 PCM WAV，经现有 `_response` 返回。
- 不在请求里隐式调用 Whisper 自动转写参考音频，避免双模型加载和依赖扩大；参考转写由调用方准备。

## 仓库改动位置

| 文件/模块 | 改动 |
| --- | --- |
| `config.py` | capability 校验加入 tts；明确 mlx_tts 后端；如提供 type:tts 简写，则同步 normalize 映射 |
| `domain.py` | tts 默认端点 `/v1/audio/speech` |
| `backends/builders.py` | `.venv-tts` 解释器选择、TTS_HOST/TTS_PORT、启动 argv、就绪超时和 launchd 行为 |
| `lifecycle/observe.py` | 新服务的进程身份识别；必须与 argv 一致，验证 stop/reconcile 不误识别 |
| `services/tts.py` | 单模型推理、参数校验、WAV 输出和清理 |
| `artifacts/paths.py` | 检查完整模型、索引分片、文本 tokenizer 与 speech tokenizer；不能只判断顶层存在一个 safetensors |
| `gateway/routing.py` | 新端点到 tts 能力的映射；JSON 模型选择；首版禁用 stream |
| `gateway/settings.py` | DEFAULT_TTS_MODEL、TTS_LANE_CONCURRENT，并加入 GATEWAY_ENV_NAMES |
| `gateway/scheduling.py` | tts lane，默认并发 1；复用有界队列和释放逻辑 |
| `observability.py` | tts 请求体不记录参考音频/base64，不把 WAV 当文本日志抓取 |
| `frontend/src/features/monitor/` | LaneKey、DTO 校验、lane 卡片加入 tts |
| `frontend/src/shared/` | capability/backend 名称与样式；ASR 与 TTS 分别显示 |
| `pyproject.toml`、`requirements/tts.txt` | 独立 tts extra/安装入口；安装只在 .venv-tts 进行 |
| `config/examples/models.json.example` | 新模型配置示例，不覆盖真实注册表 |
| `docs/deployment.md`、`docs/api-guide.md` | 安装、启动、调用和错误语义 |

入口优先采用已安装包的 `python -m local_llm_deploy.services.tts`，不新增根目录业务脚本；相应调整并测试 observe 对模块入口的识别。现有旧服务入口保持兼容。

## 注册表示例（实现完成后才可使用）

```json
{
  "qwen3-tts-1.7b-base": {
    "capabilities": ["tts"],
    "backend": "mlx_tts",
    "management": "managed",
    "full_model_name": "Qwen3-TTS 1.7B Base (MLX)",
    "repo_id": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
    "repo_name": "mlx-community-Qwen3-TTS-12Hz-1.7B-Base-bf16",
    "download_source": "huggingface",
    "alias": "qwen3-tts",
    "default_port": 8008,
    "runtime": {"python": ".venv-tts/bin/python", "management": "launchd", "ready_timeout": 300},
    "params": {"max_concurrent": 1}
  }
}
```

8008 只是建议，部署前检查真实配置和监听端口。权重先下载完整仓库快照，检查选定 revision 的 speech tokenizer 是否已打包，若加载器另行拉取依赖则显式纳入下载/安装清单。随后验证离线加载；不能承诺只靠根目录权重就完整可用。

## 实施与验收顺序

1. 核实发行版 API、权重仓库内容和依赖；独立环境做直接 Python 短句克隆，记录耗时/峰值内存/采样率并人工试听。
2. 接入服务、模型注册/安装完整性和生命周期。使用假模型测试认证、输入错误、所有音频片段拼接、临时文件清理、单并发。
3. 接入网关、调度、默认模型和监控。测试 WAV 字节原样转发，输出不能夹入 SSE 心跳；覆盖未知模型、离线、能力不匹配、队列满和断连。
4. 真实验收 `plan/start/status --probe/stop` 与网关生成；停止/重启后状态准确。前端改动通过现有测试并构建到 static，不能手改构建产物。
5. video-remake 调用网关获取 WAV，落到其 productions 资产台账，再交给已有口播生成、转写和视频编排。部署项目不承担视频剪辑。

首版不同时加入声音管理 UI、长任务 API、实时音频流、CustomVoice、VoiceDesign 或训练。先验证克隆质量和可复现性；音色相似度和语气需试听，服务/单元测试通过不等于声音验收通过。

## 上游依据

- https://github.com/Blaizzy/mlx-audio/blob/main/docs/models/tts/qwen3-tts.md
- https://github.com/Blaizzy/mlx-audio/blob/main/pyproject.toml
- https://github.com/QwenLM/Qwen3-TTS
