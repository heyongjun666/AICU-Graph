# AICU-Graph

English | [简体中文](docs/README_CN.md)

> Build dynamic AI agent organizations that collaborate on complex, long-running tasks.

AICU-Graph is a multi-agent orchestration runtime: give it a goal, and Managers plan the work while Workers carry it out inside an organization built at runtime, with every node driven by a real AI coding CLI (Claude Code, Codex, Gemini and more). It is a runnable research and engineering prototype; which CLIs and models you can use depends on your local installs, accounts and quotas.

## Demo

A ~3-minute narrated walkthrough of how a goal grows into a Manager / Worker organization. Screens come from a real, completed 60-node session; the explanatory diagrams are animation.

https://github.com/user-attachments/assets/ed0c9594-8b8f-49b8-9082-0755e13ab0bc

For a more detailed walkthrough, see the full-length demo video: https://youtu.be/Zw-mO_qeR20

## Quick Start

This takes you from a fresh machine to a running workbench and a first task. Per-CLI details, OpenRouter, credentials and troubleshooting are in [docs/INSTALL.md](docs/INSTALL.md) (Chinese).

### 1. Check the requirements

| Requirement | Check | Notes |
|---|---|---|
| macOS or Linux | | The terminal panel relies on `pty` / `fcntl`; Windows is not supported. |
| Python 3.10+ | `python3 --version` | CI runs on 3.13. |
| Node.js 18+ and npm | `node --version` | Needed to build the web workbench and to install most CLIs. |
| Git | `git --version` | |
| At least one AI coding CLI | see step 4 | Plus an account, API key or other credential for it. |

### 2. Get the code

```bash
git clone https://github.com/heyongjun666/AICU-Graph.git
cd AICU-Graph
```

### 3. Install dependencies and build the frontend

A virtual environment is recommended so the backend dependencies stay isolated:

```bash
python3 -m venv .venv
source .venv/bin/activate        # run this again in every new terminal
pip install -r requirements.txt

cd frontend
npm install
npm run build                    # the server serves frontend/dist, which is not committed
cd ..
```

### 4. Install at least one AI coding CLI

AICU drives real coding CLIs found on your `PATH`. Install one and finish **that CLI's own login / authentication** before continuing.

| CLI | Install | Check |
|---|---|---|
| Claude Code | `npm install -g @anthropic-ai/claude-code` | `claude --version` |
| Codex CLI | `npm install -g @openai/codex` | `codex --version` |
| Gemini CLI | `npm install -g @google/gemini-cli` | `gemini --version` |

Qwen Code, Kimi Code, OpenCode, Pi, CodeBuddy Code, Cursor Agent, Antigravity and Grok CLI are also supported; install commands are in [docs/INSTALL.md](docs/INSTALL.md). Afterwards you can list what AICU detected:

```bash
python3 -c "from cli_adapters import detect_installed; print(detect_installed())"
```

### 5. Start the Runtime

```bash
python3 main.py
```

Open [http://localhost:8080](http://localhost:8080). The server listens on `127.0.0.1:8080` by default (local machine only) and has **no authentication**. Set `HOST=0.0.0.0` only on a network you fully trust, and never expose it to an untrusted one — anyone who can reach the port can run commands through your CLI sessions. `PORT` changes the port.

### 6. Configure the model and API key

In the workbench, open **Settings → Connection**, then:

1. choose the CLI;
2. choose the provider and model;
3. enter the API key (or complete the CLI's own authentication);
4. save, and click **Test connection**.

Keys are stored locally in `runtime_sessions/api_keys.json`. You can also export a provider variable before starting, for example `export ANTHROPIC_API_KEY="..."`. Never put real keys in source files, `.env` files or commits.

### 7. Run your first task

Create a session with a task, then start it:

```bash
curl -X POST http://localhost:8080/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"task":"Build a small multi-stage feature that needs design, implementation and tests","title":"demo"}'

# replace <session_id> with the id returned above
curl -X POST http://localhost:8080/v1/sessions/<session_id>/graph/resume
```

Watch the graph, events and node logs in the workbench. Start with a small task: every Worker node is a real CLI run that consumes your model quota.

If something goes wrong: a CLI that is "not found" must work in the same terminal (`which claude`); a blank page means the frontend was not built (`cd frontend && npm install && npm run build`, then restart); authentication errors mean the CLI is not logged in or the key under Settings → Connection is wrong. More in [docs/INSTALL.md](docs/INSTALL.md).

## Documentation

The docs below are currently written in Chinese.

- [Installation, configuration and troubleshooting](docs/INSTALL.md)
- [Architecture, core concepts and test suites](docs/ARCHITECTURE.md)

## Security Notice

API keys and session runtime data are local runtime state and must not be committed to Git. Do not upload:

```text
runtime_sessions/
.env
api_keys.json
```

## Contributing

Contributions are welcome in areas including:

- Dynamic agent organizations and task planning;
- Manager / Worker Runtime;
- Roles, skills, and capability configuration;
- Smart role selection: the candidate-role dialog has a "Smart recommendation" option that is still a placeholder. As role pools grow, how do we pick a small set of roles that can cover a task from hundreds or thousands of candidates (retrieval, ranking, coverage checks, evaluation)? Ideas and implementations are welcome;
- Event-driven collaboration and long-running execution;
- Artifacts and shared workspaces;
- CLI adapters, evaluation, and testing.

Please read the existing documentation before submitting changes; how to run the backend and frontend test suites is described in [docs/ARCHITECTURE.md §10.7](docs/ARCHITECTURE.md#107-测试体系两套后端--一套前端). Never commit local credentials or runtime artifacts.

## License

Released under the Apache License 2.0. See [LICENSE](LICENSE). Bundled skills under `skills/` keep their original licenses; see [THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md).
