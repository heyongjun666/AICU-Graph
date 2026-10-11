# AICU-Graph

English | [简体中文](README_CN.md)

> Build dynamic AI agent organizations that collaborate on complex, long-running tasks.

AICU-Graph is a multi-agent orchestration runtime for complex tasks. It creates an execution organization around a task, uses Managers to decompose and coordinate work, uses Workers to execute concrete tasks, and supports collaboration through events, shared workspaces, and artifacts.

AICU-Graph is designed for more than one-shot question answering. It is designed to keep a real, multi-stage task moving as requirements, dependencies, and execution results change.

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

### Requirements

- macOS or Linux (the terminal panel relies on `pty` / `fcntl`; Windows is not supported);
- Python 3.10+ (CI runs on 3.13);
- At least one available AI coding CLI;
- Node.js 18+ for building the frontend.

### Install dependencies

```bash
pip install -r requirements.txt

cd frontend
npm install
npm run build
cd ..
```

### Start the Runtime

```bash
python main.py
```

The default server address is:

```text
http://localhost:8080
```

The server listens on `127.0.0.1:8080` by default (local machine only) and has **no authentication**. Set `HOST=0.0.0.0` only on a network you fully trust, and never expose it to an untrusted one — anyone who can reach the port can run commands through your CLI sessions. `PORT` changes the port.

After startup, open the workbench or use the REST API to create sessions and submit tasks. For CLI installation, credential configuration, API details, and troubleshooting, see [docs/INSTALL.md](docs/INSTALL.md).

## Testing

There are three suites, and none of them subsumes another — a green `run_tests.py` says nothing about the pytest suite, and vice versa.

| Suite | Command | Cases |
|---|---|---|
| Core primitives (self-contained runner, stdlib only) | `python3 run_tests.py` | 144 |
| Full pytest suite | `python3 -m pytest tests` | 2300+ |
| Frontend (vitest + Testing Library) | `cd frontend && npm test -- --run` | 430+ |

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
