# AICU

### Build dynamic AI agent organizations that collaborate to solve complex, long-running tasks.

**AICU (Agent Intelligent Collaboration Unit)** is a dynamic multi-agent collaboration platform that **builds and adapts AI agent organizations in real time** to solve complex, long-running, and evolving tasks.

Instead of relying on a fixed workflow or a predefined chain of agents, AICU allows an organization of specialized AI agents to be formed around the task itself. Agents can work independently, collaborate when necessary, and adapt their organization as the task evolves.

> **From single agents completing tasks to AI organizations completing tasks.**

---

## Why AICU?

Most agent systems work well when the workflow is known in advance:

```text
Task
  ↓
Planner
  ↓
Agent A
  ↓
Agent B
  ↓
Agent C
  ↓
Result
```

But many real-world tasks do not have a fixed workflow.

A complex task may require different capabilities at different stages:

```text
                    Complex Task
                         │
                         ▼
              Build an AI Organization
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
       Backend        Frontend        Designer
          │              │              │
          └──────────────┼──────────────┘
                         ▼
                      Testing
                         │
                         ▼
                  New requirements
                         │
                         ▼
                Organization adapts
```

AICU is designed for this kind of problem.

The system does not require the complete organization or workflow to be known beforehand. It can **construct the organization needed for the current task and adapt it as new requirements, dependencies, failures, and discoveries emerge.**

---

## Core Ideas

### 🏗️ Dynamic Agent Organization

AICU treats a group of agents as an **organization**, rather than simply a collection of independent agents.

Given a complex task, the system can determine:

* What capabilities are required
* Which roles are needed
* How agents should be organized
* Which agents should work independently
* Which agents need to collaborate
* How the organization should evolve as the task progresses

The organization is not necessarily fixed before execution.

---

### 🧩 Roles & Skills

Agents can be composed from reusable **Roles** and **Skills**.

A Role represents a type of responsibility:

```text
Frontend Developer
Backend Developer
Tester
Architect
Technical Writer
```

Skills represent specific capabilities:

```text
React
Django
API Integration
Database Design
Security Testing
```

This allows the system to move from:

```text
Task → Fixed Agents
```

toward:

```text
Task
  ↓
Required Capabilities
  ↓
Roles + Skills
  ↓
Dynamic Agent Organization
```

---

### 🧠 Capability-Driven Organization

AICU is designed to support a large and extensible capability universe.

Instead of forcing a manager to consider every available role and skill, relevant capabilities can first be identified and retrieved for the current task.

```text
User Task
   ↓
Capability Retrieval
   ↓
Relevant Roles / Skills
   ↓
Organization Planning
   ↓
Agent Organization
```

This allows the system to scale to much larger collections of specialized capabilities.

---

### 🔄 Event-Driven Collaboration

Agents do not need to continuously communicate with each other.

Most work can happen independently.

When something happens that actually requires collaboration — such as a blocking issue, breaking change, important discovery, failed validation, or request for assistance — it can trigger collaboration between relevant agents.

```text
Agent A
   │
   │ important event
   ▼
Collaboration
   │
   ├──► Agent B
   ├──► Agent C
   └──► Agent D
```

This makes collaboration **event-driven and demand-driven**, rather than forcing every agent to participate in every step.

---

### 🎯 Dynamic Routing

AICU separates collaboration decisions from individual agents.

When an event requires collaboration, the system determines which agents should receive or respond to it based on the current organization and task context.

```text
Event
  ↓
Routing
  ↓
Relevant Agents
  ↓
Collaboration
```

This allows the organization to remain loosely coupled while still supporting dynamic communication.

---

### 📦 Shared Work & Artifacts

Agents work toward a shared task outcome rather than exchanging their entire internal context with every other agent.

Important results are represented through task state and artifacts:

```text
Agent
  ↓
Artifact / State
  ↓
Shared Task Environment
  ↓
Other Agents
```

This allows agents to collaborate without requiring every agent to carry the entire history of the organization.

---

### ⚡ Long-Running Tasks

AICU is designed for tasks that may take many steps and involve many agents.

Examples include:

* Building complete software systems
* Large-scale refactoring
* Research projects
* Product development
* Documentation projects
* Complex data processing
* Multi-stage engineering tasks

The goal is not simply to generate a response.

The goal is to **continuously move a complex task toward completion.**

---

## AICU vs. Traditional Agent Workflows

### Traditional workflow

```text
Task
 ↓
Fixed Workflow
 ↓
Agent A
 ↓
Agent B
 ↓
Agent C
 ↓
Result
```

The organization and execution path are mostly predetermined.

### AICU

```text
                 Task
                  │
                  ▼
        Build Agent Organization
                  │
        ┌─────────┼─────────┐
        ▼         ▼         ▼
      Agent A   Agent B   Agent C
        │         │         │
        └───── collaboration ─────┐
                                   │
                                   ▼
                            Task evolves
                                   │
                                   ▼
                        Organization adapts
                                   │
                         ┌─────────┼─────────┐
                         ▼         ▼         ▼
                       Agent D   Agent E   Agent F
```

The key difference is:

> **The organization itself becomes part of the task execution process.**

---

## Architecture

At a high level:

```text
                         User Task
                            │
                            ▼
                  ┌───────────────────┐
                  │ Capability Layer  │
                  └─────────┬─────────┘
                            │
                            ▼
                  ┌───────────────────┐
                  │ Organization      │
                  │ Planning          │
                  └─────────┬─────────┘
                            │
                            ▼
                  ┌───────────────────┐
                  │ Agent Organization │
                  │      Graph        │
                  └─────────┬─────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
           Agent A        Agent B        Agent C
              │             │             │
              └─────────────┼─────────────┘
                            │
                         Events
                            │
                            ▼
                     Collaboration
                            │
                            ▼
                  Organization evolves
                            │
                            └──────────────┐
                                           │
                                           ▼
                                      Task Progress
```

AICU separates several concerns:

| Layer        | Responsibility                                      |
| ------------ | --------------------------------------------------- |
| Capability   | Discover the capabilities required by a task        |
| Organization | Build and adapt the Agent organization              |
| Agent        | Perform specialized work                            |
| Runtime      | Execute and manage agents                           |
| Event        | Represent important changes and collaboration needs |
| Routing      | Determine which agents need to collaborate          |
| State        | Track task and agent progress                       |
| Artifact     | Store concrete outputs of work                      |

---

## Example

Imagine the task:

```text
Build a complete blog platform.
```

AICU does not require a fixed workflow such as:

```text
Design → Backend → Frontend → Testing
```

Instead, it can construct an organization around the task:

```text
                 Project Manager
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
      Architect      Backend      Frontend
          │            │            │
          │            ├─────┐      │
          │            │     │      │
          │            ▼     │      ▼
          │          Database │   UI
          │                  │
          └──────────────────┼─────────┐
                             ▼         ▼
                           Testing   Security
```

During execution, new information can change what the organization needs.

For example:

```text
Backend discovers authentication complexity
                    │
                    ▼
             New collaboration
                    │
                    ▼
              Security Agent
                    │
                    ▼
             Organization adapts
```

The system therefore evolves with the task instead of simply executing a predetermined pipeline.

---

## Design Principles

AICU is built around several principles:

### 1. Organization over workflow

The system should organize capable agents around a goal rather than forcing every task into a fixed workflow.

### 2. Dynamic over static

The organization should be able to evolve as the task changes.

### 3. Collaboration over conversation

Agents collaborate to make progress. Communication itself is not the goal.

### 4. Event-driven over continuous coordination

Agents should communicate when collaboration provides value, rather than constantly exchanging messages.

### 5. Capability over identity

The system should select agents based on what they can do, rather than relying entirely on predefined agent identities.

### 6. Execution over generation

The objective is to complete real work and produce verifiable artifacts, not simply generate a final response.

---

## Project Status

AICU is currently under active development.

The project is evolving toward a general-purpose runtime for dynamic AI agent organizations.

Current development areas include:

* Dynamic Agent Organization
* Role / Skill / Capability system
* Organization Graph
* Event-driven collaboration
* Dynamic routing
* Shared workspace
* Artifact management
* Long-running task execution
* Agent state management
* Collaboration efficiency
* Evaluation and reliability

APIs and internal architecture may change during this stage.

---

## Roadmap

### Organization

* [x] Dynamic organization generation
* [x] Role-based agents
* [x] Skill-based capabilities
* [ ] Large-scale capability retrieval
* [ ] Dynamic organization adaptation

### Collaboration

* [x] Event-based collaboration
* [x] Independent routing
* [x] Agent inbox
* [ ] Event aggregation
* [ ] Cost-aware routing
* [ ] More efficient asynchronous collaboration

### Runtime

* [x] Agent scheduling
* [x] Agent state management
* [x] Shared workspace
* [x] Artifact management
* [ ] Resumable long-running execution
* [ ] Advanced observability

### Ecosystem

* [ ] More model providers
* [ ] Agent-to-Agent interoperability
* [ ] External Agent integration
* [ ] Evaluation benchmarks
* [ ] Community roles and skills

---

## Getting Started

> Documentation and installation instructions are currently being prepared.

The long-term goal is to make creating a dynamic AI organization as simple as defining the task:

```text
User
  ↓
AICU
  ↓
"Build a complete e-commerce platform."
  ↓
AICU creates the required organization
  ↓
Agents collaborate
  ↓
Artifacts are produced
  ↓
Task completed
```

---

## Why "AICU"?

**AICU — Agent Intelligent Collaboration Unit**

The name reflects the project's core idea:

> An AICU is a computational unit in which multiple specialized AI agents can be organized and coordinated to accomplish a larger goal.

The long-term vision is to move beyond individual AI assistants and toward **AI organizations capable of organizing themselves around complex objectives.**

---

## Contributing

AICU is an open-source project and contributions are welcome.

Areas that are particularly interesting include:

* Agent organization strategies
* Role and skill design
* Multi-agent collaboration
* Dynamic routing
* Agent evaluation
* Long-running agent systems
* Agent communication protocols
* Runtime reliability
* Cost-efficient agent coordination

Issues, discussions, experiments, and pull requests are welcome.

---

## License

See [LICENSE](LICENSE) for details.
