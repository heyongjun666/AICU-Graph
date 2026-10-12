# AICU-Graph

[English](README.md) | 简体中文

> 实时构建动态 AI Agent 组织，让多个智能体协同完成复杂的长期任务。

AICU-Graph 是一个面向复杂任务的多 Agent 编排运行时。它根据任务目标创建由多个 AICU 节点组成的执行组织，由 Manager 负责拆解和协调任务，由 Worker 负责执行具体工作，并通过事件、共享工作区和产物完成协作。

AICU-Graph 关注的不是一次问答，而是如何让一个 AI Agent 组织持续推进真实的、多阶段、会变化的任务。

## 演示视频

约 3 分钟的带旁白演示：一个目标如何长成 Manager / Worker 组织，节点如何通过事件和共享工作区协作，以及项目接下来的探索方向。画面中的工作台界面来自一个真实跑完的 60 节点会话，讲解用的示意图为动画。

https://github.com/user-attachments/assets/64a0be86-4a83-43ab-b8c7-2d3068da5dc7

## 项目定位

AICU-Graph 是**组织与运行时层**，负责：

- 创建和持久化任务会话；
- 根据任务生成 Manager / Worker 执行图；
- 调度节点、处理依赖和状态变化；
- 通过事件和共享产物支持节点协作；
- 暴露 REST API、SSE 事件流和前端工作台。

仓库中的主要代码边界如下：

```text
runtime/ + executables/ + roles/   AICU Runtime 后端
frontend/                           React/Vite Web 工作台
```

AICU Runtime 是项目的核心后端，由 `main.py` 启动；`frontend/` 是其 Web 工作台。

AICU 是图中的单个执行单元。一个 AICU 节点在创建时绑定角色配置、任务配置和执行参数，然后由对应的 AI Coding CLI 完成工作。

```text
用户任务
   |
   v
AICU-Graph Runtime
   |
   +-- Manager：分析任务、生成执行计划、协调复验
   |
   +-- Worker：执行具体研发任务
   |       |
   |       +-- Role / Skills / CLI / Model
   |
   +-- Event Bus：传递重要状态和协作事件
   |
   +-- Artifacts / Shared Workspace：共享中间结果和最终产物
```

## 为什么需要 AICU-Graph？

固定工作流适合执行路径明确的任务：

```text
任务 -> Agent A -> Agent B -> Agent C -> 结果
```

复杂的长期任务通常会在执行过程中出现新的需求、依赖、失败和专业能力要求。AICU-Graph 将任务组织作为运行时的一部分：先创建执行组织，再根据状态推进任务；当需要复验、修复或新的专业能力时，通过 Manager、Worker 和事件机制继续扩展或调整执行过程。

```text
任务目标
   |
   v
Manager 生成执行计划
   |
   +--> Worker：架构设计
   +--> Worker：后端实现
   +--> Worker：前端实现
   +--> Worker：测试与验证
              |
              v
       共享产物 / 事件协作
              |
              v
         复验、修复、交付
```

## 核心概念

### AICU-Graph

由多个 AICU 节点组成的任务执行组织。它拥有会话、执行图、调度器、事件总线、持久化和产物管理能力。

### AICU 节点

一个实际执行任务的工作单元。节点包含任务、角色、执行类型、配置和状态等信息。节点的身份和执行配置在创建时确定；如果任务需要新的专业能力，系统可以创建新的节点，而不是让已有节点在执行中随意改变身份。

### Manager / Worker

- **Manager**：负责理解任务、生成 `ExecutionPlan`、提交子任务，并在子任务完成后进行复验和必要的修复迭代。
- **Worker**：负责执行计划中的具体工作，并产生代码、文档、测试报告等产物。

### Role / Skills

Role 和 Skills 是节点执行时使用的能力配置。它们保存在 `roles/` 和相关技能目录中，与 Manager / Worker 的执行框架分离，可以被不同节点复用。

### Event / Artifact / Shared Workspace

- **Event**：表示状态变化、阻塞、依赖变化或需要协作的重要事件。
- **Artifact**：节点产生的可交付结果，例如代码、报告和测试输出。
- **Shared Workspace**：多个相关节点可访问的共享工作目录，用于传递实际工作结果，而不是反复复制完整对话历史。

## AICU 的执行方式

每个 AICU 节点只用一个 Agent 一次调用产出结果（Solo），节点内部没有二次审查、修订、融合或审计，以控制 token 消耗。质量把关放在节点之外：Manager 对 Worker 的产出做审查，审查不通过时由 Manager 生成修复计划，用户也可以对节点发起 Revise。

## 当前实现

当前仓库已经包含：

- Manager / Worker 执行体和 ExecutionPlan 校验；
- 多会话 Runtime、任务图、调度器和状态持久化；
- Event Bus、SSE 事件流和节点日志；
- 共享工作区与 Artifact 管理；
- 多种 AI Coding CLI 适配器及模型 / Provider 配置；
- React 前端工作台；
- 真实 CLI 执行和较完整的 Python / 前端测试套件。

AICU-Graph 当前是一个可运行的研究与工程原型。部分 CLI、模型和外部服务的可用性取决于本机安装、账号权限、API Key 和厂商配额。

## 快速开始

下面带你从一台干净的机器走到工作台跑起来、并提交第一个任务。完整说明（全部 CLI、OpenRouter、凭证、故障排查）见 [docs/INSTALL.md](docs/INSTALL.md)。

### 1. 检查环境

| 要求 | 检查命令 | 说明 |
|---|---|---|
| macOS 或 Linux | | 终端面板依赖 `pty` / `fcntl`，不支持 Windows。 |
| Python 3.10+ | `python3 --version` | CI 使用 3.13。 |
| Node.js 18+ 与 npm | `node --version` | 用于构建前端工作台，也用于安装大多数 CLI。 |
| Git | `git --version` | |
| 至少一个 AI Coding CLI | 见第 4 步 | 以及该 CLI 对应的账号、API Key 或其他认证信息。 |

### 2. 获取代码

```bash
git clone https://github.com/heyongjun666/AICU-Graph.git
cd AICU-Graph
```

### 3. 安装依赖并构建前端

建议使用虚拟环境，避免污染系统 Python：

```bash
python3 -m venv .venv
source .venv/bin/activate        # 每次新开终端都需要重新执行
pip install -r requirements.txt

cd frontend
npm install
npm run build                    # 服务直接提供 frontend/dist，该目录不在仓库中，必须自行构建
cd ..
```

### 4. 安装至少一个 AI Coding CLI

AICU 驱动的是系统 `PATH` 里真实的 Coding CLI。安装一个，并先完成**该 CLI 自己的登录或认证**，再继续。

| CLI | 安装 | 检查 |
|---|---|---|
| Claude Code | `npm install -g @anthropic-ai/claude-code` | `claude --version` |
| Codex CLI | `npm install -g @openai/codex` | `codex --version` |
| Gemini CLI | `npm install -g @google/gemini-cli` | `gemini --version` |
| Qwen Code | `npm install -g @qwen-code/qwen-code` | `qwen --version` |
| Kimi Code | `npm install -g @moonshot-ai/kimi-code` | `kimi --version` |
| OpenCode | `npm install -g opencode-ai` | `opencode --version` |
| Pi | `npm install -g @earendil-works/pi-coding-agent` | `pi --version` |
| CodeBuddy Code | `npm install -g @tencent-ai/codebuddy-code` | `codebuddy --version` |
| Cursor Agent、Antigravity、Grok CLI | 按各自官方文档安装 | `agent --version`、`agy --version`、`grok --version` |

如果角色用到 MCP，Pi 还需要多装一个扩展，见 [docs/INSTALL.md](docs/INSTALL.md)。装完后可以查看 AICU 检测到了哪些 CLI：

```bash
python3 -c "from cli_adapters import detect_installed; print(detect_installed())"
```

### 5. 启动 Runtime

```bash
python3 main.py
```

打开 [http://localhost:8080](http://localhost:8080)。服务默认只监听本机 `127.0.0.1:8080`，且**没有认证**。仅在完全可信的网络里才设置 `HOST=0.0.0.0`，不要暴露到不可信网络——能访问该端口的人可以通过你的 CLI 会话执行命令。端口用 `PORT` 修改。

### 6. 配置模型和 API Key

在工作台打开 **设置 → 连接**（Settings → Connection），依次：

1. 选择 CLI；
2. 选择服务商和模型；
3. 填写 API Key（或完成该 CLI 自己的认证）；
4. 保存，并点击 **测试连接**。

Key 保存在本机的 `runtime_sessions/api_keys.json`。也可以在启动前导出服务商环境变量，例如 `export ANTHROPIC_API_KEY="..."`。不要把真实 Key 写进源码、`.env` 文件或提交记录。

### 7. 跑第一个任务

带任务创建会话，再启动它：

```bash
curl -X POST http://localhost:8080/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"task":"实现一个需要设计、开发和测试多阶段协作的小功能","title":"demo"}'

# 把 <session_id> 换成上一步返回的 ID
curl -X POST http://localhost:8080/v1/sessions/<session_id>/graph/resume
```

在工作台里可以实时看到任务图、事件和节点日志。建议先用小任务试：每个 Worker 节点都是一次真实的 CLI 运行，会消耗你的模型额度。

### 常见问题

| 现象 | 处理 |
|---|---|
| 找不到 CLI | 在同一个终端确认命令可用（`which claude` 等）。`npm install -g` 之后请重开终端，并检查 npm 全局 bin 目录是否在 `PATH` 中。 |
| 认证失败 | 用 CLI 自己完成登录，或在 设置 → 连接 里保存正确的服务商 Key。 |
| 页面空白或没有界面 | 重新构建：`cd frontend && npm install && npm run build`，然后重启 `python3 main.py`。 |
| 找不到 `pip` / `python` | 使用 `python3` 与 `python3 -m pip`，或先激活虚拟环境。 |

## 测试

仓库里有三套互不替代的测试：`python3 run_tests.py`（核心原语自带 runner，仅标准库）、`python3 -m pytest tests`（完整后端套件）、`cd frontend && npm test -- --run`（前端）。运行前先安装测试依赖：

```bash
pip install -r requirements-dev.txt
cd frontend && npm ci
```

`pytest.ini` 把收集范围钉在 `tests/` 与 `runtime/`，避免从仓库根运行时扫进 `runtime_sessions/`。各套件覆盖范围见 [docs/ARCHITECTURE.md §10.7](docs/ARCHITECTURE.md#107-测试体系两套后端--一套前端)。

## 文档导航

- [安装、配置与启动](docs/INSTALL.md)
- [系统架构](docs/ARCHITECTURE.md)

README 只介绍项目定位和主要使用入口。API 细节、运行时目录、凭证管理、CLI 适配和故障排查以 `docs/` 中的文档为准。

## 安全提示

API Key 和会话运行数据只保存在本机运行目录，不应提交到 Git 仓库。请勿上传以下内容：

```text
runtime_sessions/
.env
api_keys.json
```

## 参与贡献

欢迎围绕以下方向参与建设：

- 动态 Agent 组织与任务规划；
- Manager / Worker Runtime；
- Role、Skills 和能力配置；
- 事件驱动协作与长周期任务执行；
- Artifact 与共享工作区；
- CLI 适配、评估和测试。

提交代码前请先阅读现有文档和测试，并确保不提交本地凭证及运行产物。

## License

本项目基于 Apache License 2.0 发布，详见 [LICENSE](LICENSE)。`skills/` 下内置的 skill 来自其它开源项目，沿用各自的许可证，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
