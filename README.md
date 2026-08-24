# AICU

[English](README.md) | [简体中文](README_CN.md)

# Build dynamic AI agent organizations that collaborate to solve complex, long-running tasks.

AICU (Agent Intelligent Collaboration Unit) is a dynamic multi-agent collaboration platform that enables AI agents to form, coordinate, and adapt organizations in real time to solve complex and evolving tasks.

Unlike traditional agent frameworks that rely on predefined workflows or fixed agent teams, AICU allows AI organizations to be created around the task itself. Different specialized agents can be organized when needed, collaborate during execution, and adapt as the task changes.

> From individual AI agents completing tasks to AI organizations completing tasks.

---

## Why AICU?

Most existing agent systems work well when the workflow is already known:

```
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

However, many real-world problems do not have a predefined solution path.

Large engineering projects, research tasks, and complex automation problems often require:

* Different expertise at different stages
* New agents when unexpected problems appear
* Dynamic collaboration between specialists
* Continuous adaptation during execution

AICU is designed for these scenarios.

Instead of defining a fixed team beforehand, AICU builds an AI agent organization around the current objective and allows that organization to evolve as the task progresses.

<!-- TODO: Add hero image / demo GIF here

Suggested content:
- User provides a complex task
- AICU creates an agent organization
- Multiple agents collaborate
- Task is completed

Recommended:
- GIF or short video
- 800x450 or similar aspect ratio

-->

---

# What is AICU?

AICU is a runtime for building and operating dynamic AI agent organizations.

The core idea is:

```
Task
 ↓
Understand required capabilities
 ↓
Build an agent organization
 ↓
Agents execute specialized work
 ↓
Agents collaborate when needed
 ↓
Organization adapts
 ↓
Task completion
```

AICU focuses on three major questions:

### 1. What capabilities are needed?

A complex task may require many different abilities.

AICU identifies required capabilities and organizes suitable agents around them.

### 2. How should agents collaborate?

Different agents may need different collaboration patterns depending on the task.

AICU enables dynamic collaboration instead of forcing every task into a fixed workflow.

### 3. How should the organization evolve?

Long-running tasks often change during execution.

New requirements, failures, discoveries, and dependencies may require the organization to adapt.

---

# Core Features

## 🏗️ Dynamic Agent Organization

AICU creates agent teams dynamically based on the task.

Instead of:

```
One task → predefined agents
```

AICU enables:

```
Task
 ↓
Required capabilities
 ↓
Agent organization
 ↓
Execution
```

The organization itself becomes part of the problem-solving process.

---

## 🧩 Capability-Driven Agents

AICU separates capabilities from individual agents.

Agents can be defined through reusable:

* Roles
* Skills
* Capabilities

Example:

```
Role:
- Backend Developer

Skills:
- Python
- Django
- Database Design
- API Development
```

This allows AICU to select and organize agents based on what they can do, rather than only their predefined identity.

---

## 🔄 Event-Driven Collaboration

Agents do not need to constantly communicate.

Instead, collaboration happens when meaningful events occur.

Examples:

* A dependency changes
* A blocking issue appears
* A review is required
* A critical problem is discovered

This enables efficient collaboration while reducing unnecessary communication.

---

## 🎯 Dynamic Coordination

AICU separates task execution from coordination.

Agents focus on completing their specialized work.

The system coordinates collaboration based on the current task state and organization.

---

## 📦 Shared Workspace and Artifacts

Agents collaborate through shared task outcomes.

Important results are represented as artifacts and shared state instead of repeatedly transferring large conversation histories.

This enables more scalable long-running execution.

---

## ⚡ Long-Running Task Execution

AICU is designed for tasks that require many steps and multiple specialized agents.

Examples:

* Software development
* Large-scale refactoring
* Research workflows
* Product development
* Documentation generation
* Complex automation

---

# How AICU Works

High-level architecture:

<!-- TODO: Add architecture diagram here

Suggested diagram:

User Task
    ↓
Capability Discovery
    ↓
Organization Planning
    ↓
Dynamic Agent Organization
    ↓
Agent Execution
    ↓
Collaboration Events
    ↓
Organization Adaptation
    ↓
Final Result

The diagram should explain the concept, not implementation details.

-->

```
                User Task
                    |
                    v
          Build Agent Organization
                    |
       +------------+------------+
       |            |            |
       v            v            v
    Agent A      Agent B      Agent C
       |            |            |
       +------------+------------+
                    |
              Collaboration
                    |
                    v
          Organization Evolves
                    |
                    v
              Task Completed
```

---

# Example

A user asks:

```
Build a complete blog platform.
```

Instead of executing a fixed workflow, AICU can create an organization:

```
                Project Goal
                    |
                    v
              AI Organization
                    |
    +---------------+---------------+
    |               |               |
Architect      Backend         Frontend
    |               |               |
    +---------------+---------------+
                    |
                 Testing
```

During execution, if new requirements appear, the organization can adapt.

For example:

```
Backend discovers authentication complexity

        ↓

Security capability is required

        ↓

New collaboration happens

        ↓

Organization adapts
```

---

# AICU Compared With Traditional Agent Workflows

Traditional workflow:

```
Task
 ↓
Fixed Pipeline
 ↓
Agents
 ↓
Result
```

AICU:

```
Task
 ↓
Create Organization
 ↓
Agents Work
 ↓
Collaboration
 ↓
Adapt Organization
 ↓
Continue Execution
 ↓
Complete Task
```

The difference:

> AICU treats the AI organization itself as a dynamic part of solving the problem.

---

# Roadmap

## Current Development

* Dynamic agent organization
* Role and skill system
* Capability-driven planning
* Event-based collaboration
* Agent runtime
* Shared workspace
* Artifact management

## Future

* Large-scale capability ecosystem
* More adaptive organization evolution
* Advanced evaluation system
* Agent interoperability
* More efficient collaboration strategies

---

# Documentation

Documentation is under development.

Future documentation will include:

* Architecture overview
* Agent organization concepts
* Capability system
* Collaboration model
* Runtime design
* Examples and tutorials

---

# Contributing

Contributions are welcome.

Areas of interest:

* Multi-agent collaboration
* Agent organization design
* Capability systems
* Agent evaluation
* Runtime infrastructure
* AI workflow research

---

# License

See [LICENSE](LICENSE).
