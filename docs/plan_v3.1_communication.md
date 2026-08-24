# AICU V3.1 节点通信与双图架构 — 规划记录

> ⚠️ **【已废弃 · 由「独立 AICU Router」（方案三）取代】**
> 本文档规划的 `related_nodes` / Communication Graph / 双图模型机制，在方案三落地后已**整体删除**：
> 事件不再按 `related_nodes` 可达性投递，也不再维护第二张 Communication Graph。唯一组织事实 =
> **Organization Graph**；所有 Node 产生极简 Event `{id, producer, content}`，由独立 **AICU Router**
> 在运行时根据「Event + 当前组织图」用 LLM 决策投递给哪些目标节点。
> 本文档仅作为历史规划记录保留，实际实现请以 `ARCHITECTURE.md` §6.5（独立 AICU Router）为准。

> 本文档记录 V3.1 的完整规划（各优先级全部），**实施按优先级两个两个推进**，一次只动当前批次，
> 其他条目先不动。当前迭代批次见文末「迭代进度」。

## 一、总体目标

将 AICU 的「组织」与「通信」两个不同问题解耦：

### 1. 组织问题（Organization Tree）
> 谁是谁的 Manager / Worker？谁属于哪个 Manager？

由**组织树**解决：

```text
Root Manager
├── Requirements Manager
│   ├── Requirement Worker
│   └── Review Worker
├── Backend Manager
│   ├── Backend Worker
│   └── Test Worker
└── Frontend Manager
    ├── Frontend Worker
    └── Test Worker
```

### 2. 通信问题（Communication Graph）
> 哪个节点可以直接给哪个节点发送消息？

由 `related_nodes` 解决（既有变量，用户确认复用，不新增 `reachable_nodes`）：

```yaml
related_nodes:
  - node_a
  - node_b
```

> **组织树 ≠ 通信图**。组织关系是树，`related_nodes` 形成的关系天然是图。

```text
组织关系：Tree
通信关系：Graph
```

### 3. 消息传递（Event Bus）
> 节点之间传什么？

Event 是节点之间**唯一的通信载体**。每个 Node 都是 Producer + Consumer，各有自己的 event queue。

三个核心概念职责不重叠：

> **树负责「谁管理谁」；related_nodes 负责「谁可以直接和谁通信」；Event 负责「节点之间传什么」。**

## 二、Event Bus 与 Routing 的定位

- **Event Bus = 节点通信基础设施**：只负责 发送 → 找目标 → 投递 Event → 进入目标节点 queue。
  不负责：判断该和谁通信 / 任务是否合理 / 要不要创建节点 / 是否跨 Manager / 是不是 collaboration。
- **Routing 不再作为跨 Manager 通信机制**：以后不存在「跨 Manager 通信」这个特殊机制，
  只有 **Node → Node Communication**。Worker→Worker / Worker→Manager / Manager→Worker / Manager→Manager 本质完全一样。

## 三、每个 Node 都是 Producer + Consumer

```text
Node A
   │  publish Event
   ▼
Event Bus
   ▼
Node B.event_queue
   ▼
Node B 下一次执行时消费
```

## 四、related_nodes 保持原变量名（用户确认）

不新增 `reachable_nodes`，复用既有 `related_nodes`（ExecutionPlan Step.related_nodes → Node.related_nodes）：

```yaml
related_nodes:
  - xxx
```

## 五、Manager 创建节点时如何生成 related_nodes

Manager 创建子节点时，由 LLM 判断「新节点应该能直接与哪些节点通信」。
`related_nodes` 允许列出**真实节点**（Manager 或 Worker 均可），此时不要求目标节点的子节点已存在。

## 六、动态扩展 related_nodes（P0#3，本轮不做）

假设 Backend Manager 有 `related_nodes: [frontend_manager]`。当 Frontend Manager 动态生成
`frontend_impl / frontend_test / frontend_review` 后，Runtime 自动把新子节点加入原可达该 Manager
的节点的 `related_nodes` 中：

```yaml
related_nodes:
  - frontend_manager   # 保留原 Manager
  - frontend_impl      # 新增
  - frontend_test      # 新增
  - frontend_review    # 新增
```

Runtime 只扩展既有可达关系，不负责「新节点该和谁通信」（仍由最初 LLM 决定）。

## 七、Event 的实际形态

```json
{
  "event_id": "evt_...",
  "type": "api_ready",
  "producer": "backend_impl",
  "target": "frontend_impl",
  "artifact": [],
  "payload": {},
  "created_at": "2026-08-14T10:00:00"
}
```

- **Event 是消息，不是通信规则**：由 `related_nodes` 决定允许发给谁，Event 只是消息载体。
- 带 `target`（Node→Node 寻址）：目标必须在生产者 `related_nodes` 可达列表内才投递，
  **不区分 Manager/Worker**（跨 Manager 直接通信，不经上级中转）。
- 无 `target`：保持按 `config.events.subscribe` 订阅广播（向后兼容）。
- 不加：Event 分类 / workflow / routing rule / priority / dependency / orchestration。

## 八、Session 页面拆两张图 + Event Stream（P1）

- **Organization Graph**：只显示 parent → child。
- **Communication Graph**：只显示 related_nodes。
- **Event Stream**：实时显示 `谁 --事件--> 谁`。

## 九、Node 不保存完整树（P1）

Session/Runtime 保存组织树；Node 只保存 `node_id / parent_id`，需要时由 Runtime 计算树位置。

## 十、优先级与迭代节奏

| 优先级 | 迭代 | 目标 | 重要性 |
|---|---|---|---|
| **P0** | Node → Node Event 通信统一 | 建立新的通信主链路 | ⭐⭐⭐⭐⭐ |
| **P0** | `related_nodes` 支持 Manager | 跨 Manager 通信不再经过上级 Manager | ⭐⭐⭐⭐⭐ |
| **P0** | Runtime 动态扩展 `related_nodes` | 解决动态树导致可达列表失效 | ⭐⭐⭐⭐⭐ |
| **P0** | Event Queue 统一 | 每个 Node 都能 Producer + Consumer | ⭐⭐⭐⭐⭐ |
| **P1** | 双图模型 | Organization Graph / Communication Graph 分离 | ⭐⭐⭐⭐ |
| **P1** | Event 前端展示 | 能看到真实通信行为 | ⭐⭐⭐⭐ |
| **P1** | Node 的树位置动态计算 | 不再在 Node 中维护完整树结构 | ⭐⭐⭐ |
| **P2** | 清理旧 Cross-Manager Collaboration | 删除旧 B1 特殊机制 | ⭐⭐⭐ |
| **P2** | 清理旧 Routing 通信逻辑 | 防止新旧两套机制并存 | ⭐⭐⭐ |
| **P3** | 性能、索引、事件归档等 | 当前不急 | ⭐ |

**实施约束**：两个两个按优先级完成，其他条目先不动，保证字节级回归保护（默认 flag 关）。

## 迭代进度

- [x] **迭代 1（已完成）**：P0#1 Node → Node Event 通信统一 + P0#2 `related_nodes` 支持 Manager
  - `EventMessage` 新增 `target` 字段（Node→Node 寻址；to_dict/from_dict 同步）。
  - `AgentEventManager._route_locked`：带 `target` 事件按 `related_nodes` 可达性直接投递
    （`_resolve_target_node` 解析 node_id/step_ref；`_reachable` 判定可达；**不区分 Manager/Worker**）；
    无 `target` 保持订阅广播（向后兼容）。测试 `tests/test_agent_events.py` TestTargetAddressedDelivery。
  - Worker 发布/收件指令支持 `target` 与可达列表说明（`executables/worker.py`）。
  - PlanValidator `known_node_ids`：`related_nodes` 允许引用图内已存在节点（Manager / 跨 scope），
    由 Planner/Runtime 注入 `_graph_known_node_ids`（node.id ∪ step_ref）。测试 `tests/test_plan_validator.py`。
  - 验收：pytest **747 passed**（732 + 15 新增）+ run_tests **144 passed**。
- [x] **迭代 5 前置（已完成）— P2#2 清理旧 Routing 通信逻辑**（Event 成为唯一通信载体）
  - **事件→重激活桥** `Scheduler._process_reactivation_events()`：把投递给非运行态节点
    （WAITING/COMPLETED/FAILED）的「带 target 事件」解释为请求该节点行动/重激活，复用
    `_activate_target`（抽取自 `_handle_routing`）全部安全边界：related_nodes 能力边界（B1 放宽）、
    A3 Manager 目标拦截、required_role 校验、C1 全局协作预算 + 升级、max_activations + 升级、
    父 Manager 通知清除 + `rewind_parent_chain_for_retry`、mark_terminal。事件 bridge 以
    `prevalidated=True` 调用（投递时已验可达）。每节点每 tick 至多处理 1 条；转换后删除 inbox 文件
    （delete-on-consume，事实仍在全局 events.json 留痕）。
  - **删除旧通信机制**：worker.py 移除 `routing.json` 文件反馈环（`_extract_routing`/
    `_find_routing_file`/`_scan_failure_evidence`/Tier-2 repair-retry/`pending_routing` backfill）；
    manager.py 移除 review JSON 顶层 `collaboration` 字段解析（`_parse_collaboration`）。
    `_activate_target`/`Node.pending_routing`/`_handle_routing` 引擎保留（兼容既有测试）。
  - **Event 语义扩展**：`payload` → 目标节点 `pending_context`；`payload.reason`/event `type` →
    `activation_reason`；event `type` ∈ {`collaboration_request`,`collaboration_response`} 或
    `payload.collaboration_type` → `collaboration_type`（B1 放宽 / C2 升级依赖）；`payload.mark_terminal`
    保留。无 `target` 事件 = 信息型广播，仅投递 inbox 不重激活。
  - **Manager 也是 Producer + Consumer**：expand/review prompt 注入事件发布指令（`_build_event_publish_instructions`）；
    review prompt 的协作指引改为发布 `collaboration_request/response` 事件；`Manager.run()` 阶段完成后
    兜底 `flush_node_events`（scheduler tick 扫描仅在存在 RUNNING 节点时执行，Manager 无 worker 的 flush 兜底）。
  - **B3 双路径**：`_build_collaboration_request_block` 主读 pending_context（桥激活写入）+
    fallback 扫 event_inbox 协作事件（覆盖「目标先于事件 READY/RUNNING」竞态）。
  - 新增测试 `tests/test_event_reactivation_bridge.py`（15）：重激活/状态过滤/预算升级/mark_terminal/协作注入。
    删除过时测试 `test_routing_extraction.py`、`test_worker_retry.py`；更新 worker/manager/rootcause 协作断言。
  - E2E 改写为事件驱动：`e2e_routing_feedback.py`（backend bug → test 反馈事件 → 重激活修复 → 复验）、
    `e2e_abc.py`（A 修复闭环 / B 跨 Manager collaboration_request→respond 事件闭环 / C 预算拦截升级）。
  - 验收：pytest **706 passed** + run_tests **144 passed** + 两个 e2e 全 PASS。
- [x] **迭代 2（已完成）**：P0#3 Runtime 动态扩展 `related_nodes` + P0#4 Event Queue 统一
  （详见下方「迭代 2」记录）
- [ ] **迭代 3**：P1#1 双图模型 + P1#2 Event 前端展示
  - [x] P1#1 双图模型（✅ 已完成，见下方「迭代 3」记录）
  - [ ] P1#2 Event 前端展示
- [ ] 迭代 4：P1#3 Node 的树位置动态计算 + P2#1 清理旧 Cross-Manager Collaboration
- [ ] 迭代 5：P2#2 清理旧 Routing 通信逻辑（✅ 已完成，见上）+ P3 性能/索引/事件归档
- [x] **方案三（已完成）**：独立 AICU Router 取代 Communication Graph 与 `related_nodes` 全链
  （✅ 已完成，见文末「方案三」记录）

## 迭代 3（已完成 P1#1）— 双图模型：Communication Graph + 候选发现 + Manager 决策

**一句话定稿（用户定义，本轮核心）**：
> **Runtime 负责发现「谁可能与当前 Manager 有关」；Manager 负责决定「我的哪些新子节点应该与他们通信」；Communication Graph 负责记录和快速查询最终形成的通信关系；Event 负责在这些关系之上传递消息。**

- **职责分工落地**：Runtime 只做 ① 从 Communication Graph 查 Manager 入/出边 → 候选注入（`communication_candidates`）；② 保存 Manager 产生的 related_nodes；③ 同步 Communication Graph。**删除 P0#3 `_propagate_related_nodes`**（不再无条件把新子节点 append 进任何节点 related_nodes）。
- **`runtime/communication_graph.py`（新建）**：有向可达图实体，边 X→Y = X 的 `related_nodes` 含 Y（与 Event bus `_reachable` 判定维度一致，node_id ∪ step_ref 双匹配）。in/out 双索引；`sync_node`/`sync_from_nodes`/`remove_node`/`in_neighbors`/`out_neighbors`/`edges`。**纯内存派生索引，不独立落盘**：related_nodes 已随 Graph 快照持久化，`Runtime.start()` 时 `sync_from_nodes` 全量重建。
- **通信决策载体 = 既有 `related_nodes` 字段，不加任何新变量**（本轮用户澄清）：Manager 在 expand 时自主决定每个新子节点的 `related_nodes`（出方向：该子节点能直接给谁发消息）。`Runtime._land_communication_decisions` 三入口（inject_plan / submit_fix_plan rebuild / inject_revision_plan **仅新建节点**）落库两件事：① **出方向** 新子节点 related_nodes 已由 planner.translate 保存 → `comm_graph.sync_node`；② **传播** 更新「其他指向该 Manager 的节点」的 related_nodes——对每个 in-neighbor P（P 的 related_nodes 含该 Manager 的 id 或 step_ref），把「在 related_nodes 中引用了 P」的新子节点追加进 P（append-only 保序去重、保留 P 原条目），再 `comm_graph.sync_node(P)`。**这就是「Manager 产出新节点后，看新产生的子节点有哪些要更新进其他指向该 Manager 的节点的可达列表」**：哪些子节点与 P 建立关系由 Manager 通过该子节点的 related_nodes 决定，Runtime 只机械镜像该决策，不是无条件全量追加。
- **候选注入（`executables/manager.py`）**：`_build_expand_prompt` 在 collab_block 之后、event_inbox_block 之前注入 `## 🧭 通信候选（Communication Candidates）` 块（`_build_communication_candidates_block`）。**空守卫**：in/out 均空 → 返回空串（字节回归保持）；**上限截断** N=20/方向（超出标注「…共 M 条」）；不含 8 个事件协议禁用字符串与 `#MGR:`/`#STEP_ROLE:` 桩分派标记。指引明确：通信载体 = 在新子节点的 `related_nodes` 中声明可达对象。
- **删除路径同步**：`_clear_children` / `_remove_children_by_step_ref` 删除子节点后 `comm_graph.sync_from_nodes(all_nodes)` 全量重建（保持与各节点实际 related_nodes 完全一致；残留旧引用按 P0#3 既有语义保留，Runtime 不擅自清理他人可达列表）。
- **测试**：`test_related_propagation.py` 改写为新语义（17，候选发现 / 传播语义 / 选择性 / fix·revise / 持久化重建）；新增 `test_communication_graph.py`（11）+ `test_comm_candidates.py`（8，候选块空守卫/注入/字节回归/截断，断言无 allow_from）。
- 验收：pytest **752 passed**（726 + 26 净新增，其中 test_related_propagation 语义改写）+ run_tests **144 passed** + e2e_routing_feedback / e2e_abc 全 PASS。

## 迭代 2（已完成）— P0#3 Runtime 动态扩展 `related_nodes` + P0#4 Event Queue 统一

- **P0#3 动态扩展 related_nodes**（解决动态树导致可达列表失效，第六节设计落地）：
  - `Runtime._propagate_related_nodes(manager_node_id, new_children)`：当 Manager 动态创建子节点后，
    把新子节点加入「原可达该 Manager 的节点」的 `related_nodes`（**保留原 Manager 条目**）。
    匹配维度 = 该 Manager 的 `node_id` ∪ `step_ref`；每个新子节点追加 `node_id` + `step_ref`
    （Scheduler 重激活桥 `_activate_target` 按 step_ref 精确匹配，只加 id 会匹配不到 → 桥拦截）。
    append-only 保序去重，**从不删除/重排/覆盖**；新子节点自身 related_nodes 不回填（单向）。
  - 三处调用点：`inject_plan`（expand/re-expand）、`submit_fix_plan` rebuild 分支（**只传 rebuilt**
    不传 continued）、`inject_revision_plan`（**只传 new_nodes** 不含 reused）。
    `_v{iteration}` 后缀直接用真实 step_ref（fix rebuild 后为 `fe_impl_v2`），不做去后缀（避免
    `_resolve_target_node` 找不到丢事件）。
  - 持久化：`Node.related_nodes` 经 to_json/from_json + graph.update_node 自动覆盖，无需额外工作。
  - 新增测试 `tests/test_related_propagation.py`（10）：传播/按 id 匹配/无引用不变/单向性/去重保序/
    fix rebuild 后缀/revise 只传新建/无自引用/不改状态/持久化恢复。
- **P0#4 Event Queue 统一**（第三节设计落地）：每个 Node 都是 Producer + Consumer，各有 event queue。
  - **不加新存储类**：既有 4 目录物理布局（`events/` outbox / `events.json` 全局日志 /
    `event_inbox/` 持久 Consumer queue / `events_inbox/` RUNNING 实时目录）即队列实现。
    收敛为统一契约 `runtime.agent_events.event_queue_dirs(node)` → {outbox, inbox, live}，
    `scan_node`/`_deliver` 与 worker prompt 构建器共用（路径数学逐字节不变）；
    新增只读 `list_inbox(node)`（解析待处理事件，不删文件）。
  - **Manager 补 `## 📥` 事件收件箱块**：expand/review prompt 复用
    `Worker._build_event_inbox_instructions`（empty-guard：无订阅且 inbox 空 → 空串，
    保证 `test_event_protocol_not_hardcoded_in_expand_prompt` 字节回归仍绿）。
  - **`_node_workspace` 去重**：删除 worker.py 本地 static（601-607），统一走
    `runtime.agent_events._node_workspace`（agent_events 只 import runtime.models，无循环导入）。
  - **不补程序化中途 consume**：`AICU.ask()` 单次阻塞 + Event 不参与调度，CC 有界轮询
    `events_inbox/` 已是正确机制。广播事件 GC/TTL 属 P3，后置。
  - 新增测试 `tests/test_event_queue_unified.py`（10）：三目录契约/publish 块路径/
    inbox 块路径/Manager expand+review 注入与空守卫/worker 无本地 _node_workspace/
    list_inbox 只读/空目录。
  - 验收：pytest **726 passed**（706 + 20 新增）+ run_tests **144 passed** +
    e2e_routing_feedback 全 PASS + e2e_abc A/B/C 全 PASS。

## 方案三（已完成）— 独立 AICU Router 取代 Communication Graph 与 `related_nodes` 全链

**核心决策（用户定稿）**：删除 Communication Graph 与 `related_nodes` 全链，Organization Graph 成为
唯一组织事实；所有 Node 产生极简 Event `{id, producer, content}`，由独立 **AICU Router** 运行时根据
「Event + 当前组织图」用 LLM 决策目标。分两个独立批次实施：

- **批次 1（P0）Event 极简 + AICU Router + 投递/桥改造**：
  - `EventMessage` 字段收敛为 `{event_id, producer, content, created_at}`（producer = node_id，
    step_ref 仅经 `_resolve_producer_node` 兼容层解析）；删除 type/target/payload。
  - 新建 `runtime/router.py` `AICURouter`：`route(event)` 返回目标 node_id 列表；
    组织图快照（id/step_ref/exec_type/role/status/组织路径/task 摘要）是唯一外部输入；
    硬约束 = 不解析 Event.content 中的任何"目标声明"。stub/real 双模式复用 Executable 模式
    （测试/e2e 走真实 Router 流程 build prompt → ask → parse targets，仅替换底层 AI 为桩；
    桩用**测试专用映射表**基于 Router prompt 决策，绝不解析 content 目标声明）。
  - `AgentEventManager`：删除 type 必填校验、`_resolve_target_node`/`_reachable` 在事件路由中的使用、
    订阅广播分支；`_deliver` 收件语义改造 —— **inbox（`event_inbox/`）是唯一待消费队列**，
    live（`events_inbox/`）只是 RUNNING 节点实时信号，两条消费路径都 delete-on-consume → 单次消费。
  - Scheduler 桥：`_process_reactivation_events` 扫非运行态节点 inbox（delete-on-consume）；
    `_peek_inbox_event` 判定改为「inbox 有文件即处理」；`_consume_inbox_event` 构造
    `{event_content, event_producer}` 上下文；`_activate_target` 删除 collab_request/required_role
    判定，**允许 Manager 成为事件目标**（Router LLM 已看组织图+角色）；C1/C2 预算与升级保留。
  - worker/manager prompt：发布指令 JSON 示例改 `{"content": "..."}`（删除 type/target/payload 示例、
    related_nodes 可达列表块、订阅类型清单）；review 协作块改为通用 `## 📥 待处理事件上下文`。
  - 测试/e2e 改写：Router 桩映射表 + content 事件化（`e2e_abc.py` 场景 B 的 collaboration_request/
    response 改为通用事件）。
- **批次 2（P1）彻底删除**：`runtime/communication_graph.py` 整个文件、`Step.related_nodes`、
  `Node.related_nodes`、`known_node_ids`、`communication_candidates`/`_land_communication_decisions`、
  `_build_communication_candidates_block`、`_activate_target` 的 related_nodes 边界（`prevalidated`
  参数删除，全部激活走 Router 语义）；同步删除相关测试
  （`test_communication_graph.py` / `test_comm_candidates.py` / `test_related_propagation.py`）。
- 验收：pytest 全绿 + run_tests **144 passed** + e2e_abc / e2e_routing_feedback 全 PASS +
  `grep -rn related_nodes|communication_graph|known_node_ids|communication_candidates runtime/ executables/ tests/ e2e_*.py` 无残留。
