# AICU v5 项目超级详解 —— 架构 / 功能 / 文件职责 / 新手修改索引

> 本文档的目标读者是**第一次接触本项目的新手**。读完本文后，你应当能够：
> 1. 理解整个项目的架构是怎么设计的、为什么这么设计；
> 2. 知道项目里"现在有哪些功能"、每个功能由谁实现；
> 3. 知道每个代码文件分别实现了什么；
> 4. **当你想调整某个功能时，能直接定位到需要读取/修改的文件**，不必把项目所有文件都遍历一遍。
>
> 配套文档：`docs/INSTALL.md`（安装与配置）。

---

## 目录

- [一、项目概览](#一项目概览)
- [二、两大子系统](#二两大子系统)
- [三、源码目录 vs 运行时数据目录（重要）](#三源码目录-vs-运行时数据目录重要)
- [四、核心概念速查表](#四核心概念速查表)
- [五、总体架构：分层设计](#五总体架构分层设计)
- [六、核心运行时（runtime/）设计详解](#六核心运行时runtime设计详解)
  - [6.1 会话层（多会话 Runtime）](#61-会话层多会话-runtime)
  - [6.2 Graph + Planner（图与唯一写入口）](#62-graph--planner图与唯一写入口)
  - [6.3 状态机与生命周期](#63-状态机与生命周期)
  - [6.4 Scheduler（调度器）](#64-scheduler调度器)
  - [6.5 独立 AICU Router（方案三：Event + 组织图 → 目标 Nodes）](#65-独立-aicu-router方案三event--组织图-目标-nodes)
  - [6.6 Agent Event Bus（节点通信总线）](#66-agent-event-bus节点通信总线)
  - [6.7 事件→重激活桥（反馈闭环）](#67-事件重激活桥反馈闭环)
  - [6.8 闭环与交付可靠性（V3-2 Delivery）](#68-闭环与交付可靠性v3-2-delivery)
  - [6.9 共享工作区（Shared Workspace）](#69-共享工作区shared-workspace)
  - [6.10 CodeGraph 上下文（AICU_CODEGRAPH）](#610-codegraph-上下文aicu_codegraph)
  - [6.11 角色 / 技能 / 持久化 / SSE / HTTP API](#611-角色--技能--持久化--sse--http-api)
- [七、执行体（executables/）设计详解](#七执行体executables设计详解)
- [八、AI 执行引擎：AICU 与 ClaudeCodeAgent](#八ai-执行引擎aicu-与-claudecodeagent)
- [九、功能清单总览](#九功能清单总览)
- [十、逐文件职责映射（新手定位表）](#十逐文件职责映射新手定位表)
  - [10.1 根目录](#101-根目录)
  - [10.2 runtime/](#102-runtime)
  - [10.3 runtime/delivery/](#103-runtimedelivery)
  - [10.4 runtime/context/](#104-runtimecontext)
  - [10.5 executables/](#105-executables)
  - [10.6 roles/（数据驱动配置）](#106-roles数据驱动配置)
  - [10.7 tests/ 与根级测试脚本](#107-tests-与根级测试脚本)
  - [10.8 frontend/（Vite+React 工作台）](#108-frontendvite-react-工作台)
- [十一、新手修改指南：想改 X → 读 Y](#十一新手修改指南想改-x--读-y)
- [十二、环境变量与功能开关总表](#十二环境变量与功能开关总表)
- [十三、端到端一次运行的真实时序](#十三端到端一次运行的真实时序)
- [十四、常见问题与调试入口](#十四常见问题与调试入口)

---

## 一、项目概览

本项目名为 **AICU v5（AI Coding Universe 第五版）**，是一个**多 Agent 软件研发编排框架**：

- 用户提交一个软件研发任务；
- 系统自动生成一棵 **Manager / Worker 的 Agent 组织树**：
  - **Manager** 负责**规划**（expand）与**评审**（review）两层工作，不做具体编码；
  - **Worker** 负责**产出具体产物**（代码 / 文档 / 测试报告 / 配置……）；
- 树上的节点通过 **Event 事件总线**互相通信（Worker→Worker / Worker→Manager / Manager→Worker / Manager→Manager），形成一个协作闭环：开发 → 测试反馈 bug → 开发修复 → 测试复验 → 全部通过 → 交付；
- 每一步的真实执行由 **AI 编程 Agent** 完成：`executables/` 负责把任务翻译成提示词喂给 AI，AI 在隔离的 `workspaces/{id}/cc_0/` 工作目录里真实读写文件、运行命令。

### 一句话总结

> **AICU = 一张图（Graph）+ 一棵组织树 + 一套事件通信总线 + 一批 Manager/Worker 执行体 + 一层交付保障（Artifact/Assembly Gate）+ 一个多会话 Web 服务（FastAPI + SSE）+ 一个 React 工作台前端。**

---

## 二、系统组成

| 层次 | 代码位置 | 职责 |
|---|---|---|
| **Runtime 后端** | `runtime/` `executables/` `roles/` `aicu.py` `claude_code_agent.py` `main.py` | 会话管理、组织图、任务规划、调度、事件路由、产物交付和 CLI 执行。 |
| **Web 前端** | `frontend/` | React/Vite 工作台，负责图可视化、会话管理、节点详情、事件流和运行配置。 |
| **辅助后端模块** | `backend/` | 独立的 Flask 应用模块，提供 CSV 数据层及其 API；不属于 AICU Runtime 的核心启动链路。 |
| **测试与文档** | `tests/` `docs/` | 验证核心运行时、适配器和前端行为，记录安装与架构说明。 |

AICU-Graph 的核心运行服务由 `main.py` 启动，使用 `runtime/` 提供的 FastAPI、SSE 和任务执行能力；`frontend/` 是对应的 Web 工作台。`backend/` 保留为仓库中的独立后端模块，使用时应按其自身入口和依赖运行。

---

## 三、源码目录 vs 运行时数据目录（重要）

以下目录看起来像源码，但**都是运行期生成的动态数据**，修改框架功能时**不要动它们**：

| 目录 | 内容 | 何时产生 |
|---|---|---|
| `nodes/` | 每个 Node 一个 UUID 目录，存节点执行产物、review 输出、事件 inbox/outbox | 运行时 |
| `workspaces/{uuid}/` | 每个 Node 的 AI 工作目录，内含 `cc_0/`（Agent 干活的地方）| 运行时 |
| `runtime_sessions/` | 多会话持久化索引（`sessions.json`）+ 每个 session 的 `graph.json` | 运行时 |
| `shared/` | 会话级共享工作区（所有节点都能读的产物根）| 运行时 |
| `frontend/dist/` `frontend/node_modules/` | 前端构建产物与依赖 | 构建 |
| `.codegraph/` | CodeGraph 代码图谱索引 | 构建 |
| `events.json` `review_v*.md` `test_report.md` 等根级文件 | 运行/测试痕迹 | 运行/测试 |

项目源码包括：`runtime/` `executables/` `roles/` `aicu.py` `claude_code_agent.py` `main.py` `frontend/` `backend/` `docs/` `tests/`、`conftest.py`、`run_tests.py` 及根级测试脚本；运行时目录和构建产物不属于源码。

---

## 四、核心概念速查表

| 概念 | 含义 | 关键字段/机制 |
|---|---|---|
| **Node（节点）** | 组织树中的一个 Manager 或 Worker | `id`(UUID) `parent_id` `exec_type`(manager/worker) `role` `task` `status` `step_ref` `max_activations` |
| **Edge（边）** | 节点间关系 | `PARENT`（组织关系）、`DEPENDENCY`（执行依赖） |
| **Graph（图）** | 内存中的 Node+Edge 集合，带邻接索引 | `runtime/graph.py` |
| **Planner** | Graph 的**唯一写入口**：校验 ExecutionPlan → 翻译成 Node+Edge | `runtime/planner.py` |
| **ExecutionPlan** | Manager 提交的计划（steps 列表） | 每个 step 含 step_id/exec_type/role/task/config/depends_on |
| **Manager** | 规划者：expand（出计划）+ review（评审子节点产物） | `executables/manager.py`，`manager_phase` 状态机 |
| **Worker** | 执行者：一次任务产出 1 份产物 | `executables/worker.py` |
| **Scheduler** | 调度循环：推进节点状态、激活可运行节点、超时/重试 | `runtime/scheduler.py` |
| **Event（事件）** | 节点间唯一的通信载体 | `EventMessage{event_id, producer, content, created_at, routed_to, status}`（6 字段） |
| **事件投递生命周期** | Router 决策记录 + 投递状态 | `routed`（已决策未投递）→ `delivered`（已落盘）→ `consumed`（已消费）；`routed_to` = 决策目标列表 |
| **AICU Router** | 独立路由决策：Event + 组织图 → 目标 Nodes（LLM，stub/real 双模式） | `runtime/router.py`；与 `structure_version` 绑定，结构变更自动重置会话 |
| **Event→重激活桥** | 把投递给非运行态节点 inbox 的事件解释为"请求该节点行动" | `Scheduler._process_reactivation_events()` |
| **AICU** | AI 执行引擎：把任务转成 Agent 提示词并编排多 peer（num_ccs） | `aicu.py` |
| **ClaudeCodeAgent** | 程序化封装 AI Coding CLI：多轮对话、超时杀进程 | `claude_code_agent.py` |
| **Executable** | 可执行体抽象（Manager/Worker 是其子类），支持 stub/real 双模式 | `executables/base.py` `registry.py` |
| **Delivery（交付保障）** | Artifact Gate + Registry + Assembly Gate 三层 | `runtime/delivery/` |
| **Session** | 一次独立的多会话运行（session_id + 独立 Runtime）| `runtime/session_manager.py` |

---

## 五、总体架构：分层设计

```text
┌────────────────────────────────────────────────────────────────────────┐
│ 展示层    FastAPI REST + SSE            React 工作台（frontend/src）     │
│          runtime/session_server.py     GraphCanvas / NodeInspector / …   │
│          runtime/server.py             SSEProvider (EventSource)         │
├────────────────────────────────────────────────────────────────────────┤
│ 会话层    SessionManager（多会话生命周期）                                │
│          runtime/session_manager.py                                       │
│          每个 Session = 1 个 Runtime + 1 个 Persistence + 1 个 SSEManager │
├────────────────────────────────────────────────────────────────────────┤
│ 编排核心  Runtime                                                          │
│          runtime/runtime.py（门面：提交任务/继续/暂停/修订/事件流）        │
│            ├─ Graph + Planner（图与唯一写入口）                            │
│            ├─ StateMachine（节点状态机）                                   │
│            ├─ Scheduler（调度循环 0.5s tick）                              │
│            ├─ AICURouter（独立路由：Event + 组织图 → 目标 Nodes）          │
│            ├─ AgentEventManager（Event Bus：路由/投递/扫描）                │
│            ├─ Persistence（graph.json 快照）                               │
│            ├─ closed_loop / delivery（闭环台账 + 交付保障）                │
│            └─ RolesManager / SkillsManager（配置驱动）                    │
├────────────────────────────────────────────────────────────────────────┤
│ 执行体层  Executable ABC → Manager / Worker                               │
│          executables/base.py / manager.py / worker.py / registry.py        │
│          （负责：装配 Prompt → 调 AICU → 解析输出 → 状态回写）            │
├────────────────────────────────────────────────────────────────────────┤
│ 引擎层    aicu.py（peer 编排） + claude_code_agent.py（CLI 封装）           │
│          EXEC_MODE = stub（DefaultStubAICU，离线测试）                     │
│                    | real（真实 AI 编程 Agent）                            │
└────────────────────────────────────────────────────────────────────────┘
```

### 关键设计原则

1. **Graph 唯一写入口**：任何对图结构（加节点/加边/改状态）的写入都必须经 `Planner` 或 `Runtime` 的受控方法，保证可审计、可恢复。
2. **组织树是唯一组织事实**：`PARENT` 边（树）管"谁管理谁"。节点间通信不依赖任何预声明关系（无 related_nodes / 通信图）——事件的去向由 **AICU Router** 在运行时结合 Event + 组织图用 LLM 统一决策，节点不需要也不能指定目标。
3. **Event 是唯一的通信载体**：节点间不直接写文件传话，一律发事件——**事件载荷极简**（`{event_id, producer, content}`，`content` 只描述事件本身，目标由 Router 决策）；`routed_to`/`status` 是框架后补的路由元数据（决策记录 + 投递生命周期），不属于事件载荷。
4. **Manager 不写码，Worker 才写码**：Manager 只做两件事——expand（规划）和 review（评审）。
5. **stub/real 双模式**：`EXEC_MODE=stub` 时用 `DefaultStubAICU`（离线、确定性），测试全走 stub；`EXEC_MODE=real` 时才真正调 AI。

---

## 六、核心运行时（runtime/）设计详解

### 6.1 会话层（多会话 Runtime）

**文件**：`runtime/session_manager.py`、`runtime/session_server.py`

- `SessionManager(registry, root_dir="runtime_sessions", max_concurrency=4, workspace_root, restore, roles_dir)`：
  - `create_session(task, settings)`：生成 session_id → 建 session 目录 → 创建 `Runtime + Persistence(graph.json) + SSEManager` → 自动提交根 Manager（submit_root_manager）→ 返回 session。
  - `list_sessions / get_session / pause_session / resume_session / stop_session / delete_session`：生命周期管理。
  - `pause_session` 支持 `drain`（排空后暂停）与 `immediate`（立即中断，记 interrupted 供恢复）。
  - `_build_handle`：组装 Runtime 与回调（`_complete_pause` 暂停完成回调、`_reconcile_terminal_state` 终态对账）。
  - 会话持久化：`sessions.json` 原子写 + 崩溃恢复 `_restore_sessions`。
  - `_summary`：根据图中节点状态**推导**会话状态：Running > Paused > Failed > Completed > Stopped > Waiting。
- `create_session_app(session_manager)`：多会话 v2.0 HTTP 应用（见 6.11）。

### 6.2 Graph + Planner（图与唯一写入口）

**文件**：`runtime/graph.py`、`runtime/planner.py`、`runtime/plan_validator.py`

- **Graph**：`add_node/update_node/get_node/all_nodes/add_edge/remove_edge/dependencies/children/...`；边按类型分索引；`graph_version` 单调版本号（持久化契约）。
- **Planner**：
  - `translate(plan, parent_node_id, iteration_count)`：校验 → 分配 UUID → 建 Node（step_ref 在 iteration>0 时追加 `_v{N}` 后缀）→ 建 PARENT 边 → 建 DEPENDENCY 边 → 注入图。
  - `translate_revision(plan, parent_node_id, reuse_map)`：增量续跑专用——`reuse_map` 里的 step 复用既有节点，其余新建；只重建"被重新激活子节点之间"的依赖边。
  - `restore(nodes, edges, skip_validation=True, graph_version)`：崩溃恢复路径。
  - `rewind_parent_chain_for_retry(parent_id, clear_notification)`：子节点重试时把已推进的父 Manager 链拉回 WAITING。
  - `validate_plan(plan)`：结构校验（steps 非空、必填字段、exec_type 合法、step_id 唯一、depends_on 引用存在且无环、单层 worker ≤8）。
  - `create_retry_node`：**已废弃**，保留仅为兼容旧测试。
- **PlanValidator（`plan_validator.py`）**：与 Planner 分离的**确定性机械校验器**，绝不进化成"第二个 Manager"：
  - `validate_errors(plan, manager_roles, worker_roles)`：`config.num_ccs ∈ {1,2,3,4}`、exec_type↔role 一致性（角色集由 RolesManager 提供）。
  - `validate_warnings(plan)`：软告警（单层 6-8 个 worker → 建议分组）。
  - 协作路由不做任何结构校验（方案三：目标由 Router LLM 决策，不再有 related_nodes / known_node_ids 跨 scope 引用校验）。

### 6.3 状态机与生命周期

**文件**：`runtime/state_machine.py`

- 节点状态全集：`WAITING → READY → RUNNING → COMPLETED / FAILED / STOPPED / PAUSED`。
  - `WAITING`：依赖未满足或等待重激活；`READY`：依赖已满足、待调度派发；`RUNNING`：正在执行。
  - `TERMINAL` 集合 = `{COMPLETED, FAILED, STOPPED}`。
  - 终端态允许**原地重试/路由重激活**：`FAILED/STOPPED/COMPLETED → WAITING/READY`（见 `Node.__setattr__` 状态拦截，`runtime/models.py`）。
- 非法转移抛 `IllegalTransitionError`；状态转移是图更新的核心原子操作。
- 这是 Scheduler 推进、Manager/Worker 执行体回写状态的依据。

### 6.4 Scheduler（调度器）

**文件**：`runtime/scheduler.py`

- 独立线程的 tick 循环（`tick_interval`，约 0.5s；无 Ready/Running/Waiting 节点时自动停止）。
- 职责：
  1. **manager-aware 并发调度**：`slots = min(max_concurrency - effective_running, len(ready))`；WAITING phase 的 Manager 不占并发槽位（`_get_effective_running_count` 排除）；优先调度 WAITING Manager 的子节点，确保 Expand→Children→Review 序列不被阻塞；
  2. 激活可运行节点（依赖已满足 + 在并发槽内 + 未超激活预算）；
  3. `_try_auto_retry`：FAILED 且有重试预算 → 原地重置（Manager 有子节点→仅 re-review / 无子节点或 Worker→WAITING+延迟→READY），不新建节点；`_process_scheduled_retries` 处理到期的延迟重试；
  4. **事件→重激活桥** `_process_reactivation_events()`（见 6.7）；
  5. `_handle_routing` / `_activate_target`：处理节点激活的安全边界（方案三：无 related_nodes 可达边界，允许任意 Node 含 Manager 为目标；保留 C1 预算、max_activations、mark_terminal）。
  6. 父 Manager 通知：子节点全部完成后通知父 Manager 进入 review。
- 全局协作预算 `max_collaboration_rounds`（默认 10）、节点 `max_activations`（默认 3）、Watchdog（异常自动重启，最多 3 次，最小间隔 60s）。

### 6.5 独立 AICU Router（方案三：Event + 组织图 → 目标 Nodes）

**文件**：`runtime/router.py`（AICURouter）；`runtime/runtime.py`（接线）；`executables/manager.py` / `executables/worker.py`（事件发布/收件箱 prompt）

一句话定稿：

> **Organization Graph 是唯一组织事实；Node 产生极简 Event（载荷 `{id, producer, content}`，框架后补 `routed_to`/`status` 元数据）；AICU Router 在运行时根据「Event + 当前组织图」用 LLM 决定交给哪些 Node；目标节点 inbox 有事件即由 Scheduler 消费执行。**

- **Router 是唯一路由形态**：不发展任何第二套"确定性路由规则"（无 Subscription Router / Collaboration Router / Rule Router / content 特判）。唯一形态 = `Event + Organization Graph → LLM Router → Targets`。
- **stub/real 双模式**（复用 Executable 模式）：stub 桩基于 Router prompt（Event + Producer + 组织图）用**测试专用映射表**做固定决策并返回 `{"targets": [...]}`，**绝不解析 Event.content 中的目标声明**；real 模式用 AICU 实例（`num_ccs=1`，`{workspace_root}/router_cc/`）。
- **组织图快照是 Router 的唯一外部输入**：`_graph_snapshot` 只含路由决策所需的节点基本上下文（id/step_ref/exec_type/role/status/组织路径/task 摘要），不建立第二套索引体系。
- **目标可为任意 Node**（Worker→Worker / Worker→Manager / Manager→Manager）——"Worker 发现问题超出职责、需要 Manager 重新规划"是自然协作；Scheduler 不解析事件语义、不校验目标角色（required_role 已删）。
- **硬约束**：`route()` 与 stub 均不解析 `Event.content` 中的任何"目标声明"（content 只描述事件本身）；`producer` 统一为 node_id（step_ref 仅经 `_resolve_producer_node` 兼容层解析）。
- **Router 生命周期（与组织结构版本绑定）**：`Graph.structure_version`（`runtime/graph.py`）只在组织结构变更（新增/删除节点或边、换 parent、改 exec_type/role/step_ref）时自增；运行状态变化不触发。`route()` 每次先 `_maybe_reset_on_structure_change()`——结构版本变化即 `_reset_conversation()`（只清 AICU 各 CC 的 conversation_id，不起新进程）；stub 模式无会话，no-op。
- **快照缓存**：`_graph_snapshot` 按 `graph_version` 缓存节点清单（图没变不扫全量节点）；`task` 截断 ~140 字。
- **目标解析**：`_parse_targets` 容忍 AI 返回 dict / JSON 字符串 / 含围栏文本；`step_ref` 经 `_resolve_node_id` 解析为 node_id（不存在的目标静默丢弃）。real 模式下 `AICU.ask()` 返回元数据 dict（`content`/`content_parsed`/`final_output_path`），先经 `_extract_targets_payload` 归一化再解析。
- **批次演进**：V3.1 的 Communication Graph + related_nodes 全链已删除（组织树 = 通信图的二图模型废弃），不再存在 `communication_graph.py` / `_land_communication_decisions` / 通信候选注入。

### 6.6 Agent Event Bus（节点通信总线）

**文件**：`runtime/agent_events.py`

- `EventMessage{event_id, producer, content, created_at, routed_to, status}`（6 字段；`producer` 恒为 node_id）。
  - `routed_to`：Router 一次决策的产物（event_id → 目标 node_id 列表），属框架元数据而非事件内容；投递/重放从该记录走，**不重跑 Router**。
  - `status`：投递生命周期 `routed`（已决策未投递）→ `delivered`（所有目标 inbox 已落盘，空目标视为 delivered 终态）→ `consumed`（全部目标 inbox 已删除）。旧日志缺省回退 `routed`。
- **四个存储对象**（物理目录即队列实现）：
  - `{node_ws}/events/*.json` —— 出站 outbox；
  - `{session}/events.json` —— 全局事件日志（事实留痕，原子写盘）；
  - `{node_ws}/event_inbox/*.json` —— 持久化 Consumer 队列（唯一待消费队列）；
  - `{node_ws}/events_inbox/*.json` —— RUNNING 实时信号（运行中 Node 消费 inbox 的实时机制，非独立待消费副本）。
- `event_queue_dirs(node)`：统一契约，返回 `{outbox, inbox, live}`。
- **事件格式校验（产出即验证）**：`_prepare_event_raws` 要求 `content` 为非空字符串——非字符串（dict/list/数字）自动 `json.dumps` 兜底规范化放行；空字符串/null/缺字段判定非法 → `_reject_event` 拒收（不摄入），并投递 `EVENT_FORMAT_INVALID` 反馈事件给生产者 inbox，由事件→重激活桥唤醒其重新生成。
- `AgentEventManager`：
  - `publish_events` / `scan_node`（摄取 `{node_ws}/events/*.json`，合法即删源文件，非法改名 `.invalid` 留存现场）/ `scan_all`；
  - `_route_locked`：**所有 event 走 `self._router.route(msg)`** → 返回目标 node_id 列表 → 落回 `msg.routed_to` → 对每个目标 `_deliver(target_node, msg)`。无订阅广播、无 target 寻址/可达判定。
  - `_deliver`：**inbox 是唯一待消费队列**——任何投递先写 inbox 文件（= `msg.to_dict()`）；**live 仅当目标是 RUNNING 态才写**；两条消费路径都 delete-on-consume → 同一 Event 对同一目标恰好消费一次。
  - **重启确定性重投**：`load()` 后 `_redeliver_routed_locked()` 按 `routed_to` 记录幂等重投 `status=routed` 的事件（**不重跑 LLM**），再 `_reconcile_statuses_locked()` 据 inbox 实际文件状态校正状态（routed→delivered / delivered→consumed）。
  - `save`（events.json，tmp+fsync+rename 原子写，`from_dict` 容忍旧字段缺失，写前 reconcile）；`list_inbox(node)`：只读解析待处理事件（不删文件）；`get_event/list_events/event_count`。

### 6.7 事件→重激活桥（反馈闭环）

**文件**：`runtime/scheduler.py` 内 `_process_reactivation_events()`；`executables/manager.py` 内 `_build_event_context_block` 兜底

- 背景（方案三）：`routing.json` 文件反馈环与 related_nodes 可达边界均已删除，Event 成为唯一通信载体。Router 路由产生的 event 进入目标节点 inbox，目标节点空闲（非运行态）时由 Scheduler 消费执行——**"inbox 有事件即处理"**，Scheduler 不做第二次路由判断。
- 桥的行为：Scheduler 每 tick 扫描各非运行态节点（WAITING/COMPLETED/FAILED，`_REACTIVATABLE_STATUSES`）的 `event_inbox/`，inbox 非空即处理，每节点每 tick 至多 1 条；消费后删除 inbox 文件（delete-on-consume，事实仍在 events.json 留痕）。
- 消费语义：读 `event_id / producer / content` → 构造 activation 上下文 `{"event_content": content, "event_producer": producer}` 写入 pending_context → `_activate_target(...)`（无 collaboration_type，无 target 校验）。RUNNING 节点的消费走 CC 有界轮询 `events_inbox/` 信号路径。
- 安全边界（方案三调整后）：C1 全局协作预算（超限升级 `collaboration_budget_exceeded`）、C2 单节点激活上限（任何被 max_activations 拦截都升级 `activation_budget_exceeded`）、父 Manager 通知清除 + `rewind_parent_chain_for_retry`、mark_terminal。**不再有 related_nodes 能力边界 / A3 Manager 目标拦截 / required_role 校验**——角色与目标判断完全由 Router LLM 负责。
- Manager 兜底：`Manager.run()` 阶段完成后 `flush_node_events`（scheduler tick 扫描仅在存在 RUNNING 节点时执行，Manager 无 worker 时需自己 flush）；`_build_event_context_block` 主读 pending_context，fallback 读 inbox 事件（覆盖"目标先于事件 READY/RUNNING"竞态）。

### 6.8 闭环与交付可靠性（V3-2 Delivery）

**文件**：`runtime/closed_loop.py`、`runtime/delivery/*`

核心观念：**`COMPLETED ≠ DELIVERED`**；Shared FS 是产物单一事实源。

- **Closed-loop（A2）**：`build_closed_loop_report` → 输出 `{output}/closed_loop.json`（含 collaboration_ledger、escalations、verdict{overall_passed, children_total, converged, escalation_count}；`converged = overall_passed and not escalations`）。`write_deliverable_manifest` → `deliverable_manifest.json`（`delivery_complete = all(artifact_path non-None)`）。`<!-- VERDICT {...} -->` 结构化缺陷块契约供测试/验收角色回写。
- **Delivery 层**（`AICU_DELIVERY=1` 总开关）：
  - **Artifact Gate**（`gate.py`）：确定性机械校验——containment（在共享根内）、exists、type、contract，通过 `check_gate` 返回错误；`ArtifactGateError` 携带 ErrorObject。`FRAMEWORK_DIR_NAMES` 排除框架目录。
  - **Artifact Registry**（`registry.py`）：非权威快照，`{shared_root}/.artifacts/{step_ref}.json`；`FilesystemRegistry.resolve(@step_ref, @step_ref/sub, @role:name)`。写失败 ≠ Worker 失败。
  - **Assembly Gate**（`assembly.py`）：Session 总装核对，manifest = `scan_shared_root` 扫描真实文件（Registry 只补元数据）；blocking issues：missing_artifact / empty_artifact / output_collision / dependency_mismatch；observation：registry_drift。`compute_session_delivery_state`（invalid/pending/delivered）。
  - **Issues**（`issues.py`）：`DeliveryIssue{severity, issue_type, ...}`，`REPAIR_STRATEGIES`（ARTIFACT_NOT_CREATED → retry_worker；WRONG_LOCATION → revise_instruction；CONTRACT_FAILED → qa_repair），`repair_strategy_for()`。
  - **Contracts**（`contracts.py`）：`ArtifactContract{artifact_type, required_types, validators}`；`TYPE_RULES`（code/document/config/dataset）；`parse_contract`。
  - **Preview**（`preview.py`）：`previewable_path`（文本扩展名白名单）与 `mime_type_for`。
  - **门控**：`delivery_enabled()` = `AICU_DELIVERY=="1"`；`gate_enabled()` = delivery_enabled 且 `AICU_ARTIFACT_GATE != "0"`。

### 6.9 共享工作区（Shared Workspace）

**文件**：`runtime/workspace_resolver.py`

- `resolve_shared_workspace(workspace_root)` = `{workspace_root}/shared`：会话级单一根，所有节点自动挂载。
- `resolve_partition(node.config)`：读 `_shared_workspace` / `_output_path` → 每节点写分区（`{shared_root}/{output_path}`）。
- `_partition_of`：**先 realpath 再 commonpath 防目录穿越**，非法路径回退共享根。
- 纯函数，无 I/O；被 worker 产物归一、Delivery、前端 API 共用。

### 6.10 CodeGraph 上下文（AICU_CODEGRAPH）

**文件**：`runtime/context/*`

- 门控：`AICU_CODEGRAPH==1` 才注入；默认关 → 字节级回归不变。
- `build_codegraph_context(shared_root, node)` / `build_review_context`：fail-silent，`DEFAULT_BUDGET=4096`，角色→项目查询映射 `_ROLE_PROJECT_QUERY`，`_SKIP_PROJECT_ROLES`。
- `codegraph.py`：`_run_cli`（CODEGRAPH_BIN）、`_project_root`（walk-up .codegraph）、`query_group_inventory(shared_root)`（`codegraph files`）、`query_symbol`。
- `resolver.py`：`DEFAULT_BUDGET`、`extract_candidates`（file/symbol 正则）、`truncate`、`assemble`。
- `state.py`：`GraphState(NOT_EXIST/READY/STALE/BUILDING/FAILED)`、`GroupGraphState.ensure_ready`（init/sync 带显式 `[path]`，避免对项目根重复建索引）。
- `metrics.py`：`record` → `{workspace_root}/codegraph_metrics.jsonl`。
- `notify_artifact_updated(shared_root)` → `GroupGraphState.mark_stale`（产物变更即失效）。

### 6.11 角色 / 技能 / 持久化 / SSE / HTTP API

| 文件 | 职责 |
|---|---|
| `runtime/roles_manager.py` | 扫描 `roles/Manager/` + `roles/Worker/` YAML；`list_roles`（跳过 default.yaml）、`get_artifact_contract`（default+role 合并）、`get_role_schema`、`get_role`、`add_role/update_role`；role_id 模式 `^[a-z][a-z0-9_]*$` |
| `runtime/skills_manager.py` | 扫描带 `SKILL.md` 的目录（主目录 + 额外目录）；`list_skills / get_skill / save_skill` |
| `runtime/persistence.py` | `graph.json` 快照的 save/load；graph_version 单调性 |
| `runtime/sse_manager.py` | `SSEManager` 订阅 EventBus `"*"`，`deque(maxlen=10000, ttl=1800)`，微批合并（同 type <200ms 合并），`get_events_since`（空 → snapshot 回退）、`get_snapshot_event`、`get_stats`、`format_sse` |
| `runtime/server.py` | `create_app(runtime, sse_manager)` 单会话 app：`CONFIG_WHITELIST{num_ccs,model,skills,audit_rounds,max_iterations}`、task≤5000、graph/manager/plan/stats、pause/resume/stop/clear、node CRUD + start/pause/stop/continue/task/delete/external-input、`/v1/logs`、`/v1/roles`、SSE `/v1/graph/events`（含 `/since/{seq}`，snapshot/catch-up、1s poll、15s keepalive） |
| `runtime/session_server.py` | `create_session_app(session_manager)` 多会话 v2.0 app：roles（list/schema/get/create/update）、skills（list/get/create/update）、sessions CRUD + settings（api_key_env 凭据检查）、graph（snapshot/link-candidates/pause/resume/stop/clear）、nodes（**手动建节点 create**/update/delete 级联 + control pause/**resume**/stop/continue/revise + **activity（cc_activity）**）、workspace/shared/results/artifact 浏览、edges add/delete（手动建依赖边）、**agent-events（事件日志查询）**、SSE per-session |

---

## 七、执行体（executables/）设计详解

**文件**：`executables/base.py`、`executables/manager.py`、`executables/worker.py`、`executables/registry.py`、`executables/default_stub.py`

### 7.1 Executable 抽象（base.py）

- `Executable` ABC：`run(ctx)`、`stop()`、`pause()`、`resume()`、`cleanup_resources()`；`EXEC_MODE` 由 `BaseContext` 的 `AICU()` 工厂决定 stub/real。
- 协议：`RuntimeGateway`（执行体反向调用 Runtime 的接口）、`SchedulerDelegate`（执行体调 Scheduler 的接口）、`ExecutableRegistry`（`register(cls)` + `create(exec_type)` 工厂）。

### 7.2 Manager（manager.py，约 2100 行）

- **两阶段状态机**：`manager_phase ∈ {expand, review, waiting, ...}`。
  - `_expand`：装配 expand prompt → `_call_aiCU_with_retry` → `_parse_plan` → `submit_plan`（或 `submit_revision_plan` 若 `pending_revise`）→ 置 WAITING + waiting。
  - `_review`：收集子节点（含产物内容/目录索引）→ 装配 review prompt → `_parse_review_assessments`（返回 assessments+status）→ 全通过则 Assembly Gate + 修复 → COMPLETED + closed_loop；有失败且迭代未超 → `submit_fix_plan` 定向修复。
- **关键内部方法**：
  - `_parse_plan`：dict/str/final_output_path 三种形态解析，尾部花括号修复 + `_parse_markdown_plan` 兜底。
  - `_build_expand_prompt`：上下文快照 + event_context_block + event inbox/publish + revise_candidates + codegraph + task。
  - `_build_review_prompt`：逐子节点 pass/fail + fix JSON 契约；协作走事件（不声明目标，Router 决策）。
  - `_build_event_context_block`：主读 pending_context（桥激活写入的 event_content/event_producer）+ fallback 读 inbox 事件（覆盖"目标先于事件 READY/RUNNING"竞态）。
  - `_build_execution_context_snapshot`：祖先链 + 兄弟节点（按状态排序）+ 可复用产物。
  - `_resolve_review_content` / `_write_review_output` / `_parse_review_assessments`：评审输出的内容归一化、落盘、pass/fail 判定。
  - `_build_repair_context_block` / `_build_repair_review_prompt` / `_degrade_review`：定向修复上下文注入、修复态评审 prompt、评审降级（无可用评审时兜底）。
  - `_run_assembly_gate` / `_repair_blocking_delivery`：交付保障在 Manager 完成阶段执行。
- **Manager 也是 Producer + Consumer**：expand/review prompt 注入事件发布/收件箱指令（`_build_event_publish_instructions`），阶段完成后 `flush_node_events`。

### 7.3 Worker（worker.py，约 1150 行）

- `run(ctx)` 主流程：
  1. 判定 `is_continuation`（`Worker._aicu_cache` 命中）与 `revise_resume` → 清 pending_context；
  2. 选 stub/real AICU → `update_node_status(RUNNING)`；
  3. 构建 routing_prefix（来自 pending_context）→ 清 pending_context；
  4. `build_codegraph_context`；
  5. 首次运行 → `_build_worker_context`（完整上下文：共享产物清单、事件收件箱/发布块、任务）；续跑 → resume 体（--resume conversation_id + 事件 inbox/publish 块）；
  6. `aicu.ask` 提交给单线程 executor；
  7. 成功 → `_normalize_worker_artifact`（共享分区=单一事实源）、Artifact Gate `_validate_worker_artifact_gate`（失败 → FAILED + delivery_state="invalid"）、`_publish_worker_artifact`、`notify_artifact_updated(shared_root)`、`flush_node_events` → COMPLETED + artifact_path + log；
  8. 异常 → FAILED + `ErrorObject.create_worker_execution_error()` + delivery_state="invalid"（若 gate 开）；
  9. `finally`：executor.shutdown + 缓存清理（paused 保留缓存；activation_count≥max 则 pop；activation_count==0 则 pop）。
- 生命周期：`stop`（killpg 杀进程组）、`pause`、`resume`（重跑 run()）、`cleanup_resources`。
- **上下文装配辅助方法**：
  - `_build_worker_context`：完整首跑上下文（共享产物清单、事件收件箱/发布块、任务）。
  - `_build_tiered_reading_instructions(deps)`：**多依赖分级阅读**（依赖 ≥4 触发）——一级全景 Glob 扫描 → 二级精读接口层（≤5 个文件）→ 三级按需深入；含 >800 行文件截断保护，避免通读所有依赖产物导致上下文溢出。
  - `_build_shared_workspace_instructions` / `_build_event_publish_instructions` / `_build_event_inbox_instructions` / `_build_routing_context` / `_build_resume_task_body`。
  - `_handle_cc_stream_output`：real 模式 CC 流式输出分流——`activity` 流只更新 `cc_activity`（不写 node.log）；stderr 写 ERROR 级 log；stdout 默认不写（`AICU_NODE_LOG_STREAMING=1` 才全量写）。
  - `_validate_model`：模型名校验；`_load_worker_artifact_contract` / `_resolve_dep_artifact_path`：产物契约加载与依赖产物路径解析。
- **Revise 续跑回退**：pop 缓存 → 不带 --resume 重建 → 重试一次。

### 7.4 注册表与 stub（registry.py / default_stub.py）

- `registry.py`：`ExecutableRegistry` 注册/创建。
- `default_stub.py`：`DefaultStubAICU`——离线、确定性、基于规则/关键词的假 AI，供测试与无 AI 环境使用。

---

## 八、AI 执行引擎：AICU 与 ClaudeCodeAgent

**文件**：`aicu.py`、`claude_code_agent.py`

### 8.1 AICU（aicu.py，约 1650 行）

- **peer 模式由 `config.num_ccs` 决定**：
  - `1` = solo（单 peer）；
  - `2` = pair（双人协作：实现 + 交叉评审）；
  - `3` = triple；
  - `>=4` = full（并行生成 + 交叉评审 + adversarial 对抗 + audit 审计）。
- **角色配置数据驱动**：加载 `roles/Manager|Worker/{role}.yaml` 覆盖 `default.yaml`；占位符 `{available_roles}` 展开。
- **Sentinel 语义**：None 哨兵（num_ccs/model/cli_name/audit_rounds 未配置）vs `_UNSET` 哨兵（output_file/final_file/modular_output/generate_instruction 显式未提供）。
- **`shared_target`**：canonical abs 路径；所有 CC（含多 CC 编排）直写同一共享分区，产物权威 = 分区本身。
- **工作区结构**：`cc_0..cc_{N-1}/`，每个包含 `CLAUDE.md`（role_md）、`.claude/skills/` 软链 + `--setting-sources project`。
  - `cc_N/{output_file}`、`review.json`、`vote_round_M.json`、`audit.json`；
  - `parent/{final_file}`、`aicu_result.json`。
- 协作产物：`vote_round_M.json`（peer 投票）、`review.json`（评审）、`audit.json`（审计），最终 `aicu_result.json`。

### 8.2 ClaudeCodeAgent（claude_code_agent.py）

- `ClaudeCodeAgent(workspace_path, instruction_md_path, model="DeepSeek-V4-Flash", skip_permissions, cli_name, enable_project_skills, output_callback, api_base_url, api_key_env)`。
- `detect_cli`：按适配器和本机 `PATH` 自动探测可用 CLI。

- `_build_base_command`：根据所选 CLI 组装模型、权限和项目设置参数。
- `chat`：`--print` + `--output-format stream-json/json`、`--resume conversation_id`、stdin 喂 prompt（`Popen` + `start_new_session` + 超时 `killpg`），`_parse_result_output`，失败（`error_during_execution`）抛 `CCExecutionError`。
- 其他方法：`spawn_child`（子工作区 CC）、`interactive`、`execute_bash`、`read_file`。

---

## 九、功能清单总览

| # | 功能 | 入口/文件 | 开关 |
|---|---|---|---|
| 1 | 多 Agent 组织树生成（Manager/Worker） | `runtime/planner.py` `executables/manager.py` | — |
| 2 | Manager expand/review 两阶段 | `executables/manager.py` | — |
| 3 | Worker 单次产物生产 | `executables/worker.py` | — |
| 4 | Scheduler 调度（并发槽/重试/激活） | `runtime/scheduler.py` | — |
| 5 | **独立 AICU Router（Event + 组织图 → 目标 Nodes，结构版本绑定的会话生命周期）** | `runtime/router.py` `runtime/agent_events.py` | 默认开 |
| 6 | **事件投递（6 字段 + routed/delivered/consumed 生命周期 + 重启确定性重投）** | `runtime/agent_events.py` `runtime/models.py` | 默认开 |
| 7 | **事件→重激活桥（反馈闭环唯一触发点）** | `runtime/scheduler.py` `_process_reactivation_events` | 默认开 |
| 8 | **Event Queue 统一（outbox/inbox/live 契约，inbox 唯一待消费队列）** | `runtime/agent_events.py` `event_queue_dirs` | 默认开 |
| 9 | Revise 增量续跑（选择性复用子节点） | `executables/manager.py` `runtime/planner.py` `translate_revision` | — |
| 10 | 协作闭环（collaboration_ledger → closed_loop.json + deliverable_manifest.json） | `runtime/closed_loop.py` | — |
| 11 | 交付保障（Artifact Gate / Registry / Assembly Gate） | `runtime/delivery/*` | `AICU_DELIVERY=1` |
| 12 | 共享工作区（会话级单根 + 每节点写分区 + 防穿越） | `runtime/workspace_resolver.py` | — |
| 13 | CodeGraph 代码图谱上下文 | `runtime/context/*` | `AICU_CODEGRAPH=1` |
| 14 | 角色/技能配置驱动 | `runtime/roles_manager.py` `runtime/skills_manager.py` | — |
| 15 | 多会话 Runtime（SessionManager） | `runtime/session_manager.py` | 默认开 |
| 16 | FastAPI REST + SSE（单/多会话两套 app） | `runtime/server.py` `runtime/session_server.py` | — |
| 17 | React 工作台前端（图可视化 + 会话管理 + 事件流） | `frontend/src/` | — |
| 18 | 崩溃恢复（graph.json 快照 + sessions.json + 事件确定性重投） | `runtime/persistence.py` `runtime/session_manager.py` `agent_events._redeliver_routed_locked` | — |
| 19 | stub/real 双执行模式 | `executables/default_stub.py` `EXEC_MODE` | `EXEC_MODE=stub\|real` |
| 21 | **事件格式校验 + 拒收反馈（content 非空字符串）** | `runtime/agent_events.py` `_prepare_event_raws`/`_reject_event` | 默认开 |
| 22 | **手动建节点 + 依赖边编辑（link-candidates）** | `runtime/runtime.py` `create_manual_node`/`add_dependency`/`get_link_candidates`、`session_server.py` | — |
| 23 | **外部输入等待/投递（external-input）** | `runtime/runtime.py` `register_external_input_wait`/`deliver_external_input`、`server.py` | — |
| 24 | **CC 活动视图（cc_activity 追踪 + /activity 端点）** | `runtime/runtime.py` `update_cc_activity`、`executables/worker.py` `_handle_cc_stream_output` | — |
| 25 | **多依赖分级阅读（tiered reading）** | `executables/worker.py` `_build_tiered_reading_instructions` | 默认开（≥4 依赖触发） |
| 26 | **CC 流式日志分流（粗粒度 log + 细粒度开关）** | `executables/worker.py` `_handle_cc_stream_output` | `AICU_NODE_LOG_STREAMING=1` 才全量写 |

---

## 十、逐文件职责映射（新手定位表）

> 这是本文档的**核心索引**：每个源码文件"实现什么"一览。按目录分组。

### 10.1 根目录

| 文件 | 职责 |
|---|---|
| `main.py` | **服务入口**：置 `EXEC_MODE=real`，注册 Manager/Worker 到 ExecutableRegistry，创建 SessionManager + `create_session_app`，uvicorn 起 `0.0.0.0:8080`，SIGINT/SIGTERM 优雅停机 |
| `aicu.py` | **AI 执行引擎**：peer 编排（num_ccs）、角色配置加载、CC 工作区创建、single-writer 约束、vote/review/audit 产物管理 |
| `claude_code_agent.py` | **AI Coding CLI 程序化封装**：多轮对话（--resume）、stdin 提示、超时 killpg、`CCExecutionError` |
| `conftest.py` | pytest 根级 fixture（stub 模式、临时工作区等） |
| `run_tests.py` | **自包含集成测试编排器**：跑 144 个测试（Models 30 / Graph 25 / StateMachine 30 / Planner 20 / Persistence 10 / EventBus 10 / SSEManager 6 / Registry 3 / Integration 10），产出 `test_report.md` |
| `e2e_*.py`（6 个） | 端到端 stub 场景：`e2e_abc.py`（A 修复闭环 / B 跨 Manager 协作 / C 预算拦截）、`e2e_routing_feedback.py`（backend bug→test 反馈→修复→复验）、`e2e_revise.py`、`e2e_session_delivery.py`、`e2e_session_shared.py`、`e2e_shared_ws_smoke.py` |
| `test_*_e2e.py`（根级，真实模型 harness） | 真实 AI 联调：`test_codegraph_value_e2e.py`、`test_routing_e2e.py`、`test_v2_e2e.py`、`test_manager_collab_e2e.py`、`test_v2_manager_expand.py`、`test_step1.py`、`test_step2.py` |
| `repro_*.py` `rerun_integration.py` `restart_e2e.py` `resume_manager_collab_e2e.py` `retry_failed.py` `analyze_tool_usage.py` | 调试/复现辅助脚本（非核心） |

### 10.2 runtime/

| 文件 | 职责 |
|---|---|
| `models.py` | **数据模型**：Node / Edge / EdgeType / ExecutionPlan / Step / EventMessage（6 字段含 `routed_to`/`status`）/ RoutingDecision（activate/contexts/mark_terminal/reason）/ LogEntry / LogLevel / NodeStatus（含 `READY`）/ ManagerPhase / ErrorObject 等；`to_dict/from_dict` 序列化；Node 含 `cc_activity`/`delivery_state`/`pending_revise`/`activation_count`/`pending_context` |
| `graph.py` | 内存图（Node+Edge + 邻接索引），`add_node/update_node/get_node/all_nodes/add_edge/remove_edge/dependencies/children`、`graph_version`（状态/结构都变）、`structure_version`（仅组织结构变更自增，Router 据此切换上下文）、`set_on_mutation` 变更回调 |
| `planner.py` | Graph 唯一写入口：`translate` / `translate_revision`（增量复用）/ `restore` / `rewind_parent_chain_for_retry` / `validate_plan` / `create_retry_node`（废弃） |
| `plan_validator.py` | 确定性机械校验：num_ccs、exec_type↔role、软告警（不做任何通信关系校验） |
| `state_machine.py` | 节点状态机：合法转移表、`IllegalTransitionError` |
| `scheduler.py` | tick 调度循环：并发槽、激活、自动重试（原地+定时重试）、**事件→重激活桥**、`_activate_target`、预算/升级（C1/C2） |
| `router.py` | **AICU Router**：`route(event)` = 组织图快照 + prompt → LLM/桩 → `{"targets":[...]}` 解析；stub/real 双模式；**生命周期**（`structure_version` 变化 → `_reset_conversation`）、快照缓存、`_resolve_node_id`、real AICU 元数据归一化 |
| `runtime.py` | **Runtime 门面**：`submit_root_manager` / `inject_plan` / `submit_fix_plan` / `inject_revision_plan` / `continue_node` / `revise_manager_node` / `pause` / `resume` / `stop` / 事件流 / escalation 查询 / 崩溃恢复 / **手动建节点（create_manual_node/update_manual_node/delete_manual_node）** / **依赖边编辑（add_dependency/remove_dependency/get_link_candidates）** / **外部输入（register_external_input_wait/deliver_external_input）** / **cc_activity（update_cc_activity/get_cc_activity）** / 共享工作区/workspace 浏览 |
| `agent_events.py` | **Event Bus**：四存储对象、`event_queue_dirs` 统一契约、`_route_locked`（Router 决策投递，落 `routed_to`，inbox 唯一待消费队列）、`_deliver`、`scan_node/scan_all`、`list_inbox`、**格式校验（_prepare_event_raws/_reject_event）**、**重启确定性重投（_redeliver_routed_locked）**、**状态 reconcile（_reconcile_statuses_locked）** |
| `closed_loop.py` | 闭环台账：`build/write_closed_loop_report`（含 verdict/converged/escalation）、`build/write_deliverable_manifest`、VERDICT 块契约 |
| `event_bus.py` | **SSE 层事件总线**（`SSEEvent` + 通配符订阅，供 SSEManager/Scheduler 用），与 `agent_events.py`（节点间 Agent 通信总线）是两条独立总线：前者管前端 SSE 推送，后者管 Agent 协作事件 |
| `persistence.py` | `graph.json` 快照 save/load、graph_version 单调恢复 |
| `workspace_resolver.py` | 共享工作区解析：`resolve_shared_workspace` / `resolve_partition` / `_partition_of`（realpath+commonpath 防穿越） |
| `roles_manager.py` | 角色配置：扫描 YAML、`list_roles` / `get_artifact_contract` / `get_role_schema` / `get_role` / `add_role` / `update_role` |
| `skills_manager.py` | 技能目录扫描：`list_skills` / `get_skill` / `save_skill` |
| `sse_manager.py` | SSE 事件流：环形缓冲、微批合并、`get_events_since`（空→snapshot 回退）、`get_snapshot_event` |
| `server.py` | `create_app` 单会话 FastAPI app（CONFIG_WHITELIST、节点 CRUD、日志查询、SSE `/v1/graph/events`） |
| `session_manager.py` | 多会话生命周期：create/list/pause/resume/stop/delete、`sessions.json` 原子持久化、`_summary` 状态推导 |
| `session_server.py` | `create_session_app` 多会话 v2.0 app（roles/skills/sessions/nodes/workspace/edges/SSE per-session） |

### 10.3 runtime/delivery/

| 文件 | 职责 |
|---|---|
| `__init__.py` | 开关：`delivery_enabled()`（AICU_DELIVERY==1）、`gate_enabled()` |
| `gate.py` | **Artifact Gate**：`check_gate`（containment→exists→type→contract）、`ArtifactRoots`、`FRAMEWORK_DIR_NAMES`、`extract_verdict`、`ArtifactGateError` |
| `contracts.py` | `ArtifactContract`、`TYPE_RULES`（code/document/config/dataset）、`parse_contract` |
| `issues.py` | `DeliveryIssue`、`REPAIR_STRATEGIES`、`issue_type_from_gate_error`、`repair_strategy_for` |
| `registry.py` | **Artifact Registry**：`FilesystemRegistry` → `{shared_root}/.artifacts/{step_ref}.json`、`resolve`（@step_ref/@role:name）、`latest_per_partition` |
| `assembly.py` | **Assembly Gate**：`scan_shared_root`（manifest=真实文件扫描）、`verify_assembly`（blocking issues）、`compute_session_delivery_state` |
| `preview.py` | `previewable_path` / `mime_type_for`（前端预览用） |

### 10.4 runtime/context/

| 文件 | 职责 |
|---|---|
| `__init__.py` | `AICU_CODEGRAPH==1` 门控；`build_codegraph_context` / `build_review_context` / `notify_artifact_updated`；预算 4096 |
| `codegraph.py` | `_run_cli`（CODEGRAPH_BIN）、`_project_root`、`query_group_inventory`、`query_symbol` |
| `resolver.py` | `extract_candidates` / `truncate` / `assemble`（≤4KB 确定性提取） |
| `state.py` | `GraphState` 状态机、`GroupGraphState.ensure_ready`（init/sync 显式 [path]） |
| `metrics.py` | `record` → `codegraph_metrics.jsonl` |

### 10.5 executables/

| 文件 | 职责 |
|---|---|
| `base.py` | `Executable` ABC（run/stop/pause/resume/cleanup_resources）+ 协议（RuntimeGateway/SchedulerDelegate/ExecutableRegistry）+ `BaseContext` |
| `manager.py` | **Manager 执行体**：expand/review 状态机、prompt 装配（事件上下文/事件 inbox/revise 候选/codegraph/repair context）、plan 解析、Assembly Gate 执行、评审输出归一化（`_resolve_review_content`/`_parse_review_assessments`）、评审降级（`_degrade_review`） |
| `worker.py` | **Worker 执行体**：首跑/续跑/revise-resume、产物归一、Artifact Gate、事件发布/收件箱、缓存生命周期、**多依赖分级阅读（`_build_tiered_reading_instructions`）**、**CC 流式输出分流（`_handle_cc_stream_output`）**、模型名校验（`_validate_model`）、产物契约加载（`_load_worker_artifact_contract`） |
| `registry.py` | `ExecutableRegistry` 工厂（`register`/`create`） |
| `default_stub.py` | `DefaultStubAICU`：离线确定性假 AI（stub 模式） |
| `__init__.py` | 包导出 |

### 10.6 roles/（数据驱动配置）

| 文件 | 职责 |
|---|---|
| `roles/Manager/default.yaml` | Manager 基类配置（8 步决策策略） |
| `roles/Manager/engineering_coord.yaml` `release_planner.yaml` | 具体 Manager 角色覆盖 |
| `roles/Worker/default.yaml` | Worker 基类配置 |
| `roles/Worker/dev_member.yaml` `test.yaml` `document.yaml` `architecture.yaml` `requirements.yaml` `code.yaml` `acceptance.yaml` `data_analysis.yaml` `deploy.yaml` `dev_lead.yaml` `grill.yaml` `incremental_plan.yaml` `prototype.yaml` `test_role.yaml` | 具体 Worker 角色；`dev_member`（code+source_file）、`test`（document+verdict_block）等含 `artifact_contract` 示例 |

### 10.7 tests/ 与根级测试脚本

| 文件 | 职责 |
|---|---|
| `tests/conftest.py` `tests/__init__.py` `tests/integration_stubs.py` | 测试基建与集成 stub |
| `tests/test_agent_events.py` | Event Bus 单测（Router 投递 / 单次消费语义 / 状态生命周期 / 格式校验等） |
| `tests/test_event_bus.py` | SSE 层 EventBus 单测（通配符订阅/发布/隔离） |
| `tests/test_event_queue_unified.py` | Event Queue 三目录契约单测 |
| `tests/test_event_reactivation_bridge.py` | 事件→重激活桥单测 |
| `tests/test_router_lifecycle.py` | AICU Router 单测（组织图快照 / prompt / stub 目标解析 / 生命周期） |
| `tests/test_plan_validator.py` | 结构校验单测 |
| `tests/test_artifact_gate.py` `test_artifact_registry.py` `test_assembly_gate.py` `test_delivery_issues.py` | 交付层单测 |
| `tests/test_closed_loop.py` | 闭环台账单测 |
| `tests/test_codegraph_context.py` | CodeGraph 上下文单测 |
| `tests/test_runtime.py` `test_scheduler.py` `test_scheduler_retry.py` `test_scheduler_manager.py` `test_manager.py` `test_worker.py` `test_convergence_budget.py` `test_manager_collaboration.py` `test_rootcause_fixes.py` `test_root_revise.py` `test_routing_reactivation.py` `test_concurrency_robustness.py` | 核心运行时单测（含调度重试 `test_scheduler_retry.py`） |
| `tests/test_session_shared.py` `test_shared_target.py` `test_shared_workspace.py` `test_workspace_resolver.py` `test_session_server.py` `test_server.py` `test_sse_manager.py` `test_registry.py` `test_executable_base.py` | 周边单测 |

### 10.8 frontend/（Vite+React 工作台）

| 文件 | 职责 |
|---|---|
| `frontend/src/App.tsx` `main.tsx` | 应用入口与路由 |
| `frontend/src/workbench.css` | 全局样式 |
| `frontend/src/types.ts` `constants.ts` | 类型与常量 |
| `frontend/src/stores/GraphStore.ts` `SessionStore.ts` `UIStateStore.ts` | zustand 状态（图/会话/UI） |
| `frontend/src/providers/SSEProvider.tsx` | SSE 事件订阅（EventSource） |
| `frontend/src/providers/MockProvider.tsx` | 离线 mock 数据（VITE_MOCK_MODE） |
| `frontend/src/components/GraphCanvas.tsx` `TreeNode.tsx` `DependencyEdge.tsx` | React Flow 图渲染 |
| `frontend/src/components/NodeInspector.tsx` `NodeDetailPanel.tsx` `TaskTab.tsx` `LogsTab.tsx` `ArtifactTab.tsx` `SubgraphTab.tsx` | 节点详情多标签 |
| `frontend/src/components/SessionSidebar.tsx` `SessionOutputsDialog.tsx` `WorkbenchSettings.tsx` `SubmitTaskModal.tsx` `EditTaskModal.tsx` `StopConfirmModal.tsx` `GlobalControlButtons.tsx` `GlobalToolbar.tsx` `GlobalLogSearchModal.tsx` `ExportModal.tsx` `CanvasControls.tsx` `ConcurrencySlider.tsx` `EmptyState.tsx` `StatusBar.tsx` `ToastContainer.tsx` `WorkbenchDialogs.tsx` `WorkbenchHeader.tsx` | 工作台各 UI 组件 |
| `frontend/src/components/mdeditor/` | Markdown 编辑器（编辑/预览/图片上传/自动保存） |
| `frontend/src/utils/` `dagreLayout.ts` `sseEventMapper.ts` `eventBuffer.ts` `layoutCache.ts` `api.ts` | 布局/事件映射/缓冲/API 工具 |
| `frontend/src/hooks/` | `useGraphOptimizations` / `useNodeVisibility` / `usePerformanceMode` |
| `frontend/src/infra/` | 前端基础设施 API 与运行时接口 |

### 10.9 backend/（独立 Flask 后端模块）

| 文件 | 职责 |
|---|---|
| `backend/app.py` `backend/config.py` | Flask 应用工厂与配置 |
| `backend/api/` | API 蓝图，包括认证、文章、标签、统计、上传、RSS 和健康检查 |
| `backend/tests/test_api.py` | Flask 后端模块测试 |

`backend/` 与 `main.py` 启动的 AICU Runtime 相互独立。需要运行该模块时，应安装 `backend/requirements.txt` 并使用其自身入口；它不是 README 快速启动流程的一部分。

---

## 十一、新手修改指南：想改 X → 读 Y

> 这是最实用的一节。**按"你想改什么"查找，先读对应文件，再动手。**

### A. 通信 / 事件相关

| 想做什么 | 读这些文件 |
|---|---|
| 改 Event 消息结构（加字段） | `runtime/models.py`（EventMessage）、`runtime/agent_events.py`（to_dict/from_dict、投递）、`runtime/closed_loop.py` |
| 改事件投递/路由规则 | `runtime/agent_events.py` 的 `_route_locked` / `_deliver`；`runtime/router.py`（AICURouter.route） |
| 改 Router 的路由决策（prompt / 组织图快照 / 目标解析） | `runtime/router.py`（`_build_prompt` / `_graph_snapshot` / `_parse_targets`） |
| 改事件→重激活桥的行为 | `runtime/scheduler.py` 的 `_process_reactivation_events` / `_activate_target` |
| 改节点收件箱/发件箱目录布局 | `runtime/agent_events.py` 的 `event_queue_dirs` |
| 改协作事件的消费语义 | `runtime/scheduler.py`（桥消费 → pending_context）+ `executables/manager.py`（`_build_event_context_block` prompt 注入）|
| 改事件投递生命周期（routed/delivered/consumed）与重启重投 | `runtime/agent_events.py` 的 `_reconcile_statuses_locked` / `_redeliver_routed_locked`、`runtime/models.py`（EventMessage.status/routed_to） |
| 改事件格式校验/拒收反馈规则 | `runtime/agent_events.py` 的 `_prepare_event_raws` / `_reject_event` |

### B. 组织 / 规划相关

| 想做什么 | 读这些文件 |
|---|---|
| 改 Manager 出计划的规则/提示词 | `executables/manager.py`（`_build_expand_prompt`、`_parse_plan`）、`roles/Manager/default.yaml` |
| 改 Manager 评审逻辑 | `executables/manager.py`（`_review`、`_build_review_prompt`、`_parse_review_assessments`） |
| 改计划结构校验规则 | `runtime/plan_validator.py` + `runtime/planner.py`（`validate_plan`） |
| 改单层 worker 上限 | `runtime/planner.py` 的 `MAX_DIRECT_WORKERS` |
| 改 Revise 增量续跑逻辑 | `executables/manager.py`（`pending_revise`）+ `runtime/planner.py`（`translate_revision`） |
| 加新节点类型/角色 | `roles/Worker|Manager/{role}.yaml`、`runtime/roles_manager.py`、`runtime/planner.py`（VALID_EXEC_TYPES） |
| 改"节点不能保存完整树"的树位置计算 | `runtime/runtime.py`（树位置计算）+ `runtime/graph.py` |

### C. 执行 / AI 引擎相关

| 想做什么 | 读这些文件 |
|---|---|
| 改 Worker 一次任务的提示词装配 | `executables/worker.py`（`_build_worker_context`） |
| 改多依赖分级阅读策略 | `executables/worker.py`（`_build_tiered_reading_instructions`） |
| 改 CC 流式日志分流/日志粒度 | `executables/worker.py`（`_handle_cc_stream_output`）、环境变量 `AICU_NODE_LOG_STREAMING` |
| 改 peer 协作模式（num_ccs） | `aicu.py`（策略选择 + 工作区结构） |
| 改 AI Coding CLI 的调用参数 | `claude_code_agent.py`（`_build_base_command` / `chat`） |
| 换默认模型 | `claude_code_agent.py`（`model` 参数）、`main.py`、roles yaml |
| 改 stub 假 AI 行为 | `executables/default_stub.py` |
| 注册新的 Executable 类型 | `executables/registry.py` + `main.py` |

### D. 交付 / 产物相关

| 想做什么 | 读这些文件 |
|---|---|
| 改 Artifact Gate 校验规则 | `runtime/delivery/gate.py`、`runtime/delivery/contracts.py` |
| 改产物类型规则 | `runtime/delivery/contracts.py`（TYPE_RULES） |
| 改总装校验 / 阻塞问题分类 | `runtime/delivery/assembly.py`、`runtime/delivery/issues.py` |
| 改产物注册表 | `runtime/delivery/registry.py` |
| 改闭环台账内容 | `runtime/closed_loop.py` |
| 改共享工作区路径规则 | `runtime/workspace_resolver.py` |

### E. 调度 / 状态 / 生命周期

| 想做什么 | 读这些文件 |
|---|---|
| 改调度 tick 间隔/并发槽规则 | `runtime/scheduler.py` |
| 改重试预算 | `runtime/scheduler.py`（max_activations）、`runtime/models.py` |
| 改节点状态转移表 | `runtime/state_machine.py` |
| 改多会话生命周期 | `runtime/session_manager.py` |
| 改崩溃恢复逻辑 | `runtime/persistence.py`、`runtime/runtime.py`、`runtime/session_manager.py`、`runtime/agent_events.py`（`_redeliver_routed_locked` / `_reconcile_statuses_locked`） |
| 改手动建节点/删节点/依赖边编辑 | `runtime/runtime.py`（`create_manual_node`/`update_manual_node`/`delete_manual_node`/`add_dependency`/`remove_dependency`/`get_link_candidates`）+ `runtime/session_server.py` |
| 改外部输入等待/投递 | `runtime/runtime.py`（`register_external_input_wait`/`deliver_external_input`）+ `runtime/server.py`（`/external-input`） |
| 改 CC 活动视图（cc_activity） | `runtime/runtime.py`（`update_cc_activity`/`get_cc_activity`）、`runtime/models.py`（Node.cc_activity）、`executables/worker.py`（`_handle_cc_stream_output`） |

### F. Web / 前端相关

| 想做什么 | 读这些文件 |
|---|---|
| 加 REST 端点 | `runtime/server.py`（单会话）/ `runtime/session_server.py`（多会话） |
| 改 SSE 事件流格式/缓冲 | `runtime/sse_manager.py` |
| 改前端图渲染 | `frontend/src/components/GraphCanvas.tsx` `TreeNode.tsx` |
| 加前端标签页 | `frontend/src/components/NodeInspector.tsx` + 对应新组件 |
| 改前端状态管理 | `frontend/src/stores/*.ts` |
| 加/改环境变量功能开关 | `runtime/delivery/__init__.py`、`runtime/context/__init__.py`、`runtime/scheduler.py`、`executables/worker.py` |

---

## 十二、环境变量与功能开关总表

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `EXEC_MODE` | `stub`（离线假 AI）/ `real`（真实 AI） | `stub`（测试）/ `real`（main.py） |
| `AICU_DELIVERY` | 交付保障总开关 | 空（关） |
| `AICU_ARTIFACT_GATE` | Artifact Gate 细分开关（`!= "0"` 且 delivery 开才生效） | 开 |
| `AICU_CODEGRAPH` | CodeGraph 上下文注入开关 | 空（关） |
| `CODEGRAPH_BIN` | codegraph CLI 路径 | 默认 |
| `AICU_CODEGRAPH_BUDGET` | 上下文预算（覆盖 4096） | 4096 |
| `MAX_COLLABORATION_ROUNDS`（运行时属性） | 全局协作轮数上限（C1） | 10 |
| `AICU_NODE_LOG_STREAMING` | CC 流式日志全量写入 node.log（`=1` 时；默认只写 stderr/activity 分流） | 关 |
| `VITE_MOCK_MODE` | 前端离线 mock | 关 |

---

## 十三、端到端一次运行的真实时序

```text
1. 用户 POST /api/sessions {task, settings}
   → SessionManager.create_session
2. SessionManager 创建 Runtime，Runtime.submit_root_manager(task)
   → Planner.translate(root ExecutionPlan) → 根 Manager Node (WAITING)
3. Scheduler tick 发现根 Manager 可激活 → 激活 → Manager.run() → _expand
   → AICU(real/stub) 生成 ExecutionPlan → submit_plan → Planner.translate
   → 生成子 Manager / Worker 节点（无 related_nodes；协作靠事件 + Router）
4. Scheduler 继续推进：
   → Worker 节点激活 → Worker.run() → aicu.ask → AI 在 workspaces/{id}/cc_0/ 干活
   → 产物归一到 shared/{output_path} → Artifact Gate → COMPLETED
   → Worker 发事件（content 描述事件本身）→ AICU Router 结合组织图决策目标 → 投递目标 inbox
5. 目标非运行态节点（如 backend_impl）被事件→重激活桥唤醒（inbox 有事件即处理）
   → 读 pending_context（event_content）→ 修复 → 再发事件通知 test 复验
6. 子节点全部完成后 → 父 Manager 收到通知 → _review 评审
   → 全通过 → Assembly Gate → COMPLETED + closed_loop.json + deliverable_manifest.json
   → 逐级向上直到根 Manager 收敛
7. 前端通过 SSE 实时看到：图变化、节点状态、事件流（谁产生事件 → Router 路由 → 目标消费）
```

---

## 十四、常见问题与调试入口

| 症状/问题 | 排查入口 |
|---|---|
| 测试挂了但不确定改动影响面 | 先跑 `python3 run_tests.py`（144 自包含测试），再 `pytest tests/ -x` |
| 事件没到达目标节点 | 看 `runtime/agent_events.py` 的 `_route_locked`/`_deliver`；检查 `runtime/router.py`（Router 是否返回目标、目标 node_id 是否正确） |
| 节点没被重激活 | 看 `runtime/scheduler.py` 的 `_process_reactivation_events`；确认事件已投递到目标 inbox 且目标非 RUNNING；检查 `max_activations` / C1 预算 |
| Manager 评审卡住 | 看 `executables/manager.py` 的 `_review` 与 `_parse_review_assessments`；事件是否被 `flush_node_events` 兜底 |
| 产物报 invalid | 看 `runtime/delivery/gate.py`（Artifact Gate 的 containment/exists/type/contract） |
| 想看真实事件流转 | `events.json` 全局日志 + 前端 Event Stream / `runtime/sse_manager.py` |
| 想跑一个确定性端到端场景 | `python3 e2e_abc.py`（A/B/C 三场景，全 stub，全 PASS） |

---

*文档完。遇到新功能迭代，请同步更新本文档的架构说明与文件职责映射，保持文档与代码同轨。*
