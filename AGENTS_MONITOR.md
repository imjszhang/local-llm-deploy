# AI 探员总部 - 本地 AI Agent 监控系统

## 概述

实时监控本地运行的 AI agents（Claude Code、Codex、OpenClaw、Grok Bot），将它们的活动可视化为一个虚拟办公楼。每个 agent 类型占据一层楼，每个活跃会话是一个低面卡通风格的"员工"坐在工位前工作。

## 已完成功能

### 后端 (Python)

**文件：`src/local_llm_deploy/gateway/agents_api.py` (395 行)**

- **AgentCollector** 基类：负责采集单个 agent 类型的会话数据
- **ClaudeCodeCollector**：
  - 监控 `~/.claude/projects/*/*.jsonl`
  - 提取任务开始、工具调用（Bash/Read/Edit/Write/WebFetch）、token 消耗
- **CodexCollector**：
  - 监控 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`（最近 7 天）
  - 提取 task_started、custom_tool_call、token_count、task_complete
- **AgentMonitor**：
  - 后台线程每 0.5 秒采集一次
  - 订阅者模式，通过 queue 广播事件
  - 提供 `snapshot()` 和 WebSocket 流式推送

**API 端点：**
- `GET /agents-api/v1/snapshot` → 返回当前所有会话的快照 JSON
- `GET /agents-api/v1/stream` → WebSocket，实时推送新事件

**数据脱敏：**
- 移除 API keys (`sk-...`)、Bearer tokens、密码
- 任务描述截断到 200 字符，工具目标截断到 120 字符

### 前端 (TypeScript + Three.js)

**文件结构：**
```
frontend/
├── agents.html                   # 入口 HTML
├── vite.agents.config.ts         # 独立构建配置
├── scripts/export-agents.mjs     # 发布脚本
└── src/agents/
    ├── main.ts       (33 行)     # 入口：连接 scene + ws + ui
    ├── scene.ts      (511 行)    # Three.js 场景：楼层、角色、动画
    ├── ws.ts         (58 行)     # WebSocket 客户端（自动重连）
    ├── ui.ts         (190 行)    # HTML 覆盖层：标题栏、状态、详情面板
    └── types.ts      (38 行)     # TypeScript 类型定义
```

**3D 场景特性：**

1. **楼层布局**（垂直堆叠）：
   - 0 层 (y=0)：档案室 - 已完成的会话以灰色显示
   - 1 层 (y=5)：Claude Code 楼层 - 橙色主题 (#e07050)
   - 2 层 (y=10)：Codex 楼层 - 紫色主题 (#8b5cf6)
   - 3 层 (y=15)：预留楼层

2. **每层楼结构**：
   - 地板、天花板、后墙、侧墙
   - 前面开放（相机视角朝向前方）
   - 玻璃幕墙（透明度 0.15，蓝色调）
   - 最多 6 个工位（2 排 × 3 列）

3. **工位组成**：
   - 桌面：1.8 × 0.08 × 1.0 单位
   - 显示器：带发光屏幕，颜色对应 agent 类型
   - 椅子

4. **低面卡通角色**（~1.5 单位高）：
   - 头部：6 面球体（低面数）
   - 身体：方块
   - 四肢：方块，带简单关节
   - 颜色按 agent 类型区分

5. **动画系统**（基于当前工具）：
   - **Bash**：身体前倾，双臂快速上下（打字）
   - **Read**：头部左右轻摇（阅读）
   - **Edit/Write**：右臂前后移动（书写）
   - **WebFetch**：头部抬起，左右环顾
   - **task_start**：角色短暂站立（跳起动画）
   - **idle**：轻微上下浮动（0.05 单位，1.5 秒周期）
   - **archived**：无动画，半透明

6. **光照系统**：
   - 环境光（0xffffff, 强度 0.4）
   - 定向光（0xfff4e0, 强度 1.2）- 暖色调
   - 活跃角色头顶有点光源（颜色匹配 agent 类型）
   - 雾化效果（30-80 单位）

7. **交互**：
   - 点击角色 → 右侧滑出详情面板
   - 显示：项目名、任务描述、当前工具、目标、会话 ID
   - 轨道控制器（OrbitControls）可旋转、缩放场景

**UI 覆盖层：**
- **标题栏**（左上）："AI 探员总部" + "实时监控"
- **状态指示器**（右上）：连接状态圆点 + 文字
  - 绿色：已连接
  - 红色：已断开
  - 黄色脉动：重新连接中
- **会话详情面板**（右侧）：点击角色时滑入
- **空闲提示**（居中）：无活跃会话时显示"暂无活跃会话"

### 构建产物

**Bundle 大小：**
- JavaScript：**128.2 KB gzip**（预算 320 KB）
- CSS：0 KB（所有样式内联）
- 输出文件：
  - `static/agents.html`
  - `static/agent-assets/agents-Du9Munei.js`

### 集成到 Gateway

**修改文件：**
1. `src/local_llm_deploy/gateway/app.py`：
   - 导入 `AgentMonitor`
   - 在 `GatewayContext.__init__()` 中初始化并启动 monitor
   - 添加路由：`/agents-api/v1/snapshot` 和 `/agents-api/v1/stream`
   - 实现 WebSocket 握手和帧发送
   - 更新静态文件缓存规则：支持 `/agents.html` 和 `/agent-assets/`

2. `src/local_llm_deploy/gateway/auth.py`：
   - 允许 console session 访问 agents API 端点

## 使用方法

### 1. 启动 Gateway

```bash
cd /Users/jszhang/github/my/local-llm-deploy
python3 -m src.local_llm_deploy.gateway.app --host 0.0.0.0 --port 8888
```

### 2. 访问监控界面

在浏览器中打开：
```
http://localhost:8888/agents.html
```

或从局域网其他设备访问：
```
http://<你的机器IP>:8888/agents.html
```

### 3. 观察效果

- 当你在本地运行 Claude Code 或 Codex 时，对应楼层会出现"员工"
- 每个"员工"代表一个活跃会话
- 点击"员工"查看详细信息
- 实时观察工具调用动画：
  - 看到角色打字 → 正在执行 Bash 命令
  - 看到角色点头阅读 → 正在 Read 文件
  - 看到角色书写 → 正在 Edit/Write 文件

### 4. 重新构建前端（如需修改）

```bash
cd frontend
npm run build:agents
```

构建产物会自动发布到 `../static/`。

## 架构图

```
┌─────────────────────────────────────────────────────────────┐
│                        浏览器                                 │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  agents.html                                          │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │  │
│  │  │  scene.ts    │  │    ws.ts     │  │   ui.ts    │ │  │
│  │  │  (Three.js)  │◄─┤ (WebSocket)  │  │  (DOM)     │ │  │
│  │  │              │  │              │  │            │ │  │
│  │  │  - 楼层      │  │  - 自动重连   │  │  - 状态栏  │ │  │
│  │  │  - 角色      │  │  - 事件流     │  │  - 详情板  │ │  │
│  │  │  - 动画      │  │              │  │            │ │  │
│  │  └──────────────┘  └──────────────┘  └────────────┘ │  │
│  └───────────────────────────────────────────────────────┘  │
└────────────────────────┬────────────────────────────────────┘
                         │ WebSocket: ws://host/agents-api/v1/stream
                         │ HTTP GET: /agents-api/v1/snapshot
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Gateway (Python)                                            │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  app.py                                              │   │
│  │  - ProxyHandler._stream_agent_events()              │   │
│  │  - WebSocket 握手 + 帧发送                            │   │
│  │  - 路由: /agents-api/v1/snapshot|stream             │   │
│  └──────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  agents_api.py                                       │   │
│  │  ┌────────────────┐  ┌────────────────┐            │   │
│  │  │ ClaudeCode     │  │   Codex        │            │   │
│  │  │ Collector      │  │   Collector    │            │   │
│  │  └────────────────┘  └────────────────┘            │   │
│  │          │                   │                       │   │
│  │          └───────┬───────────┘                       │   │
│  │                  ▼                                    │   │
│  │         ┌──────────────────┐                         │   │
│  │         │  AgentMonitor    │                         │   │
│  │         │  - 后台线程       │                         │   │
│  │         │  - 订阅者队列     │                         │   │
│  │         │  - snapshot()    │                         │   │
│  │         └──────────────────┘                         │   │
│  └──────────────────────────────────────────────────────┘   │
└────────────────────────┬────────────────────────────────────┘
                         │
                         │ 每 0.5 秒读取增量
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  本地文件系统                                                 │
│  ~/.claude/projects/*/session.jsonl                         │
│  ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl              │
└─────────────────────────────────────────────────────────────┘
```

## 扩展性

### 添加新 Agent 类型（如 OpenClaw、Grok Bot）

1. **在 `agents_api.py` 中添加新 Collector**：
   ```python
   class OpenClawCollector(AgentCollector):
       def __init__(self):
           super().__init__('openclaw')
           self.openclaw_home = Path.home() / '.openclaw'
       
       def _scan_sessions(self) -> list[Path]:
           # 找到 OpenClaw 的会话文件
           pass
       
       def _extract_events(self, records, session_id):
           # 提取事件
           pass
   ```

2. **在 `AgentMonitor.__init__()` 中注册**：
   ```python
   self.collectors = [
       ClaudeCodeCollector(),
       CodexCollector(),
       OpenClawCollector(),  # 新增
   ]
   ```

3. **在前端 `scene.ts` 中添加楼层**：
   ```typescript
   const FLOORS = [
     { y: 0,  label: '档案室',     agentType: 'archive' },
     { y: 5,  label: 'Claude Code', agentType: 'claude-code' },
     { y: 10, label: 'Codex',       agentType: 'codex' },
     { y: 15, label: 'OpenClaw',    agentType: 'openclaw' },  // 新增
   ]
   
   const AGENT_COLORS = {
     'claude-code': 0xe07050,
     'codex': 0x8b5cf6,
     'openclaw': 0x10b981,  // 新增：绿色
   }
   ```

4. **重新构建**：
   ```bash
   cd frontend && npm run build:agents
   ```

### 性能优化建议

- 当会话数量超过 50 个时，考虑分页或只显示最近活跃的
- 角色动画使用 `requestAnimationFrame`，已经是最优
- WebSocket 消息已经是增量推送，不是全量快照

### 安全建议

- 生产环境建议配置 API key 认证
- LAN 访问时注意防火墙规则
- Secret 脱敏已内置，但建议定期审查日志

## 技术栈

- **后端**：Python 3.11+, threading, WebSocket (RFC 6455)
- **前端**：TypeScript, Three.js 0.169, Vite 8
- **通信**：WebSocket (JSON 流)
- **构建**：Vite, 独立 bundle (避免主 monitor 应用的 200KB 限制)

## 文件清单

**新增文件：**
- `src/local_llm_deploy/gateway/agents_api.py` (395 行)
- `frontend/agents.html`
- `frontend/vite.agents.config.ts`
- `frontend/scripts/export-agents.mjs`
- `frontend/src/agents/main.ts` (33 行)
- `frontend/src/agents/scene.ts` (511 行)
- `frontend/src/agents/ws.ts` (58 行)
- `frontend/src/agents/ui.ts` (190 行)
- `frontend/src/agents/types.ts` (38 行)

**修改文件：**
- `src/local_llm_deploy/gateway/app.py` (+97 行)
- `src/local_llm_deploy/gateway/auth.py` (+2 行)
- `frontend/package.json` (添加 scripts)

**构建产物：**
- `static/agents.html`
- `static/agent-assets/agents-Du9Munei.js` (128.2 KB gzip)
- `static/agent-manifest.json`

## 开发者备注

- Three.js 的低面卡通角色使用 6 面球体和方块组成，简化了渲染
- 动画系统基于 `clock.getDelta()` 和简单的三角函数，避免复杂的骨骼系统
- WebSocket 帧手动构造（`struct.pack`），符合 RFC 6455 规范
- 采用发布-订阅模式，后台线程持续采集，WebSocket 订阅者接收增量
- 文件位置追踪（offset）避免重复读取大文件，内存占用低

---

**完成时间**：2026-11-07  
**Bundle 大小**：128.2 KB gzip (under 320 KB budget)
**状态**：✅ 完成并验证
