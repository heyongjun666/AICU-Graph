# 安装与使用

本文档面向第一次使用 AICU-Graph 的用户。

## 1. 环境要求

- Python 3.10 或更高版本
- Node.js 18 或更高版本
- Git
- 至少一个受支持的 AI Coding CLI
- 该 CLI 对应的账号、API Key 或其他认证信息

Qwen Code、Kimi Code 和 Pi 可能要求更高版本的 Node.js，请以对应 CLI 的官方要求为准。

## 2. 获取项目

安装 Git 后，在终端执行：

```bash
git clone https://github.com/heyongjun666/AICU-Graph.git
cd AICU-Graph
```

## 3. 安装依赖

安装 Python 依赖：

```bash
python3 -m pip install -r requirements.txt
```

安装并构建前端：

```bash
cd frontend
npm install
npm run build
cd ..
```

## 4. 安装 AI Coding CLI

AICU 会检测系统 `PATH` 中的 CLI。至少安装一个，并先完成该 CLI 自己的登录或认证。

### Claude Code

```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

### Codex CLI

```bash
npm install -g @openai/codex
codex --version
```

### Qwen Code

```bash
npm install -g @qwen-code/qwen-code
qwen --version
```

### Kimi Code CLI

```bash
npm install -g @moonshot-ai/kimi-code
kimi --version
```

也可以按照 Kimi 官方文档使用安装脚本安装。

### Gemini CLI

```bash
npm install -g @google/gemini-cli
gemini --version
```

### OpenCode

```bash
npm install -g opencode-ai
opencode --version
```

### Pi

```bash
npm install -g @earendil-works/pi-coding-agent
pi --version
```

### CodeBuddy Code

```bash
npm install -g @tencent-ai/codebuddy-code
codebuddy --version
```

### Cursor Agent

请按照 Cursor 官方文档安装 CLI。安装完成后确认命令可用：

```bash
agent --version
```

### Antigravity

请按照 Antigravity 官方文档安装 CLI。该适配器使用 `agy` 命令：

```bash
agy --version
```

### Grok CLI

请按照 Grok 官方文档安装 CLI。安装完成后确认命令可用：

```bash
grok --version
```

安装后可以查看 AICU 检测到的 CLI：

```bash
python3 -c "from cli_adapters import detect_installed; print(detect_installed())"
```

## 5. 启动项目

在项目根目录执行：

```bash
python3 main.py
```

然后打开：

```text
http://localhost:8080
```

## 6. 配置模型和 API Key

在前端工作台的设置页完成以下操作：

1. 选择 CLI；
2. 选择服务商和模型；
3. 填写 API Key 或完成对应认证；
4. 保存配置；
5. 点击连接测试。

API Key 会保存在本机的 `runtime_sessions/api_keys.json`，不要把它提交到 Git 仓库。

也可以在启动前设置服务商环境变量，例如：

```bash
export ANTHROPIC_API_KEY="你的 API Key"
```

不要把真实 API Key 写入源码、README、`.env` 文件或 Git 提交中。

## 8. 基本验证

服务启动后，检查 API 是否正常响应：

```bash
curl http://localhost:8080/v1/sessions
curl http://localhost:8080/v1/roles
```

创建并启动一个会话：

```bash
curl -X POST http://localhost:8080/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"task":"实现一个需要多阶段协作的软件功能","title":"demo"}'
```

将返回结果中的 `<session_id>` 替换为实际 ID：

```bash
curl -X POST http://localhost:8080/v1/sessions/<session_id>/graph/resume
```

## 9. 常见问题

### 找不到 CLI

确认 CLI 已安装，并且命令位于系统 `PATH`。例如：

```bash
which claude
which codex
which qwen
which kimi
which gemini
which opencode
which pi
which codebuddy
which agent
which agy
which grok
```

如果使用 npm 全局安装后仍找不到命令，请重新打开终端，并检查 npm 全局 bin 目录是否在 `PATH` 中。

### 认证失败

确认 CLI 已完成登录，或在 AICU 设置页保存了正确的服务商 API Key。不同 CLI 的认证方式可能不同。

### 前端页面无法访问

在项目根目录重新构建前端，然后重启服务：

```bash
cd frontend
npm install
npm run build
cd ..
python3 main.py
```
