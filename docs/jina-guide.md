# Jina Embedding 与 Rerank

模型服务源码位于 `src/local_llm_deploy/services/`。根目录 Python 服务入口与 `manage.sh` 命令保留，手动 `jina.sh` 已移至 `scripts/compat/`；模型、端口和解释器通过统一配置解析。

## 安装与启动

依赖安装和锁定版本见 [部署指南](deployment.md)。Embedding 使用 `.venv-embed`，Rerank 使用 `.venv-rerank`；可通过模型 `runtime.python` 指定其他环境。

```bash
./manage.sh download jina-embed
./manage.sh download jina-rerank-mlx
./manage.sh plan jina-embed
./manage.sh start jina-embed
./manage.sh start jina-rerank-mlx
./manage.sh status --probe
```

同类模型无需修改 Jina Shell 脚本，在注册表设置不同 key、alias、repo_id 和端口即可。macOS 默认通过 launchd 管理，日志位于 `logs/<模型键>.log`。

## Embedding

`POST /v1/embeddings` 接收字符串或字符串数组的 `input`。`task` 可选 text-matching、retrieval、classification、clustering，兼容 retrieval.query / retrieval.passage。`dimensions` 在模型支持范围内进行截断并归一化。

```bash
curl http://localhost:8888/v1/embeddings \
  -H 'Authorization: Bearer <Key>' -H 'Content-Type: application/json' \
  -d '{"model":"jina-embed","input":["北京是中国首都"],"task":"retrieval.passage","dimensions":256}'
```

实现直接使用 Torch / Transformers，自动选择 MPS、CUDA 或 CPU。锁覆盖 LoRA adapter 切换及推理，避免并发任务互相切换 adapter。返回向量顺序与输入一致，并包含 token usage。

## Rerank

`POST /v1/rerank` 接收 query、documents，可选 top_n、return_documents、return_embeddings；结果包含 index 与 relevance_score。默认最多 64 个文档，可通过 params.max_documents 调整。

```bash
curl http://localhost:8888/v1/rerank \
  -H 'Authorization: Bearer <Key>' -H 'Content-Type: application/json' \
  -d '{"model":"jina-rerank-mlx","query":"北京","documents":["北京是中国首都","苹果是一种水果"],"top_n":1}'
```

Rerank 使用 MLX 与模型仓库附带的 `rerank.py`，其 revision 应和权重一起固定。模型调用有独立互斥锁，网关也有默认并发 1 的 Rerank lane。

## 验证与排查

```bash
./scripts/test-services.sh --jina 8004 --rerank 8006 --proxy 8888
./manage.sh logs jina-embed
./manage.sh logs jina-rerank-mlx
```

401 检查 Key；503 检查 `status --probe`、就绪日志和路由能力；依赖错误在相应虚拟环境执行 `pip check`。新增安装路径通过 `register` 登记，多安装时以 `--model-dir` 明确选择。

详细认证、排队和错误规则见 [API 指南](api-guide.md)。
