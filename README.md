# AICU-Graph

English | [简体中文](README_CN.md)

> Build dynamic AI agent organizations that collaborate on complex, long-running tasks.

AICU-Graph is a multi-agent orchestration runtime for complex tasks. It creates an execution organization around a task, uses Managers to decompose and coordinate work, uses Workers to execute concrete tasks, and supports collaboration through events, shared workspaces, and artifacts.

AICU-Graph is designed for more than one-shot question answering. It is designed to keep a real, multi-stage task moving as requirements, dependencies, and execution results change.

## Demo

A ~3-minute narrated walkthrough: how a single goal grows into a Manager / Worker organization, how nodes coordinate through events and a shared workspace, and where the project is heading. The screens come from a real, completed 60-node session; the explanatory diagrams are animation.

https://github.com/user-attachments/assets/ed0c9594-8b8f-49b8-9082-0755e13ab0bc

## Project Scope

AICU-Graph is the **organization and runtime layer**. It is responsible for:

- Creating and persisting task sessions;
- Building Manager / Worker execution graphs;
- Scheduling nodes and handling dependencies and state changes;
- Supporting collaboration through events and shared artifacts;
- Providing REST APIs, SSE event streams, and a React workbench.

The repository has three clear code boundaries:

```text
runtime/ + executables/ + roles/   AICU Runtime backend
frontend/                           React/Vite web workbench
```

The AICU Runtime is the core backend and is started by `main.py`; `frontend/` is its web workbench.

An AICU is one execution unit in the graph. When an AICU node is created, it receives a role configuration, task configuration, and execution parameters, then performs its work through a configured AI coding CLI.

```text
User task
   |
   v
AICU-Graph Runtime
   |
   +-- Manager: understand the task, create a plan, coordinate verification
   |
   +-- Worker: execute concrete tasks
   |       |
   |       +-- Role / Skills / CLI / Model
   |
   +-- Event Bus: publish important state and collaboration events
   |
   +-- Artifacts / Shared Workspace: share intermediate and final results
```

## Why AICU-Graph?

Fixed workflows work well when the execution path is known in advance:

```text
Task -> Agent A -> Agent B -> Agent C -> Result
```

Complex long-running tasks often introduce new requirements, dependencies, failures, and capability needs while they are running. AICU-Graph treats task organization as part of runtime execution: it creates an execution organization, advances it according to the current state, and uses Managers, Workers, and events to continue verification, repair, or collaboration when needed.

```text
Task objective
   |
   v
Manager creates an execution plan
   |
   +--> Worker: architecture
   +--> Worker: backend implementation
   +--> Worker: frontend implementation
   +--> Worker: testing and verification
              |
              v
       Shared artifacts / events
              |
              v
          Verification, repair, delivery
```

## Core Concepts

### AICU-Graph

A task execution organization made up of multiple AICU nodes. It provides sessions, execution graphs, scheduling, an event bus, persistence, and artifact management.

### AICU Node

A concrete work unit that executes a task. A node contains task data, role, execution type, configuration, and state. Its identity and execution configuration are established when it is created. If new expertise is required, the graph can create another node instead of arbitrarily changing the identity of a running node.

### Manager / Worker

- **Manager**: understands a task, creates an `ExecutionPlan`, submits child tasks, and verifies or iterates on the result.
- **Worker**: performs a concrete task from the plan and produces artifacts such as code, documents, and test reports.

### Role / Skills

Roles and skills are reusable capability configurations used by nodes during execution. They are kept separately from the Manager / Worker execution framework and can be reused by different nodes.

### Event / Artifact / Shared Workspace

- **Event**: an important state change, blocking issue, dependency change, or collaboration request.
- **Artifact**: a concrete result produced by a node, such as code, a report, or test output.
- **Shared Workspace**: a shared working directory used by related nodes to exchange actual work results instead of repeatedly copying full conversation histories.

## How an AICU Node Executes

Each AICU node uses a single agent and a single call to produce its result (Solo). There is no in-node review, revision, merge, or audit, which keeps token cost predictable. Quality control lives outside the node: the Manager reviews Worker output, a failed review leads to a repair plan from the Manager, and users can trigger Revise on any node.

## Current Implementation

The repository currently includes:

- Manager / Worker executables and `ExecutionPlan` validation;
- Multi-session Runtime, task graphs, scheduling, and state persistence;
- Event Bus, SSE event streams, and node logs;
- Shared workspaces and artifact management;
- Multiple AI coding CLI adapters and model / provider configuration;
- A React workbench;
- Real CLI execution and a substantial Python / frontend test suite.

AICU-Graph is currently a runnable research and engineering prototype. The availability of a specific CLI, model, or external service depends on local installation, account permissions, API keys, and provider quotas.

## Quick Start

This gets you from a fresh machine to a running workbench and a first task. For the full reference (every CLI, OpenRouter, credentials, troubleshooting) see [docs/INSTALL.md](docs/INSTALL.md).

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
| Qwen Code | `npm install -g @qwen-code/qwen-code` | `qwen --version` |
| Kimi Code | `npm install -g @moonshot-ai/kimi-code` | `kimi --version` |
| OpenCode | `npm install -g opencode-ai` | `opencode --version` |
| Pi | `npm install -g @earendil-works/pi-coding-agent` | `pi --version` |
| CodeBuddy Code | `npm install -g @tencent-ai/codebuddy-code` | `codebuddy --version` |
| Cursor Agent, Antigravity, Grok CLI | follow each vendor's official instructions | `agent --version`, `agy --version`, `grok --version` |

Pi needs one extra extension if your roles use MCP servers; see [docs/INSTALL.md](docs/INSTALL.md). Afterwards you can list what AICU detected:

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

### Common problems

| Symptom | Fix |
|---|---|
| CLI not found | Confirm the command works in the same terminal (`which claude`, ...). After `npm install -g`, open a new terminal and check that the npm global bin directory is on `PATH`. |
| Authentication failed | Log in with the CLI itself, or save the right provider key under Settings → Connection. |
| Blank page or missing UI | Rebuild with `cd frontend && npm install && npm run build`, then restart `python3 main.py`. |
| `pip` / `python` not found | Use `python3` and `python3 -m pip`, or activate the virtual environment. |

## Testing

There are three suites, and none of them subsumes another — a green `run_tests.py` says nothing about the pytest suite, and vice versa.

| Suite | Command | Cases |
|---|---|---|
| Core primitives (self-contained runner, stdlib only) | `python3 run_tests.py` | 137 |
| Full pytest suite | `python3 -m pytest tests` | 2100+ |
| Frontend (vitest + Testing Library) | `cd frontend && npm test -- --run` | 300+ |

Install the test dependencies first:

```bash
pip install -r requirements-dev.txt
cd frontend && npm ci
```

`pytest.ini` deliberately pins discovery to `tests/` and `runtime/`. Without it, a bare `pytest` from the repository root wanders into `runtime_sessions/` and collects tests belonging to whatever project AICU was last run against, so "green locally" and "green in CI" would not be the same set. See [docs/ARCHITECTURE.md §10.7](docs/ARCHITECTURE.md#107-测试体系两套后端--一套前端) for what each suite covers and how to run a single file.

CI (`.github/workflows/ci.yml`) runs exactly these three commands as three independent jobs on every push and pull request.

## Documentation

- [Installation, configuration, and startup](docs/INSTALL.md)
- [Architecture](docs/ARCHITECTURE.md)

This README is the project overview and entry point. API details, runtime directories, credential management, CLI adapters, and troubleshooting belong in `docs/`.

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
- Event-driven collaboration and long-running execution;
- Artifacts and shared workspaces;
- CLI adapters, evaluation, and testing.

Please read the existing documentation and tests before submitting changes, and never commit local credentials or runtime artifacts.

## License

Released under the Apache License 2.0. See [LICENSE](LICENSE). Bundled skills under `skills/` keep their original licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
