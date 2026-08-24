# AICU v5 安装与部署文档

> 本文档介绍如何在一台新机器上安装并启动 **AICU v5（AI Coding Universe 第五版）多 Agent 软件研发编排框架**。
> 配套文档：`docs/ARCHITECTURE.md`（架构详解）、`docs/user_guide.md`（示例博客平台用户手册）、`docs/plan_v3.1_communication.md`（通信架构迭代规划）。

---

## 1. 项目简介

AICU 是一个**多 Agent 软件研发编排框架**：

- 用户提交一个研发任务（例如"做一个博客系统"）；
- 系统自动生成一棵 **Manager / Worker 的 Agent 组织树**；
- 节点通过 **Event 事件总线**互相通信，形成「开发 → 测试反馈 → 修复 → 复验 → 交付」的协作闭环；
- 每一步真实执行由 **AI 编程 Agent**（CC / internal 等终端 CLI）完成，在隔离工作目录里真实读写文件、运行命令。

---

## 2. 环境要求

| 依赖 | 版本 | 是否必需 | 说明 |
| --- | --- | --- | --- |
| Python | 3.10+ | ✅ 必需 | 后端运行时 |
| AI 编程 CLI | — | ✅ 必需 | 至少安装一个，见 [4.3](#43-安装--确认-ai-编程-cli) |
| Node.js | 18+ | ⭕ 可选 | 仅前端工作台（React）需要 |
| codegraph | — | ⭕ 可选 | 仅开启 `AICU_CODEGRAPH=1` 时需要 |

支持的 AI 编程 CLI（优先级即默认选择顺序）：

```
internal > claude > codebuddy > codex > kimi > qwen > opencode > pi > gemini > cursor > antigravity > grok
```

- `internal`：内部 Claude Code 封装（内部环境推荐，需账号）。
- `claude`：官方 Claude Code CLI。
- 其余为第三方终端 Agent CLI，均可直连对应厂商。

---

## 3. 目录结构

```
final_solution/
├── main.py               # 后端入口（FastAPI + uvicorn，默认 0.0.0.0:8080）
├── aicu.py               # AI 执行引擎（任务→CC/internal 提示词，编排多 peer）
├── claude_code_agent.py  # internal/claude CLI 程序化封装（多轮对话/超时/续接/重试）
├── cli_adapters.py       # 多终端 CLI 适配层（协议差异抽象）
├── cli_models.json       # 各 CLI 已知模型清单（前端下拉候选）
├── requirements.txt      # Python 依赖
├── runtime/              # 核心运行时（Graph/Planner/Scheduler/Router/EventBus/Delivery…）
├── executables/          # 执行体（Manager/Worker）
├── roles/                # 18 个角色定义（Manager/Worker/system_role 的 YAML）
├── frontend/             # React 工作台（graph-viewer，可选）
├── backend/              # 示例产物：Markdown 博客平台（框架外，非必需）
├── docs/                 # 文档
└── tests/                # 测试套件
```

> 运行时会在项目根生成 `runtime_sessions/`、`workspaces/`、`nodes/`、`events*` 等动态数据目录，均已在打包时排除。

---

## 4. 快速安装

### 4.1 解压

```bash
tar xzf aicu_v5_final_solution.tar.gz
cd final_solution
```

### 4.2 安装 Python 依赖

```bash
pip install -r requirements.txt
```

依赖仅三项：`fastapi`、`uvicorn[standard]`、`pydantic`。

### 4.3 安装 / 确认 AI 编程 CLI

AICU 通过 `shutil.which` 探测已安装的 CLI。至少安装其一：

**内部环境（推荐 internal）：**

```bash
# 确认 internal 可用
which internal
internal --version
```

**通用环境（官方 Claude Code）：**

```bash
which claude
```

若都没有，请按 `cli_adapters.py` 支持的 CLI 之一安装（codex / kimi / qwen / …），并确保其可执行文件在 `PATH` 中。

### 4.4 （可选）构建前端工作台

前端工作台提供图形化界面（图编辑、节点查看、会话管理、凭证配置）。不构建也不影响后端 API 运行。

```bash
cd frontend
npm install
npm run build     # 产物输出到 frontend/dist
cd ..
```

开发模式：`cd frontend && npm run dev`。

---

## 5. 配置

### 5.1 模型与 CLI 选择

- 各 CLI 支持的模型清单集中在 `cli_models.json`（`internal` 键为内部网关 display name，`all` 键为全厂商官方名）。
- 前端工作台「设置」页可切换 CLI 与模型；`internal` 默认返回网关全厂商模型。

### 5.2 凭证

凭证存储于项目根 `api_keys.json`（明文，0600 权限，首次配置时自动创建），两类：

| 场景 | 需要 | 配置方式 |
| --- | --- | --- |
| 使用 internal | 账号名（username） | `PUT /v1/config/internal-username` 或前端设置页 |
| 使用第三方 CLI（非 internal） | 厂商 API Key（按 vendor） | `PUT /v1/config/api-key` 或前端设置页 |

vendor 映射：`anthropic / openai / moonshot / xai / google / deepseek / zhipu / minimax / aliyun`。

```bash
# 示例：配置 internal 账号
curl -X PUT http://localhost:8080/v1/config/internal-username \
  -H "Content-Type: application/json" \
  -d '{"username": "v_xxx"}'

# 示例：配置 anthropic 厂商 API Key
curl -X PUT http://localhost:8080/v1/config/api-key \
  -H "Content-Type: application/json" \
  -d '{"vendor": "anthropic", "api_key": "sk-..."}'
```

> 内部模型（internal 网关 `auto` / `*-内部`）无需 sk。第三方 CLI 可通过 `POST /v1/config/test-connection` 测试连接。

### 5.3 环境变量

| 环境变量 | 作用 | 默认 |
| --- | --- | --- |
| `EXEC_MODE` | `stub`（离线假 AI）/ `real`（真实 AI） | `real`（main.py 强制） |
| `HOST` / `PORT` | HTTP 监听地址 / 端口 | `0.0.0.0` / `8080` |
| `AICU_DELIVERY` | 交付保障总开关 | 空（关） |
| `AICU_CODEGRAPH` | CodeGraph 上下文注入 | 空（关） |
| `CODEGRAPH_BIN` | codegraph CLI 路径 | 默认探测 |
| `AICU_CODEGRAPH_BUDGET` | 上下文预算 | 4096 |
| `AICU_NODE_LOG_STREAMING` | CC 流式日志全量写 node.log | 关 |

---

## 6. 启动与验证

### 6.1 启动后端

```bash
python main.py
```

启动成功标志：

```
INFO:  Uvicorn running on http://0.0.0.0:8080
```

> 生产/后台启动建议用 `setsid`（避免 nohup 的 SIGINT 被忽略问题）：
>
> ```bash
> setsid python main.py > /tmp/aicu_backend.log 2>&1 &
> ```

### 6.2 健康检查

```bash
curl http://localhost:8080/v1/sessions
curl http://localhost:8080/v1/roles
```

### 6.3 创建会话并提交任务

```bash
# 1. 创建会话（提交研发任务）
curl -X POST http://localhost:8080/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"task": "做一个博客系统", "title": "demo"}'

# 返回 {"id": "<session_id>", ...}

# 2. 启动该会话的调度循环（开始执行）
curl -X POST http://localhost:8080/v1/sessions/<session_id>/graph/resume
```

更多端点：`GET /v1/sessions/{id}/graph`（查看图）、`GET /v1/sessions/{id}/nodes/{node_id}/logs`（节点日志）、`GET /v1/sessions/{id}/graph/events`（事件流）。

### 6.4 前端工作台（可选）

构建完成后访问 `http://localhost:8080`（后端托管 `frontend/dist` 静态文件）；或开发模式 `npm run dev` 后访问对应端口。

---

## 7. 常见问题

**Q1：启动报「未找到可用的 CLI 工具」？**
AICU 未探测到任何已安装的 AI 编程 CLI。请安装 internal / claude 之一并确保在 `PATH` 中，再重启。

**Q2：internal 报多账号需指定 username？**
配置 internal 账号：`PUT /v1/config/internal-username`。

**Q3：第三方 CLI 调用报认证失败？**
在 `PUT /v1/config/api-key` 中按 vendor 配置对应 API Key，并用 `POST /v1/config/test-connection` 验证。

**Q4：只想离线跑通流程、不接真实 AI？**
设置 `EXEC_MODE=stub`（确定性 stub 假 AI），用于测试/验收流程。

**Q5：前端访问不到页面？**
未执行 `npm run build` 时后端无 `frontend/dist` 静态产物；请先构建，或直接使用 REST API。

**Q6：数据/会话存在哪里？**
会话持久化在 `runtime_sessions/`（`sessions.json` + 每个 session 的 `graph.json`），节点产物在 `nodes/`、工作区在 `workspaces/`。
