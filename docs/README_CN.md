# AICU-Graph

[English](../README.md) | 简体中文

> 实时构建动态 AI Agent 组织，让多个智能体协同完成复杂的长期任务。

AICU-Graph 是一个多 Agent 编排运行时：给它一个目标，Manager 负责规划，Worker 在运行时动态组建的组织里执行，每个节点都由真实的 AI Coding CLI（Claude Code、Codex、Gemini 等）驱动。它是一个可运行的研究与工程原型，能用哪些 CLI 和模型取决于你本机的安装、账号和额度。

## 演示视频

约 3 分钟的带旁白演示：一个目标如何长成 Manager / Worker 组织。画面中的工作台界面来自一个真实跑完的 60 节点会话，讲解用的示意图为动画。

https://github.com/user-attachments/assets/64a0be86-4a83-43ab-b8c7-2d3068da5dc7

## 快速开始

下面带你从一台干净的机器走到工作台跑起来、并提交第一个任务。各 CLI 的细节、OpenRouter、凭证和故障排查见 [docs/INSTALL.md](INSTALL.md)。

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

同样支持 Qwen Code、Kimi Code、OpenCode、Pi、CodeBuddy Code、Cursor Agent、Antigravity 和 Grok CLI，安装命令见 [docs/INSTALL.md](INSTALL.md)。装完后可以查看 AICU 检测到了哪些 CLI：

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

遇到问题：提示"找不到 CLI"时，先在同一个终端确认命令可用（`which claude`）；页面空白说明前端没有构建（`cd frontend && npm install && npm run build`，然后重启）；认证失败说明 CLI 没登录，或 设置 → 连接 里的 Key 不对。更多见 [docs/INSTALL.md](INSTALL.md)。

## 文档导航

- [安装、配置与故障排查](INSTALL.md)
- [系统架构、核心概念与测试体系](ARCHITECTURE.md)

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
- 角色智能选取：候选角色对话框里的「智能推荐」目前只是占位，尚未实现。随着角色池越来越大，如何根据任务从海量角色中选出一小批能覆盖需求的角色（检索、排序、覆盖度校验、效果评估），欢迎提出思路或贡献实现；
- 事件驱动协作与长周期任务执行；
- Artifact 与共享工作区；
- CLI 适配、评估和测试。

提交代码前请先阅读现有文档；后端与前端测试套件的运行方式见 [docs/ARCHITECTURE.md §10.7](ARCHITECTURE.md#107-测试体系两套后端--一套前端)。请勿提交本地凭证及运行产物。

## License

本项目基于 Apache License 2.0 发布，详见 [LICENSE](../LICENSE)。`skills/` 下内置的 skill 来自其它开源项目，沿用各自的许可证，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
