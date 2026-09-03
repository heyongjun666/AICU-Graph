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
backend/                            Independent Flask backend module
```

The AICU Runtime is the core backend and is started by `main.py`; `frontend/` is its web workbench. The repository's `backend/` directory is an independent backend module outside the Runtime startup path and has its own dependencies and entry point.

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

## AICU Internal Work Modes

Each AICU node can configure `num_ccs` to select an internal peer collaboration strategy. These are **execution strategies inside one AICU node**. They are not four node types and are not four Runtime states.

| Mode | Configuration | Collaboration pattern | Typical use |
| --- | ---: | --- | --- |
| **Solo** | `1` | One agent generates, self-reviews, and revises when needed | Simple, low-risk, mechanical tasks |
| **Pair** | `2` | One agent produces, another reviews, and the producer revises | General development; default quality/cost balance |
| **Triple** | `3` | Two agents produce independently; a third compares, selects, and merges | Requirements, architecture, important decisions |
| **Full** | `4` | Two proposals, merge review, independent deep audit, and repair iterations | Core features, high-risk work, release audits |

## Current Implementation

The repository currently includes:

- Manager / Worker executables and `ExecutionPlan` validation;
- Multi-session Runtime, task graphs, scheduling, and state persistence;
- Event Bus, SSE event streams, and node logs;
- Shared workspaces, artifact management, and delivery checks;
- Multiple AI coding CLI adapters and model / provider configuration;
- A React workbench;
- Real CLI execution and a substantial Python / frontend test suite.

AICU-Graph is currently a runnable research and engineering prototype. The availability of a specific CLI, model, or external service depends on local installation, account permissions, API keys, and provider quotas.

## Quick Start

### Requirements

- Python 3.10+;
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

After startup, open the workbench or use the REST API to create sessions and submit tasks. For CLI installation, credential configuration, API details, and troubleshooting, see [docs/INSTALL.md](docs/INSTALL.md).

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
- Artifacts, shared workspaces, and delivery guarantees;
- CLI adapters, evaluation, and testing.

Please read the existing documentation and tests before submitting changes, and never commit local credentials or runtime artifacts.

## License

There is currently no LICENSE file at the repository root. Before publishing a formal release, add the appropriate license file and update this section.
