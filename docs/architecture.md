# Local LLM Deploy 系统架构说明

> 文档更新时间：2026-02-17

---

## 一、系统概览

Local LLM Deploy 采用**多实例部署架构**：每个模型运行一个独立的 llama-server 进程，通过 `manage.sh` 统一管理，前端代理自动路由到各后端。

```
┌─────────────────────────────────────────────────────────────────┐
│                    用户 / OpenAI 客户端                            │
│                                                                  │
│  http://localhost:8001/v1 (GLM-5)                                │
│  http://localhost:8002/v1 (Qwen3.5)                              │
│  http://localhost:8888/   (前端监控，自动路由)                       │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                      宿主机推理服务                                │
│                                                                  │
│  ┌────────────────────────────────┐  ┌──────────────────────┐   │
│  │  llama-server (GLM-5)          │  │  llama-server         │   │
│  │  端口: 8001                    │  │  (Qwen3.5)            │   │
│  │  PID: run/glm-5.pid           │  │  端口: 8002            │   │
│  │  日志: logs/glm-5.log         │  │  PID: run/qwen3.5.pid │   │
│  └────────────────────────────────┘  └──────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  serve-ui.py (前端 + API 代理)                            │   │
│  │  端口: 8888                                              │   │
│  │  /api/models → 模型列表                                   │   │
│  │  /api/<model>/* → 路由到对应 llama-server                 │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  models.json (模型注册中心)                               │   │
│  │  manage.sh (统一 CLI: list/download/start/stop/status)    │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 二、服务组件

| 组件 | 端口 | 说明 |
|------|------|------|
| **llama-server** (每模型一个) | 由 models.json 配置 | C++ 推理引擎，OpenAI 兼容 API |
| **serve-ui.py** | 8888 | 前端静态服务 + 多模型 API 代理 |
| **manage.sh** | - | 统一 CLI，管理所有模型生命周期 |

---

## 三、数据流

```
OpenAI Client / curl → llama-server(:8001/:8002) → GGUF 模型
                              ↓
OpenAI Client / curl ← JSON 响应 ←

浏览器 → serve-ui.py(:8888) → /api/<model>/* → llama-server
                              → /api/models → 运行中模型列表
```

---

## 四、模型管理

### models.json 结构

每个模型注册以下信息：
- `repo_id`: HuggingFace 仓库 ID
- `quants`: 可用量化版本及磁盘占用
- `alias`: llama-server 的 --alias 参数
- `default_port`: 默认端口
- `params`: 推理参数（temp, top_p, ctx_size 等）

### 进程管理

| 文件 | 用途 |
|------|------|
| `run/<model>.pid` | PID 文件（第一行 PID，第二行端口号） |
| `logs/<model>.log` | 独立日志文件 |

---

## 五、已注册模型

| 模型 | 端口 | 量化版本 | 大小 |
|------|------|----------|------|
| GLM-5 | 8001 | UD-IQ2_XXS (2-bit), UD-TQ1_0 (1-bit) | 241GB / 176GB |
| Qwen3.5 | 8002 | MXFP4_MOE (4-bit), UD-Q4_K_XL, UD-Q2_K_XL | 214GB / 214GB / 120GB |

---

## 六、关键配置文件

| 文件 | 说明 |
|------|------|
| `models.json` | 模型注册中心 |
| `manage.sh` | 统一 CLI 入口 |
| `deploy.sh` | 通用部署脚本 |
| `download.sh` | 通用下载脚本 |
| `serve-ui.py` | 前端 + 多模型代理 |

---

## 七、常用运维命令

```bash
# 管理模型
./manage.sh list
./manage.sh start glm-5
./manage.sh start qwen3.5
./manage.sh status
./manage.sh stop --all

# 日志
./manage.sh logs glm-5

# 监控
./monitor.sh health --model glm-5
./monitor.sh metrics --model qwen3.5

# 前端
./serve-ui.sh
```
