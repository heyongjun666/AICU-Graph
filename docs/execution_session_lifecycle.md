# 执行会话后端全生命周期与细粒度行为说明

> 整理日期：2026-10-19。依据当前磁盘源码核对，描述实际实现，而不是理想设计。
>
> 范围：会话、节点、调度、Manager/Worker、事件、路由、重试、控制、持久化和恢复。不包含前端实现、页面交互、浏览器同步及终端通信。
>
> 本文是代码行为分析，不是运行测试报告。修改仅涉及本文档，没有修改业务实现，也没有执行测试。

## 阅读方法

每个场景尽量回答：

1. 谁触发、什么状态下可以触发？
2. 读取哪些数据，按什么顺序判断？
3. 条件成立、不成立、抛异常时分别走哪里？
4. 节点状态、Manager phase、上下文和磁盘文件怎样变化？
5. 之后由谁继续推进，是否需要人工操作？

特别区分：

| 标记 | 含义 |
|---|---|
| **代码规则** | Python 直接判断、修改状态、写文件或提交执行 |
| **提示词约定** | 框架要求 Agent 做判断，但没有对应的确定性业务判定器 |
| **实现边界** | 不能从当前代码推导出的保证，或与直觉、旧注释不同的行为 |

例如，“先判断事件是否属于职责”主要是 Router/节点 Prompt 的语义要求；“达到 3 条或等待 1800 秒再批量激活”是 Scheduler 的代码规则。两者不能混为一谈。

## 目录

1. [状态、职责和几个独立维度](#1-状态职责和几个独立维度)
2. [服务启动与新建会话](#2-服务启动与新建会话)
3. [手动创建、编辑、删除节点与依赖](#3-手动创建编辑删除节点与依赖)
4. [Scheduler 一次 tick 的实际顺序](#4-scheduler-一次-tick-的实际顺序)
5. [事件发布、校验、去重与摄取](#5-事件发布校验去重与摄取)
6. [Router 如何决定目标与处理失败](#6-router-如何决定目标与处理失败)
7. [事件投递之后，不同状态的节点怎么办](#7-事件投递之后不同状态的节点怎么办)
8. [累计事件、时间兜底与批量重激活](#8-累计事件时间兜底与批量重激活)
9. [目标节点判断职责之后如何结束](#9-目标节点判断职责之后如何结束)
10. [事件消费、恢复与交付保证的边界](#10-事件消费恢复与交付保证的边界)
11. [Worker 一次执行的完整分支](#11-worker-一次执行的完整分支)
12. [Manager 规划、复用、等待、评审与修复](#12-manager-规划复用等待评审与修复)
13. [自动重试及多层预算](#13-自动重试及多层预算)
14. [节点 Pause、Resume、Stop、Continue、Revise](#14-节点-pauseresumestopcontinuerevise)
15. [会话 Pause、Resume、Stop](#15-会话-pauseresumestop)
16. [工作区、任务历史和共享产物](#16-工作区任务历史和共享产物)
17. [持久化、进程恢复与关闭](#17-持久化进程恢复与关闭)
18. [完整场景推演与排查顺序](#18-完整场景推演与排查顺序)
19. [源码与测试索引](#19-源码与测试索引)

---

## 1. 状态、职责和几个独立维度

### 1.1 组件之间如何分工

```text
SessionManager
  └─ 每个会话一个 Runtime
       ├─ Graph：节点、parent 边、dependency 边
       ├─ Planner：校验 ExecutionPlan、翻译为节点和边
       ├─ Scheduler：依赖门控、并发、Future、重激活和重试
       ├─ Manager / Worker：调用 AICU 执行规划、评审或具体工作
       ├─ AgentEventManager：摄取、校验、记录、路由委托、投递和恢复
       ├─ AICURouter：事件 + 当前组织图 → 目标节点列表
       └─ Persistence：图快照保存和读取
```

必须分清 parent 与 dependency：

- `parent` 表示组织归属，影响子节点汇总、父 Manager 等待和 Review。
- `dependency` 表示执行前置条件；只有依赖节点 Completed 才满足。
- 兄弟关系不自动等于依赖关系。
- 事件用于任务协作，不替代依赖检查；收到事件也不能绕过未完成依赖。

### 1.2 不能混用的五个维度

| 维度 | 举例 | 不能推导出什么 |
|---|---|---|
| 节点 `status` | Waiting、Ready、Running、Paused、Completed、Failed、Stopped | Completed 不代表以后不会被事件激活 |
| Manager `manager_phase` | expand、waiting、review、None | status=Waiting 不一定是等依赖，也可能是在等子节点 |
| 会话 metadata | running、pausing、paused、resuming、recovery_required、completed、failed、stopped | 元数据 running 不保证此刻一定有执行任务 |
| Scheduler 线程/暂停闸门 | `is_running`、`_paused` | tick 正在运行不等于允许派发 |
| 被追踪 Future | `_futures`、`active_execution_count` | 图中没有 Running 不等于所有执行线程都退出了 |

事件还有独立状态：`routed / route_failed / delivered / consumed`，与节点状态没有一对一关系。

例如：

```text
事件 = consumed
节点 = Waiting
原因：事件已转成 pending_context，但目标还在等 dependency。
```

### 1.3 状态机不是全部入口行为的唯一说明

普通执行链是：

```text
Waiting → Ready → Running → Completed / Failed
```

Manager 常规执行链是：

```text
Waiting → Ready → Running(expand)
  → Waiting(waiting)
  → Ready(review) → Running(review)
  → Completed，或重新修复后 Waiting(waiting)
```

但 Resume、Continue、Revise、恢复、事件重激活会使用额外转换路径。Scheduler 的 `_transition_node()` 会尝试 StateMachine 校验，失败后仍可能继续由 Node 的底层保护决定。因此不能仅凭状态机表，断言某个 Runtime API 一定不允许转换。

源码：[scheduler.py:784-916](../runtime/scheduler.py#L784-L916)、[models.py](../runtime/models.py)、[state_machine.py](../runtime/state_machine.py)。

---

## 2. 服务启动与新建会话

### 2.1 进程启动时

入口：[main.py:77-146](../main.py#L77-L146)。

1. 注册 `manager → Manager`、`worker → Worker` 执行器。
2. 创建 SessionManager，读取全局配置和历史会话索引。
3. 为历史会话构造 Runtime，并走安全恢复，不能直接续跑旧进程。
4. 创建多会话 API 应用并启动服务。

恢复历史会话与创建新会话的关键区别：

- 新会话：空图，正常启动 Scheduler。
- 历史会话：先恢复图，关 dispatch 闸门，再启动 Scheduler；即使有 Ready 节点也不立即执行。

### 2.2 创建会话实际做什么

入口：`POST /v1/sessions` → `SessionManager.create_session()`。

执行顺序：

1. 生成会话 UUID。
2. 标题优先采用显式 title，否则使用 task 首行前 60 字或默认标题。
3. 过滤会话 config，合并全局 CLI/model/连接信息等默认值。
4. 决定 workspace_root，验证显式 shared_workspace。
5. 新建会话时，按需创建显式外部共享目录。
6. 构造 Runtime、Planner、Scheduler、事件系统和持久化组件。
7. Runtime 启动，开启周期图保存。
8. 把会话加入管理集合并保存 sessions.json。

初始 metadata 包括：

```text
lifecycle_state = running
desired_state = running
restart_safe = False
recovery_required = False
interrupted_node_ids = []
```

**不会自动创建根 Manager，不会因为请求含 task 就执行 task。** 用户必须另行创建节点。

会话级 `max_concurrency` 取全局配置；单节点 `num_ccs` 是另一套配置，不是节点并发数。

源码：[session_manager.py:187-251](../runtime/session_manager.py#L187-L251)、[_build_handle:708-776](../runtime/session_manager.py#L708-L776)。

### 2.3 会话摘要怎样算

`_summary()` 的图状态计算大致依次判断：

1. 有 Running → Running。
2. 否则有 Paused → Paused。
3. 有 Failed，且没有 Ready/Waiting/Running/Paused → Failed。
4. 非空图全部 Completed → Completed。
5. 非空图全部 Stopped → Stopped。
6. 其余 → Waiting。

所以：

- 摘要没有单独输出 Ready 的分支。
- 空图返回 Waiting。
- Completed 与 Stopped 混合但没有 Failed 的图，也可能返回 Waiting。
- “图摘要 Waiting”不一定能证明确实存在一个可执行 Waiting 节点。

节点状态回调可能触发 `_reconcile_terminal_state()`：只有真实 metadata 仍为 running 时，才将终态图摘要写入会话元数据。

源码：[session_manager.py:816-859](../runtime/session_manager.py#L816-L859)、[863-890](../runtime/session_manager.py#L863-L890)。

---

## 3. 手动创建、编辑、删除节点与依赖

### 3.1 创建节点：按发生顺序展开

入口：`POST /v1/sessions/{session_id}/nodes` → `Runtime.create_manual_node()`。

| 步骤 | 判断/动作 | 失败或结果 |
|---|---|---|
| 1 | 合并会话连接信息与节点配置 | 形成节点执行配置 |
| 2 | 校验 role 与 exec_type 对应关系 | 角色属于另一类型时拒绝 |
| 3 | 校验 parent | 必须存在且为 Manager，不能形成非法 parent 关系 |
| 4 | 校验 dependency 引用 | 每个依赖节点必须存在 |
| 5 | 校验共享路径配置 | 非法 output_path、路径穿越等拒绝 |
| 6 | 生成 UUID，准备内部配置 | 注入工作区、共享区、输出分区、资源策略等 |
| 7 | 创建 Node 并写 Graph | 初始 status=Waiting |
| 8 | 创建 parent edge 和 dependency edges | 建边出错时删除刚创建节点并重抛 |
| 9 | `_ensure_scheduler_running()` | 若非 shutdown，启动 tick，但不打开 pause 闸门 |

注意：建边回滚不代表文件系统副作用也事务性撤销。例如配置准备阶段创建的目录，不应理解为一定随着节点回滚删除。

源码：[runtime.py:2678-2732](../runtime/runtime.py#L2678-L2732)、[session_server.py:856-875](../runtime/session_server.py#L856-L875)。

### 3.2 创建后为什么没有运行

按以下顺序检查：

1. 会话 dispatch 是否暂停？创建成功不解除暂停。
2. 是否存在未 Completed 的 dependency？Failed/Stopped 同样阻塞。
3. 是否有具备展开条件的 Manager 正在形成 planning barrier？
4. 当前节点是否正在等待 scheduled retry 的 ready_time？
5. 是否没有并发槽位？Ready 可以正常排队。
6. Scheduler 是否已关闭或 Runtime 已 shutdown？

### 3.3 编辑节点

入口：`Runtime.update_manual_node()`。

- 只有 Waiting/Paused 允许编辑。
- 字段限 task、role、exec_type、config；其他字段不通过该路径修改。
- task 改变时记录原任务；相同 task 不重复记录。
- 有子节点的 Manager 不能改成 Worker。
- 编辑 config 是**整体替换**，不是与旧 config 逐键合并。
- 此路径不重新调用 `_prepare_node_config()`，也不主动启动 Scheduler。

因此编辑 config 与创建/Revise 的配置处理不能等同；不能保证编辑后自动补齐所有内部路径字段。

源码：[runtime.py:2733-2770](../runtime/runtime.py#L2733-L2770)。

### 3.4 删除节点

检查顺序涉及 Runtime 与 Graph 两层保护：

- 有子节点 → 拒绝，不级联删除。
- Running → 拒绝，要求先处理执行状态。
- 其他节点仍依赖它 → Graph 抛 DependencyError。
- 满足条件 → 删除节点、相关边和索引。

删除图节点不等于删除节点工作区，也不会主动重启 Scheduler。

**接口异常边界：** 当前节点路由没有统一捕获所有领域异常。PlanValidationError、DependencyError 不继承 ValueError/RuntimeError，相应错误可能沿未处理异常路径失败，不能把所有拒绝都写成稳定的 HTTP 400/409。

源码：[runtime.py:2842-2872](../runtime/runtime.py#L2842-L2872)、[graph.py:228-280](../runtime/graph.py#L228-L280)、[session_server.py:896-911](../runtime/session_server.py#L896-L911)。

### 3.5 创建/移除 dependency

新增依赖：

1. 拒绝自依赖。
2. 检查是否形成环。
3. 拒绝重复 dependency。
4. Graph 再验证两个端点存在。
5. 创建 dependency 边；当前添加路径不主动启动 Scheduler。

删除依赖：

1. 边不存在 → 拒绝。
2. 如果是 parent edge → 拒绝，不能通过此路径改变组织归属。
3. 删除 dependency。
4. `_ensure_scheduler_running()`，让可能解除阻塞的节点重新被扫描；暂停闸门仍保留。

源码：[runtime.py:2813-2841](../runtime/runtime.py#L2813-L2841)。

### 3.6 另一条根节点入口，不要和手动节点混写

`submit_root_manager()` 会检查是否已有根 Manager，并创建 `Ready/expand` 的根节点。

这与 `create_manual_node()` 的 `Waiting` 初态、无同样单根保护的行为不同。阅读旧单 Runtime 调用链时，应先确认实际进入了哪个入口。

源码：[runtime.py:600-650](../runtime/runtime.py#L600-L650)。

---

## 4. Scheduler 一次 tick 的实际顺序

源码：[scheduler.py:338-429](../runtime/scheduler.py#L338-L429)。循环间隔常量为 0.5 秒，但一次 tick 内的耗时会额外增加实际周期。

### 4.1 顺序

```text
未暂停时：尝试一次事件恢复
  ↓
统计图节点状态，判断是否自动停止
  ↓
清本 tick 的 Manager 已通知集合
  ↓
未暂停时：处理到期 scheduled retries
  ↓
回收已完成 Future（暂停时仍执行）
  ↓
若 tick 前统计存在 Running：扫描并路由 outbox
  ↓
再次检查 stop/pause 闸门
  ↓
未暂停：事件批量重激活
  ↓
未暂停：普通 Waiting → Ready
  ↓
未暂停：筛选 Ready 并派发
  ↓
暂停且 active Future=0：回调暂停完成
  ↓
等待下一 tick
```

`running_count` 是前面统计出的值，不是每一步实时重算。短任务结束时的残留 outbox，还有执行器结束阶段的 flush 兜底。

### 4.2 Waiting → Ready 的门控

候选：

- Worker 的 Waiting；
- Manager 的 Waiting，且 phase=None/expand。

不由普通依赖扫描推进：

- `Waiting/waiting` Manager：它在等子节点。
- `Waiting/review` Manager：不按普通 expand 入口处理。

对候选依次检查：

1. 还在 `_scheduled_retries` 中 → 不提前提升，保留 retry_delay。
2. 有 planning barrier 且是 Worker → 等待。
3. 所有 dependency 都 Completed → Ready。
4. 否则保持 Waiting，不级联 Failed。

### 4.3 Planning barrier 的准确条件

不是“图中任何 Manager 没完成就阻塞 Worker”。

Manager 必须同时满足：

- phase=None/expand；
- status∈Waiting/Ready/Running；
- 自己的 dependency 已全部 Completed。

只有存在这样的可展开 Manager，且 `plan_before_execute=True`，才阻挡 Worker。

如果某 Manager 还依赖一个未完成 Worker，不能拿这个 Manager 作屏障，否则会出现 Worker 等规划、Manager 又等 Worker 的死锁。

### 4.4 Ready 不是执行许可的最终保证

派发前 `_schedule_ready_nodes()` 再做一次检查：

- dependency 不满足 → Ready 降回 Waiting。
- Worker 遇 planning barrier → Ready 降回 Waiting。
- Manager waiting/review 阶段不走 dependency 阻塞；expand 阶段要检查。

因此事件、Resume、Continue、重试等旁路即使把节点置 Ready，也不能绕过最终依赖门控。

通过后排序：

1. Waiting/waiting Manager 的直接子节点优先。
2. 同类按最长 dependency 链深度降序。
3. 按 `max_concurrency - effective_running_count` 取节点。

Manager 的 waiting phase 不占有效并发槽位；实际限制不是简单统计 Graph 中 Running 标签数量。

源码：[scheduler.py:467-629](../runtime/scheduler.py#L467-L629)。

### 4.5 创建执行器或执行失败

```text
Ready → Running
  → 构造 ExecutableContext
  → registry.create(exec_type, ctx)
  → thread_pool.submit(run)
  → 保存 Future
```

- Registry 创建失败：直接标 Failed 并通知，然后返回；这一分支没有创建 Future，不会自然进入“完成 Future → 自动重试”的同一链。
- 执行器抛异常：wrapper 捕获并返回 Failed。
- Future 取消：回收时映射为 Stopped。
- Future 回收时节点已不存在：跳过。
- 只有节点仍 Running，回收器才用 Future 返回值更新状态。
- 返回 Failed 时尝试自动重试；随后检查父 Manager，再处理残留路由数据。

**边界：** Future 回收层的保护不等于所有执行器回填路径都有相同保护。不能泛化成“Stop 后任何晚到结果都绝不发生副作用”。

源码：[scheduler.py:678-783](../runtime/scheduler.py#L678-L783)。

### 4.6 什么情况下 tick 自动停止

需要没有 Ready、Running、Waiting，没有 scheduled retry，没有活跃 Future；未暂停时还要求可重激活节点没有可读取 inbox 事件。

所以：

- 所有节点 Completed，但某 Completed 节点有未满阈值事件 → tick 会保留，等时间阈值。
- 一直有 Waiting 节点阻塞在失败依赖上 → 不会因为“没有可执行工作”就一定自动停止。
- 暂停会话不会因为待处理事件自动开闸。
- 没有统一 Future 墙钟超时；0.5 秒是扫描间隔，不是任务执行超时。

Watchdog 捕获 tick 异常并记录，恢复尝试上限 3、最小间隔 60 秒；它不是节点执行 timeout。

---

## 5. 事件发布、校验、去重与摄取

### 5.1 四个存储位置

路径以 `event_queue_dirs(node)` 为准，不硬编码另一套节点目录布局。

| 位置 | 写入者 | 含义 |
|---|---|---|
| `{node_ws}/events/*.json` | Producer CC | 待摄取 outbox |
| `{workspace_root}/events.json` | AgentEventManager | 事件历史、去重及恢复来源 |
| `{target_ws}/event_inbox/{id}.json` | AgentEventManager | 唯一持久待消费队列 |
| `{target_ws}/events_inbox/{id}.json` | AgentEventManager，仅目标 Running 时 | 实时提醒，不是第二份独立工作队列 |

另有 `router_traces.jsonl`：框架写入路由审计记录，不是 Router 必须产出的交付文件。

源码：[agent_events.py:133-187](../runtime/agent_events.py#L133-L187)、[238-256](../runtime/agent_events.py#L238-L256)。

### 5.2 Producer 什么情况下应发布

**提示词约定：** 发布能力默认注入，不依赖旧的 `config.events.publish` 开关。

要求发布的主要是：

- 依赖链覆盖不到的跨链影响；
- 需要其他节点提供信息、决策、复核或改变工作；
- 验收/测试发现缺陷，需要产出方修复。

不应发布：普通开始、完成、进度、验证通过、仅属于正常下游依赖衔接的产物就绪。

要求先合并小发现，避免同一事项换说法重复发布；详细内容写共享产物，objective 只作简短通知。

这不是 Runtime 对“是否业务上值得发布”的完整硬校验。后续仍有噪声过滤和 Router 语义判断。

源码：[worker.py:624-693](../executables/worker.py#L624-L693)。

### 5.3 Runtime 何时发现发布文件

两条主要路径：

1. Scheduler tick 在前面统计有 Running 节点时调用扫描；`scan_all()` 实际遍历全部节点 outbox。
2. Worker/Manager 阶段结束或失败时调用 `flush_node_events()`，兜底摄取已写文件。

不是看到新文件就由文件监听器立即唤醒节点；路由/投递本身也不直接改目标状态。

### 5.4 一个 outbox 文件的分支

`scan_node()`：

1. 节点或 Graph 不存在 → 返回 0。
2. 找不到 `*.json` → 返回 0。
3. JSON 读取/解析失败 → 记录错误，保留原文件，后续仍可再扫。
4. JSON 是 dict → 当成一条事件。
5. JSON 是 list → 只取其中 dict 项。
6. 其他类型 → 没有有效条目；代码没有将所有这种情况转成格式反馈。
7. 有格式不合格条目 → 对坏条目反馈，并把源文件改名 `.invalid` 留现场；同一文件中的合格条目仍可进入后续摄取。
8. 没有格式错误 → 删除源文件，然后做后续噪声过滤、内存摄取、路由和保存。

**实现边界：** 当前文件删除在内存摄取/路由/save 之前，不能描述成“事件先可靠落盘成功，才删除 outbox”。进程在中间崩溃存在交付窗口，详见第 10 节。

源码：[agent_events.py:360-450](../runtime/agent_events.py#L360-L450)。

### 5.5 objective 格式验证

| 输入 | 实际分支 |
|---|---|
| 非空字符串，≤80 字且≤3 句 | 接受 |
| 空字符串 | 拒收 |
| 缺 objective，但有旧 content | 使用旧字段做兼容校验 |
| 缺有效 objective/content | 拒收 |
| 字符串超长或句数过多 | 拒收，不静默截断 |
| 非字符串，如 dict/list/数字 | 转 JSON 字符串后放行；此分支未再次做同样的长度校验 |

句数按代码中的句末标点/换行正则计算，不等于自然语言模型判断句子。

非法事件反馈：

- 原事件不摄入；
- 找到 producer 节点后，直接向其 inbox 投递 `EVENT_FORMAT_INVALID` 反馈；
- producer Running 时也写 live 提醒；
- producer 不存在则只能记录日志；
- 反馈本身不是普通 Router 选目标，直接投回生产者；
- **投回 inbox 不等于下一个 tick 一定运行**，仍受节点状态、数量/时间阈值及执行门控约束。

源码：[agent_events.py:510-610](../runtime/agent_events.py#L510-L610)。

### 5.6 Router 前的两级噪声过滤

#### 第一级：同 Producer 120 秒窗口

1. objective 去空白、标点并转小写，产生比较指纹。
2. 取同 producer 最近 120 秒保留记录。
3. 若记录已有 5 条，后续条目按 rate_limited 丢弃。
4. 否则与已有指纹比较：完全相等或 SequenceMatcher 相似度≥0.8 → duplicate_objective。
5. 通过才加入最近记录并继续摄取。

被这一层抑制的条目：不进入正常事件摄取、不调用 Router、不投 inbox；写日志及 suppressed 审计 trace。它们不是“排队等以后再发”。

#### 第二级：全局 event_id 与长期同 Producer 内容

- `_ingest_locked()` 按 event_id 去重，已有 ID 不新增。
- 缺 ID/时间会补齐；producer 缺失时补调用者 producer。
- `_route_locked()` 再和历史同 producer 的 objective 做相似比较，没有 120 秒限制。
- 长期重复事件已进入事件历史，但不调用 Router，记录 `routed_to=[] / delivered` 和 duplicate_suppressed trace。
- 不同 producer 的相似事件不由这个长期规则互相抑制。

**边界：** 长期判重没有限定“旧事件已经成功消费”，它比较同 producer 历史，因此不能理解成完整的业务幂等判定。

源码：[agent_events.py:71-123](../runtime/agent_events.py#L71-L123)、[451-509](../runtime/agent_events.py#L451-L509)、[611-797](../runtime/agent_events.py#L611-L797)。

---

## 6. Router 如何决定目标与处理失败

### 6.1 进入 LLM 前的短路

如果图中最多只有一个节点且 producer 就在图中：

```text
不调用 Router
routed_to = []
status = delivered
trace.decision = no_other_nodes
```

这意味着单节点场景即使事件建议 current，也会先命中当前实现的“没有其他节点”短路。

长期重复事件也会跳过 Router，见上一节。

### 6.2 Router 获取哪些信息

真正调用 `route()` 时：

1. 用锁串行化本 Router 的 reset/ask。
2. 将 producer 的 node_id 或兼容 step_ref 解析成节点。
3. 根据 graph_version 获取/更新组织图快照。
4. 快照包含 id、step_ref、exec_type、role、status、组织路径、task 摘要、task_summary。
5. Stopped 节点不进入正常候选快照；Paused/Failed 并没有同样统一排除。
6. 重置本次 Router conversation；复用 AICU 实例/工作目录，但不累积每次 route 的对话历史。
7. 发送事件与组织图，要求返回 JSON。

输出语义：

```json
{"targets": ["目标 node_id 或 step_ref"], "reason": "投递或忽略理由"}
```

**Router 的路由结果是 CC 回复的 JSON，不是一个要求 Router 生成的产物文件。** 解析代码保留了兼容 AICU 元数据/旧文件返回值的 fallback，不应倒推为设计上要求 Router 交付文件。

源码：[router.py:92-164](../runtime/router.py#L92-L164)、[302-458](../runtime/router.py#L302-L458)。

### 6.3 第一次职责判断在 Router

**提示词约定：**

先判断是否值得投递：

- 纯状态/完成/验证通过，没人需要改变工作 → ignore。
- 有明确的新行动/决策需求，且目标 role/task_summary 能承接 → deliver。
- 职责不匹配 → 不应投给它。
- 所有节点都不匹配 → 空 targets。

scope、affected_roles、action 都是参考信息，不直接指定目标。Router Prompt 允许必要时超出建议 scope，但后面 Scheduler 对 current/subtree 还有硬预筛，两层语义不完全相同。

### 6.4 返回值解析与失败

| 情况 | 行为 | 外层事件状态 |
|---|---|---|
| 合法空 targets | 正常忽略，不重试 | delivered，无接收者 |
| 合法列表，引用存在 | 解析 node_id，去重复目标 | 后续投递 |
| 某引用不是字符串/无法解析 | 跳过该引用 | 有效目标为零时仍按空路由处理 |
| 输出不是合法 targets JSON | reset 后再 ask 一次 | 看第二次结果 |
| 第二次仍解析失败 | fail-closed 返回空 targets，trace 记录 parse_error | 外层通常记 delivered，而不是 route_failed |
| AICU ask 抛异常 | 向外抛出 | AgentEventManager 标 route_failed |

目标兼容解析包含完整 ID、step_ref、至少 6 位 ID 前缀。前缀匹配的实现不是严格唯一匹配校验，因此不要把它当成可靠的唯一身份声明机制。

**route_failed 与合法 ignore 不同；解析两次失败与抛异常也不同。** 原文“Router 失败都会重试路由”过于笼统。

源码：[router.py:134-164](../runtime/router.py#L134-L164)、[259-285](../runtime/router.py#L259-L285)、[459-541](../runtime/router.py#L459-L541)、[agent_events.py:633-779](../runtime/agent_events.py#L633-L779)。

---

## 7. 事件投递之后，不同状态的节点怎么办

### 7.1 投递阶段只写队列，不调度

对每个 routed_to：

1. 目标不存在 → 记录 target_missing，跳过该目标。
2. 目标存在 → 创建 event_inbox 目录。
3. 同 event_id 的 inbox 文件已存在 → 不重复写入，也不会刷新原文件 mtime。
4. 文件不存在 → `.tmp + fsync + replace` 原子写入。
5. 若目标 Running → 另外写 events_inbox 实时信号。
6. 全部目标 inbox 都存在 → status=delivered。
7. 部分失败/目标缺失 → 保持 routed，待恢复路径处理。

一个事件可以投给多个节点，每个目标有自己的待消费文件和批量等待时间。

源码：[agent_events.py:798-834](../runtime/agent_events.py#L798-L834)。

### 7.2 节点状态分支表

| 目标当前状态 | 是否写持久 inbox | Scheduler 批量桥是否处理 | 后续行为 |
|---|---|---|---|
| Waiting | 是 | 是 | 达阈值后注入上下文；仍受依赖/屏障限制 |
| Completed | 是 | 是 | 达阈值后重新进入 Waiting/Ready，可再次执行 |
| Ready | 是 | 否 | 避免重复激活；执行器启动时可能直接读 inbox |
| Running | 是，同时写 live | 否 | 当前 Agent 自行融合/有界读取，不重启当前执行 |
| Failed | 是，若路由到它 | 否 | 事件不负责复活；自动重试或人工 Continue/Revise 是另一条路径 |
| Paused | 是，若路由到它 | 否 | 保留队列，不用事件覆盖人工暂停 |
| Stopped | 投递函数本身可写，但正常 Router 候选排除它 | 否 | 不由事件复活 |

**数量/时间阈值不是所有事件消费的统一前置条件。** Ready/Running 或正常依赖调度起来的节点，可以在执行中直接读取 inbox，不必先等到 3 条/1800 秒。

### 7.3 Running 节点收到事件是否必定立即处理

不是。

- 框架写 live 文件不会自动向 CLI 注入一轮新消息，也不强制中断当前任务。
- Worker 的 inbox Prompt 建议在需要等待协作时做有界轮询，例如有限次数、间隔数秒。
- 这是 Agent 指令，不是 Python 创建了一个固定轮询消费线程。
- `_build_event_inbox_instructions()` 在构建时 inbox 为空就返回空串，因此不能保证每次启动都带这段实时读取说明。
- 如果 Agent 没处理新事件，inbox 留存；当节点后来 Completed，Scheduler 可再按批量规则处理。

源码：[worker.py:694-770](../executables/worker.py#L694-L770)。

---

## 8. 累计事件、时间兜底与批量重激活

这是“达到一定 event 次数或时间，再一次性唤醒处理”的完整实现链。

### 8.1 阈值在哪里，何时检查

Scheduler 构造默认值：

```python
event_batch_threshold = 3
event_batch_max_age = 1800.0
```

它们是 Scheduler 参数；不能直接假设在任意 node.config 中设置同名字段就能生效。

每次未暂停的 tick，在普通 Waiting→Ready 之前执行 `_process_reactivation_events()`。

触发条件有三个，满足任一即可：相关事件数达到阈值、最老事件年龄达到兜底时限，**或者整张图已空闲**（`graph_idle`，见 8.4）。

源码：[scheduler.py:98-124](../runtime/scheduler.py#L98-L124)、[1202-1280](../runtime/scheduler.py#L1202-L1280)。

### 8.2 第一步：只选 Waiting/Completed 节点

```python
_REACTIVATABLE_STATUSES = (NodeStatus.WAITING, NodeStatus.COMPLETED)
```

其余状态完全不进入这轮批量桥。

对候选读取 inbox 的 `*.json`：

- JSON/读取错误 → 日志后跳过，文件不删。
- 只有 dict 进入候选事件列表。
- 按文件 mtime 升序排序。
- 获取 mtime 失败时使用 0，可能使该条目立即满足年龄兜底。

### 8.3 第二步：scope 硬预筛，不是职责判断

| scope / producer | 代码判断 |
|---|---|
| current，producer 能解析 | 只允许目标就是 producer 自己 |
| subtree，producer 能解析 | 目标必须通过 parent 链位于 producer 下方 |
| related/global/其他值 | 此处放行，不分析业务内容 |
| producer 无法解析 | 保守放行，不误判越界 |

`_is_descendant_of()` 从目标的 parent 开始回溯，所以当前实现的 subtree 判断**不包含 producer 自身**。

明确越界的事件：

1. 删除该目标的 inbox 文件。
2. 记录 declined 日志。
3. 不纳入数量与最老时间计算。
4. 不调用 Agent 判断。
5. 不改变节点状态，也不把节点强制标 Completed。

例如：节点原本 Waiting，收到 current scope 的别人的事件，过滤后仍然 Waiting；原本 Completed 则仍然 Completed。

源码：[scheduler.py:548-563](../runtime/scheduler.py#L548-L563)、[1202-1280](../runtime/scheduler.py#L1202-L1280)。

### 8.4 第三步：数量、年龄或图空闲，满足一个即可

对筛完的 relevant 事件：

```python
trigger = (
    graph_idle
    or len(relevant) >= self.event_batch_threshold
    or (now - oldest_mtime) >= self.event_batch_max_age
)
```

其中 `graph_idle` 是**整张图**的判定，不是单节点判定：

```python
graph_idle = (
    stats["Ready"] == 0
    and stats["Running"] == 0
    and stats["Waiting"] - waiting_with_events == 0
    and active_execution_count == 0
    and not scheduled_retries
)
```

含义是：没有 Ready/Running 节点；所有 Waiting 节点都只是在本轮有待处理事件（`waiting_with_events` 恰好等于 Waiting 总数），没有别的等待依赖放行的下游；没有在途执行；也没有已排期的重试。此时不会再有正常任务推进，继续凑批只会白白拖延，所以第一条相关事件到达就立即批量重激活。

只要图中还有其他活跃工作（Ready/Running、在途执行、排期重试，或存在不靠事件推进的 Waiting 节点），`graph_idle` 为假，才需要满足数量或年龄条件。

其余含义：

- 统计单位是**这个目标节点当前 inbox 中的相关事件文件数**。
- 不是全会话发布总数、不是 activation_count、不是同一事件重发次数。
- 可以来自不同 producer。
- 使用文件 mtime，不使用 Event.created_at。
- 不是每来一条就重新计时的 debounce；最老未消费文件的年龄持续增长。
- 达到第 3 条即具备触发条件，不必同时等 30 分钟。
- 有 8 条时一次处理这 8 条，不是拆成 3+3+2。
- 实际发生在后续 tick，路由耗时、暂停等都会影响实际等待长度。

未达阈值：不删除 relevant 文件，不写 pending_context，不因这批事件修改状态。

**但普通任务调度仍可继续。** Waiting Worker 如果 dependency 已满足，后面的普通 Waiting→Ready 可能照常派发，并在执行时读取尚未达阈值的 inbox。事件阈值限制的是“为了事件额外唤醒”，不是“节点只要有事件就必须等批”。

### 8.5 第四步：合并 event_batch

每条事件提取：

```text
event_id / producer / objective / intent / scope / action / affected_roles
```

形成：

```json
{
  "event_batch": ["完整事件条目1", "完整事件条目2", "完整事件条目3"],
  "event_content": "最早事件的 objective",
  "event_producer": "最早事件的 producer",
  "event_intent": "最早事件的 intent",
  "event_scope": "最早事件的 scope",
  "event_action": "最早事件的 action",
  "event_affected_roles": []
}
```

这里数组只是结构示意，实际每项是 dict。

如果节点已有未执行的 `pending_context.event_batch`：

```text
新 event_batch = 旧的未消费 batch + 本轮 batch
```

这样节点因为 dependency 阻塞时，后来的事件不会覆盖上一批尚未交给执行器的上下文。

标量 event_content 等只是兼容字段，取合并后最早一条；不能据此认为只处理了第一条。Manager 优先读取 canonical event_batch。

### 8.6 第五步：一次激活产生哪些变化

`_consume_inbox_batch()` 构造内部 RoutingDecision，再调用 `_activate_target(..., reset_artifacts=False)`：

1. 注入 event_batch 及兼容字段。
2. 加入 activated_by、activation_reason、activated_at。
3. 追加 routing_history。
4. activation_count 加 1。
5. 将目标回到 Waiting。
6. 回退父 Manager 链、清通知记录，使父节点可重新评审。
7. 保留 artifact，不因为事件激活删除旧产物。
8. 目标为已有子节点的 Manager → `manager_phase=expand`、`pending_revise=True`。
9. dependency 不满足 → 留 Waiting。
10. dependency 满足 → Ready；仍要经过实际派发前的屏障与并发检查。

**当前 activation_count 仅作观测，不设置事件激活次数上限。** 不要根据旧注释或 max_activations 字段推断有自动截断循环的预算。

激活日志中的 activated_by 使用批次最早 producer；不代表整批事件都来自它。完整来源在 event_batch 中。

源码：[scheduler.py:1072-1162](../runtime/scheduler.py#L1072-L1162)、[1305-1427](../runtime/scheduler.py#L1305-L1427)。

### 8.7 第六步：删除 inbox 的时机

实际顺序：

```text
_consume_inbox_batch()
  → 上下文/状态更新返回
  → 删除本轮相关 inbox 文件
  → 后续派发 Manager/Worker
  → Agent 处理业务
```

**不是等 Agent 完成才删除。**

- `_consume_inbox_batch()` 抛异常 → 本轮相关文件不删，后续可再尝试；已有部分内存修改不自动事务回滚。
- 删除某文件失败 → 记录 warning；残留可能再次被扫描。
- 激活成功但 dependency 未满足 → 文件仍会删除，事件已经在 pending_context 中等候。
- 这一调用链没有和 graph.json 保存组成事务。

### 8.8 明确算一遍

先区分两种情形。B 均已 Completed、会话未暂停、B 的依赖全部 Completed、无 planning barrier。

**情形一：整张图空闲。** 除 B 外没有 Ready/Running/等待依赖放行的节点，也没有在途执行和排期重试。

| 时刻 | B 的 inbox | 发生什么 |
|---|---|---|
| t=0 | E1 | `graph_idle=True`，第一条事件就在后续 tick 被合并激活 |
| 激活后 | 文件删除 | pending_context 保存 E1；B 进入 Ready/Waiting |

空闲时不需要凑够三条，也不受 1800 秒兜底限制。

**情形二：图中还有其他活动工作。** 例如另一个节点正在 Running，或有 Waiting 节点正等依赖放行，此时 `graph_idle=False`。

| 时刻 | B 的 inbox | 发生什么 |
|---|---|---|
| t=0 | E1 | 1<3，年龄不足，不因事件激活 |
| t=60 | E1、E2 | 2<3，仍等待 |
| t=70 | E1、E2、E3 | 后续 tick 合并三条，激活一次 |
| 激活后 | 三个文件删除 | pending_context 保存三条；B 进入 Ready/Waiting |
| 有槽位后 | 新事件可能另行入队 | B Running，处理当前批次 |

如果 E3 一直不来且图中仍有其他活动工作，t≈1800 后按 E1 文件年龄触发，合并 E1、E2。若会话一直暂停，则不会到点自动开闸。

---

## 9. 目标节点判断职责之后如何结束

### 9.1 不能统一描述成“不属于职责 → 立即 Completed”

至少有四种不同路径：

| 发生位置 | 如何判断 | 不需要处理时 |
|---|---|---|
| Router | LLM 根据组织图、role/task_summary | 不投递，目标节点状态不变 |
| Scheduler scope 预筛 | current/subtree 的确定性组织边界 | 删除越界 inbox，目标状态不变 |
| Worker 读取事件 | Prompt 要求结合职责判断 | 可忽略事件，但 Python 没有专用 decline→Completed 分支 |
| Manager 增量展开 | LLM 决定是否需要子树行动，返回空 steps | 不派发子节点，进入 Waiting/waiting 或 Ready/review，非直接 Completed |

### 9.2 Worker 直接读取 inbox 的职责判断

`_build_event_inbox_instructions()` 明确要求：

1. 对每条事件，先判断是否属于自己的职责。
2. 不属于 → 忽略，不据此修改产物、重跑或再发布事件。
3. 属于 → 结合领域含义推进任务，必要时读取共享产物详情。
4. 处理完删除 event_inbox 文件，表示消费。

**注意：** 忽略一条事件不等于自己的主任务也结束。Running Worker 原本还有工作，应继续自己的任务。

Python Worker.run() 最终根据 AICU 调用返回情况走普通成功/异常路径，并没有读取统一的 `declined=true` 结果来专门决定完成状态。

### 9.3 Worker 批量激活路径与直接 inbox 路径并不完全一致

批量桥已删除 inbox，事件转进 pending_context：

1. Worker 将 `_build_routing_context()` 输出拼入任务。
2. 该函数会展示 event_batch，但结尾要求“基于以上反馈修订产出并重新交付”。
3. 它本身没有同样的“先判职责，不相关则忽略”明确条款。
4. 如果构造 Prompt 时 inbox 已空，`_build_event_inbox_instructions()` 返回空串。

所以准确结论是：

> Worker 的 inbox Prompt 有职责判断约定，但当前批量重激活 Prompt 不能据此保证每次都重新注入同样的职责过滤要求；更不存在 Runtime 强制“判无关后直接 Completed”的判断器。

如果 Agent 决定无需修改并正常返回，Worker 仍走普通成功回填 Completed。这个结果是 Agent 行为加普通完成路径，不是框架专门实现的无关事件快退。

源码：[worker.py:694-844](../executables/worker.py#L694-L844)、[983-1188](../executables/worker.py#L983-L1188)。

### 9.4 已有子节点的 Manager 收到事件

```text
批量激活
  → Waiting/expand，pending_revise=True
  → 满足依赖后 Ready → Running
  → _expand() 走 _build_expand_prompt()
  → 注入完整事件批次（_build_event_context_block）和既有子节点候选（_build_revise_candidates_block）
  → AICU 决定是否需要行动
```

增量任务优先取 pending_context.incremental_task，没有时取 node.task；事件作为单独的事件上下文块注入。不是把 objective 自动覆写为 node.task。

Manager Prompt 明确要求：

- 每条事件先判断是否落在自己的职责范围；
- 不相关事件不据此调整计划；
- 已有子节点已覆盖、无需重跑也可以判无行动；
- 不要为了非空计划原样重发旧子节点；
- 真有工作时优先 reuse_step_ref 复用，确有新能力才新建。

源码：[manager.py:1115-1231](../executables/manager.py#L1115-L1231)、[1232-1296](../executables/manager.py#L1232-L1296)、[1344-1393](../executables/manager.py#L1344-L1393)。

### 9.5 Manager 返回空计划后的精确状态分支

合法无行动示例：

```json
{"steps": [], "reason": "本批事件不属于本子树职责，已有任务无需调整"}
```

`_expand()` 检查：revision=True、有子节点、steps 为空。

| 条件 | 状态/字段变化 | 后续 |
|---|---|---|
| 普通事件/增量，子节点仍有非终态 | Waiting，phase=waiting | 等子节点结束，再 Review |
| 普通事件/增量，全部子节点终态 | Ready，phase=review | 后续调度 Review |
| pending_context.collaboration_type=repair | 抛 ValueError，run 捕获后 Failed | 已要求修复缺陷，不允许用空计划假装修复完成 |
| 无子节点且 steps 为空 | 不命中“已有子树无需行动”分支，提交给计划校验 | 空计划被拒绝，不会静默永远 Waiting |

普通无行动分支还会：

- 不创建子节点；
- 不重激活子节点；
- 记录 no_action_reason/reason；
- 清 pending_revise 和 pending_context；
- 保留已有子树与产物。

**即使事件不相关，Manager 也不是在 Expand 里直接 Completed。** 后续 Review 全部通过才 Completed；Review 发现已有缺陷仍可能进入修复。

源码：[manager.py:213-340](../executables/manager.py#L213-L340)。

### 9.6 事件只有部分相关

- Scheduler scope 预筛只移除明确组织越界项，不分析“业务职责是否相关”。
- 剩余批次一起交给 Agent。
- Manager Prompt 要求逐条判断，把相关项纳入本轮计划。
- 不应把“其中一条无关”解释成整批丢弃。
- 事件层没有通用的逐条 accepted/declined 业务回执字段；不行动原因主要通过 Agent 输出、Manager 日志、路由 trace 等追溯。

---

## 10. 事件消费、恢复与交付保证的边界

### 10.1 delivered 与 consumed 的真实含义

- `routed`：已有路由目标，但还没有确认所有 inbox 已写入。
- `delivered`：所有目标 inbox 写入完成；空目标也直接是 delivered。
- `consumed`：此前 delivered 且所有目标的 inbox 文件都已经不在。
- `route_failed`：调用 Router 抛错，尚无稳定决策。

consumed 由 save/load/recover 中的 reconcile 根据文件是否存在推断，不是 Agent 回报“业务成功”。

会使 inbox 消失的行为包括：

- Scheduler scope 拒绝；
- Scheduler 把事件转入 pending_context；
- CC 处理后删除。

因此 **consumed 不能证明目标已成功修复、已交付产物或已 Completed**。

多个目标时，只要还有任一目标 inbox 文件存在，通常仍是 delivered。状态更新也不是文件删除瞬间同步发生，而要等 reconcile。

源码：[agent_events.py:893-923](../runtime/agent_events.py#L893-L923)。

### 10.2 重启怎么处理

Runtime 启动先 `load(recover_delivery=False)`，只读历史，避免在安全暂停恢复阶段立即调用 Router。

Scheduler 第一次未暂停运行时调用一次恢复：

| 历史状态 | 行为 |
|---|---|
| routed，有目标 | 按记录目标重投；已有 inbox 不重写；不重新 LLM 决策 |
| routed，空目标 | 改 delivered |
| route_failed | 重新调用 Router；失败继续保留，成功后投递 |
| delivered | 不按 routed 分支重投；据文件检查是否 consumed |
| consumed | 不重放业务处理 |

事件恢复并不是每个 tick 无限重试。`_recover_pending_events_once()` 用标记控制一次，甚至捕获异常后也会置完成标记。后续 route_failed 是否再次尝试取决于恢复入口是否再次被调用，不能承诺“下一个 tick 自动重试”。

源码：[runtime.py:219-345](../runtime/runtime.py#L219-L345)、[scheduler.py:324-337](../runtime/scheduler.py#L324-L337)、[agent_events.py:257-293](../runtime/agent_events.py#L257-L293)、[851-923](../runtime/agent_events.py#L851-L923)。

### 10.3 单队列设计不等于端到端 exactly-once

当前实现通过唯一 inbox 和 delete-on-consume 降低普通路径的重复消费，但没有跨多个文件/状态的统一事务或业务 ACK。

需要知道的窗口：

1. **outbox 删除后、events.json 保存前崩溃**：不能承诺该事件已可靠进入恢复源。
2. **pending_context 写内存、inbox 删除后、graph 保存前崩溃**：图快照与 inbox 可能不一致。
3. **目标部分投递后保持 routed**：恢复会重新遍历目标；若先前成功目标已经删掉文件，可能再次投给它。
4. **删除 inbox 失败**：残留可再次被扫到。
5. **业务执行失败**：事件文件可能早已删除，业务重试依靠节点重试链，而非事件 ACK。
6. **save 失败**：事件系统记录错误，不保证立即重试成功；后续保存可能补齐内存记录，但进程崩溃会失去未落盘部分。

这些是实现边界，不是本次已通过故障注入复现的测试结论。

---

## 11. Worker 一次执行的完整分支

源码：[worker.py:983-1188](../executables/worker.py#L983-L1188)。

### 11.1 启动与输入构造

1. 先查 `Worker._aicu_cache`，判断是否存在续接会话。
2. 记录是不是 `collaboration_type=revise` 的续接；事件 batch 不自动等同 revise 类型。
3. 没有注入 AICU 时创建真实实例。
4. 构造期失败，例如角色/MCP/skills 配置问题 → `_fail_startup()` 回填 Failed，不继续 ask。
5. 更新 Running。
6. **先读取 pending_context 构造 routing_prefix，再清 pending_context**，防止上游反馈丢失。
7. 构造代码图谱等上下文。
8. 首轮：完整项目/节点/依赖/共享区/事件协议上下文 + 当前任务。
9. 续接：保留会话历史，只注入增量、事件和相关协议，不再次整段重复主任务。
10. 单独线程执行 `aicu.ask(task_body)` 并等待结果。

清 pending_context 只表示已转入本次输入，不表示模型已经成功完成。

### 11.2 依赖产物怎么读

Worker 输入会指向依赖节点的产物；多依赖场景有分级阅读提示：先扫描目录、精读接口文件，再按需深入。

这些是 Agent 阅读指令，不是 Scheduler 在 Ready 之前强制读取并验证每一个文件。依赖 Completed 也不自动证明文件内容正确，质量仍依赖执行/评审链。

### 11.3 Resume 失败是否总会重建

仅当执行前已存在缓存且 pending_context.collaboration_type=revise：

```text
第一次 ask 抛异常
  → 清该节点缓存
  → 创建无旧 resume 的新 AICU
  → 重试一次
```

普通事件续接或其他执行失败不自动进入这个窄范围兜底；它们走失败回填及 Scheduler 重试。

### 11.4 成功路径顺序

1. ask 正常返回，记录 CC handoff。
2. 如果节点已 Paused → 返回 Paused，不写普通成功状态。
3. 规范化 artifact_path。
4. 若有共享根，通知共享产物上下文失效。
5. flush 节点 outbox；失败在此被吞掉，不因 flush 本身让整次 Worker 失败。
6. 写完成日志、artifact_path，更新 Completed。
7. Future 完成后 Scheduler 回收，检查父 Manager。

### 11.5 异常路径顺序

- 已 Paused → 返回 Paused，保留暂停意图。
- 否则先尝试 flush 失败前已发布事件。
- `_backfill_failed()` 记录 ErrorObject/日志，返回 Failed。
- Scheduler 根据 Future 的 Failed 进入自动重试判断。

**Stop 边界：** Worker 这里显式检查的是 Paused，不能把它扩写为所有 Stopped 晚到结果都在这里被忽略。节点 Stop 与杀进程的区别见第 14、15 节。

### 11.6 缓存是否始终保留

不是。

finally 中：

- 已无缓存 → 无动作。
- Paused → 保留，供 Resume。
- activation_count=0 且没有 pending_routing → 清缓存。
- 其他情况按当前条件保留。

所以“以后重激活一定 resume 原 CC 会话”不成立，尤其首轮普通 Worker 完成后的 cache 可能已清除。持久化 Graph 也不会把 Python 内存中的 AICU cache 原样恢复。

---

## 12. Manager 规划、复用、等待、评审与修复

### 12.1 run 根据 phase 分派

- None/expand → `_expand()`。
- review → `_review()`。
- 其他 phase 进入 run → ValueError，外层捕获后 Failed。

phase=waiting 应该等待子节点，不应被当成一个正常执行 phase 直接调用。

Manager 成功/失败阶段均尝试 flush 事件；普通异常由 run 捕获，记录错误并回填 Failed。

源码：[manager.py:149-212](../executables/manager.py#L149-L212)。

### 12.2 Expand 从输入到计划

1. pending_revise=True → 构造增量 Prompt；否则完整 Expand Prompt。
2. AICU 调用异常时最多两次额外重试，即最多 3 次 ask，没有 sleep/指数退避。
3. 解析 ExecutionPlan：优先结构化 content，其次兼容文本、旧输出形态等。
4. 判断是否已有子节点或 pending_revise。
5. 有既有子树 → revision 选择性复用通道。
6. 无子树首次规划 → full plan 通道。
7. 合法计划提交后 → Waiting/waiting，清 pending_revise、pending_context。

空计划的无行动/repair/无子节点分支见第 9.5 节，不重复当作“普通成功”。

### 12.3 Plan 在写图前检查什么

Planner/Runtime 校验包括：

- steps 非空（已有子树无行动路径会先被 Manager 特别处理）；
- step 必填项、合法 exec_type、角色匹配；
- step_id 不重复；
- depends_on 指向存在的 step、不自环、不构成环；
- 直接 Worker 数量限制；
- num_ccs 档位和 task_summary 要求；
- 共享路径配置；
- revision 额外验证 reuse_step_ref。

校验不通过一般不进入 Planner 建图。但不能称为全流程数据库事务：例如 full `submit_plan()` 在 inject 前已写 execution_plan；建图后的配置准备也可能失败。

源码：[planner.py:62-256](../runtime/planner.py#L62-L256)、[399-516](../runtime/planner.py#L399-L516)、[runtime.py:1031-1114](../runtime/runtime.py#L1031-L1114)。

### 12.4 Revision 如何决定复用而不是重建

语义层：Manager 对每个增量能力匹配已有候选。

代码层：

1. 有 reuse_step_ref → 必须能解析为当前 parent 的既有直接子节点。
2. 可用既有 step_ref、完整 node_id、唯一 node_id 前缀。
3. 同一旧节点不能被多个新 step 重复复用。
4. 旧 role 与计划 role 不同 → 拒绝复用，应该新建对应能力节点。
5. 被复用节点保留 ID、工作区、产物，注入增量任务并重新激活。
6. 新 step 创建新节点。
7. 未引用旧节点保持原样，不自动删除，也不自动重跑。
8. 重建本次激活节点之间的依赖；涉及未引用节点的旧边保留。

仅使用相同 step_id 不等于复用；真正的复用依据是 reuse_step_ref。

有一个兼容分支：完全没有子节点却带 reuse_ref 时，可警告后按全部新建处理；不能把所有 reuse_ref 错误一概写成同一拒绝路径。

源码：[runtime.py:1031-1291](../runtime/runtime.py#L1031-L1291)。

### 12.5 被复用子节点的字段变化

`_reactivate_reused_worker()` 名字含 worker，但也处理被复用子 Manager：

```text
task/config = 新增量值
pending_revise = True
pending_context = 本次 revise 增量、prior_artifacts、激活原因
activation_count += 1
iteration_count = 0
status = Waiting
```

- 子 Manager → phase=expand，递归增量规划自己的子树。
- Worker → phase=None，清 pending_routing。
- role、关键 AICU 配置或工作区失效时清 cache，否则尽量保留。
- 任务改变时写 task_history。

### 12.6 为什么 Manager 等待不占槽位

Expand 返回 Waiting，Future 在后续 tick 回收。Scheduler 的有效执行数还专门排除 waiting-phase Manager，避免“Manager 占满全部槽位，子节点永远跑不了”。

正常子节点完成回调要求：

1. 找到父 Manager。
2. 父仍 Waiting。
3. 不处于 `expand && pending_revise=True`，否则必须先处理增量。
4. 本 tick 未通知过该父节点。
5. 直接子节点非空，且全部为 Completed/Failed/Stopped。
6. Runtime 再检查后设置 Ready/review。

注意是“全部终态”，不是“全部成功”。Failed 子节点也允许父进入 Review 决定修复。

源码：[scheduler.py:917-983](../runtime/scheduler.py#L917-L983)、[runtime.py:1989-2062](../runtime/runtime.py#L1989-L2062)。

### 12.7 Review 到底收集什么

当前 Review 构造子节点元信息：step_ref、node_id、exec_type、role、task、status、共享目录及 output_path。

- Stopped 子节点被跳过，不进入本轮评估清单。
- 不是 Python 预加载所有子节点完整日志/全部文件再审；Prompt 让 AICU 识别关键产物并定向检查。
- Review 内容统一归一化，供评估解析与报告使用，避免两份解析来源不一致。

### 12.8 Review 输出解析失败，不等于质量不通过

分支：

1. 首次评审结构无法解析 → 给一次修复格式的评审请求。
2. 修复调用本身仍可使用 AICU 异常重试。
3. 修复结果可解析 → 采用修复后的评估和报告。
4. 仍无法解析 → `_degrade_review()` 根据 AICU 元数据决定降级结果。
5. 降级通过时写明确 warning 报告。
6. `unparseable_fail` → Failed，不立即进入精准修复计划。

最后一种失败仍会返回给 Scheduler，不能把 Manager 内“不进入修复计划”扩写成“全系统绝不再自动重试”。

### 12.9 评审通过、可修复、耗尽预算

| 情况 | 行为 |
|---|---|
| failed_assessments 为空 | Completed，phase=None，保存报告与协作台账 |
| 有失败且 `iteration_count + 1 < max_iterations` | submit_fix_plan，仅处理失败项，Manager 回 Waiting/waiting |
| submit_fix_plan 抛错 | Manager Failed，记录计划验证错误，不进入等待修复 |
| 有失败但无预算 | Failed，记录 max iteration exceeded |

成功后会尝试写 closed_loop 和 deliverable_manifest；写这些业务报告发生异常会记日志，代码不会因此自动撤销之前已写的 Completed。

精准修复尽量保留已通过子节点；失败节点是否原地复用还是替换，取决于修复计划与既有节点能力/角色条件。被替代节点退出活跃评估，不能再按“所有历史节点都要重新通过”理解。

源码：[manager.py:341-623](../executables/manager.py#L341-L623)。

---

## 13. 自动重试及多层预算

### 13.1 先区分三种次数

- Manager 单次 AICU 调用的异常重试：默认额外 2 次。
- Manager Review→修复→Review 的质量迭代：node.iteration_count/max_iterations。
- Scheduler 对 Failed Future 的自动重试：`_retry_attempts[node_id]` 对 max_iterations。
- 事件 activation_count：当前仅观测，不作为硬上限。

它们不是同一个计数器。Scheduler 重试字典是内存状态，不能默认进程恢复后完整保留。

### 13.2 自动重试入口和拒绝条件

Future 返回 Failed 后 `_try_auto_retry()`：

1. 无 Planner → 返回。
2. 节点不存在 → 返回。
3. 类型不是 worker/manager → 返回。
4. 当前 Paused/Stopped → 返回，不能让 stale Failed 覆盖人工意图。
5. `_retry_attempts >= max_iterations` → 保持失败，不再安排。
6. 其余按节点类型继续。

### 13.3 Worker 优先增量修复

- 提取 error.message/cause。
- 构造“上一轮失败，请基于保留会话与产物修复，不要从头开始”的增量 task。
- 调用 revise_worker_node，reason=retry（网关支持时）。
- 保留产物，尽量保留缓存。
- 回写本次 iteration_count 和 `_retry_attempts`。
- 登记 `ready_time = now + retry_delay`。

网关没有 revise_worker_node 时才回退全量路径；不能把所有 Worker 自动重试都写成删除产物、重建会话。

### 13.4 Manager/回退路径

进入这条路径会清缓存、尝试删除旧 artifact 文件，然后分支：

| 情况 | 操作 |
|---|---|
| Manager 有子且全部终态 | Ready/review，清部分旧状态与日志，记录重试次数，回退父链 |
| Manager 有子但未全部终态 | Waiting/waiting，pending_revise=False，清通知，暂不消耗重试次数 |
| 无子 Manager，或 Worker 全量回退 | Waiting，phase=None，清旧运行状态/日志，登记延迟，再派发 |

artifact 删除失败只记录 warning。若 artifact_path 指向目录，单文件删除并不意味着整个目录被清掉。

### 13.5 延迟到期仍要过依赖

默认 retry_delay=1 秒，固定延迟，不是指数退避。

到期时先移除该 scheduled retry 记录：

- 节点仍 Waiting 且依赖满足 → Ready。
- 依赖仍不满足 → 继续 Waiting，之后交普通依赖扫描。
- 节点已非 Waiting → 不强行改变当前状态。

源码：[scheduler.py:1444-1686](../runtime/scheduler.py#L1444-L1686)。

---

## 14. 节点 Pause、Resume、Stop、Continue、Revise

### 14.1 对照表

| 操作 | 可接受状态 | 核心行为 | 启动 tick | 打开会话 pause 闸门 |
|---|---|---|---|---|
| Pause | Running | 请求停止当前执行，转 Paused | 否 | 否 |
| Resume | Paused | 依赖满足 Ready，否则 Waiting | 是 | 否 |
| Stop | 节点存在，底层允许转换 | 标 Stopped，不等于杀进程 | 否 | 否 |
| Continue | Failed/Stopped | 保留产物继续；类型分支见下文 | 是 | 否 |
| Worker Revise | Completed/Failed/Stopped/Paused | 写新增量，回退父链 | 是 | 否 |
| Manager Revise | 同上 | 写新增量，Waiting/expand | 不主动启动 | 否 |

源码：[runtime.py:1311-1638](../runtime/runtime.py#L1311-L1638)。

### 14.2 Pause 与 Stop 的根本区别

节点 Pause：

1. 校验 Running。
2. 调用 `scheduler.stop_node()` 请求终止当前缓存 AICU 的执行进程。
3. 转 Paused。
4. 保留 cache，供后续续接。
5. Future 仍被追踪到退出，不能只看状态就认定已完全静止。

节点 Stop：

- `Runtime.stop_node()` 主要是 Graph status=Stopped。
- 不调用同样的 scheduler.stop_node 杀进程路径。
- 不自动停止子节点。
- 不保证在途外部副作用已经停止。

因此不能把 Stop 当作更强的 Pause，或据此承诺旧任务绝不继续写文件。

### 14.3 Continue 保留什么、改什么

所有类型共同：清 error/started_at/finished_at，保留 artifact；返回原 node_id，不创建“续跑节点”。

Worker：

```text
Failed/Stopped → Waiting，phase=None
→ 回退父 Manager 链 → scheduler.start()
```

Manager 无子节点：

```text
Failed/Stopped → Waiting，phase=None
→ 后续重新 Expand
```

Manager 有子节点：

```text
Failed/Stopped → Ready，phase=review
→ 直接安排 Review
```

**手工 Continue 只看有没有子节点，不检查子节点是否全部终态。** 不能照抄正常 children-completed 回调条件。

Manager 还获得预算：

```python
max_iterations = max(原上限, 当前 iteration_count + 3)
```

不把 iteration_count 清零，保证额外机会但不缩减旧上限。

### 14.4 Revise 公共校验

- 状态必须 Completed/Failed/Stopped/Paused。
- 类型必须匹配。
- task Runtime 上限 5000 字符；即使 HTTP 模型允许更长，后端仍可拒绝。
- config 只接受该路径白名单字段，再与旧 config 合并。

Manager Revise：

- task 替换为增量任务，旧 task 进入 task_history。
- pending_context 替换为 original_task/incremental_task 等。
- Waiting/expand，pending_revise=True。
- iteration_count=0、activation_count=0、routing_history=[]。
- 清 error、时间、pending_routing；保留子节点与 artifact。
- 清自身 Manager 通知。
- **该方法不主动启动 tick，也不回退父链。** 已运行且未暂停的 tick 可拾取，否则需要恢复调度。

Worker Revise：

- 经共享复用函数生成 pending_context，包含 prior_artifacts。
- activation_count+1，iteration_count=0，Waiting。
- 清错误/时间，回退父链，启动 Scheduler。
- 会话 pause 闸门仍然保留。

### 14.5 父链怎样回退

从指定父节点逐级向上：

- 遇不存在或非 Manager → 结束。
- 非 Waiting 的父 Manager → Waiting/waiting，pending_revise=False。
- 已 Waiting 的父 Manager → 不重写 phase/pending_revise。
- 每层均清理完成通知，允许下一轮再次 Review。

不是删除祖先子树，也不是重建所有父节点。

### 14.6 HTTP 尾处理与 Runtime 不是一个事务

Continue/Revise 成功后，服务层可能把 failed/recovery_required/stopped 的会话 metadata 改为 paused，允许后续恢复。

此辅助函数只改 metadata，不额外调用 Scheduler.pause。因此不能看到响应里的 running/revising/paused 就推导当前 Future 状态或 pause 闸门状态。

源码：[session_server.py:939-958](../runtime/session_server.py#L939-L958)、[session_manager.py:510-528](../runtime/session_manager.py#L510-L528)。

---

## 15. 会话 Pause、Resume、Stop

### 15.1 Pause 请求首先写过渡状态

允许 mode=drain/immediate。

- 已 paused/recovery_required → 直接返回摘要。
- pausing 且同 mode → 幂等返回。
- pausing 但换 mode → 拒绝。
- stopped/completed → 拒绝 Pause。

接受后先写 sessions.json：

```text
lifecycle=pausing
desired=paused
pause_mode=请求模式
restart_safe=False
recovery_required=False
interrupted_node_ids=[]
```

再调用 Runtime.global_pause。

Runtime 调用失败 → metadata 改 recovery_required，保留需要人工处理的暂停意图并重抛。

### 15.2 Drain 的精确含义

1. 设置 Scheduler._paused=True，不再派发新工作。
2. 已有 Future 继续自然执行，仍回收完成结果。
3. 不统一把 Ready/Waiting 改成 Paused。
4. 活跃 Future 数归零后，触发完成暂停回调。
5. Runtime 先保存 graph，再让 SessionManager 写 paused/restart_safe=True。

如果没有 tracked Future，但图中存在孤立 Running：会将这些节点转 Paused，并交给恢复清单处理。

### 15.3 Immediate 的精确含义

1. 关闭派发闸门。
2. 收集图 Running ID 和 tracked Future ID 的并集。
3. 对 tracked 节点请求停止当前执行。
4. 仍 Running 的节点转 Paused。
5. **仍等待 tracked Future 真正退出**，不是立刻宣布暂停完成。
6. 完成后保存图与中断 ID 清单，再写 paused/restart_safe。

中断清单可能含当时已非 Running 但 Future 尚未退出的节点。Waiting/Ready 不会在此统一转 Paused。

外部文件写入、网络调用等不被回滚；恢复不是回到精确 CLI 指令断点。

源码：[runtime.py:1651-1708](../runtime/runtime.py#L1651-L1708)、[1748-1758](../runtime/runtime.py#L1748-L1758)、[session_manager.py:463-509](../runtime/session_manager.py#L463-L509)、[612-633](../runtime/session_manager.py#L612-L633)。

### 15.4 Resume 接受条件

- pausing → 拒绝，必须先完成 drain/immediate。
- paused/recovery_required/failed → 可尝试。
- completed/stopped → 图摘要需被判断为有可调度工作。
- running → 若已有正常活跃执行可直接返回；否则可能重开 dispatch，处理 idle/revise 错过启动的窗口。

注意第 2.3 节摘要的边界：摘要 Waiting 不一定真的有可执行节点。

### 15.5 Resume 的状态与写盘顺序

```text
metadata → resuming，desired=running
  → 保存 sessions.json
  → Runtime 校验没有未完成 pause，没有 active Future
  → 只恢复 interrupted_node_ids 中仍 Paused 的节点
  → 依赖满足则 Ready，否则 Waiting
  → 保存 graph.json
  → scheduler.resume() 打开闸门
  → scheduler.start()
  → metadata running，清恢复清单
  → 保存 sessions.json
```

未在 interrupted 清单里的手工 Paused 节点不会全部被顺手恢复；不存在/已非 Paused 的清单项跳过。

Resume 失败 → metadata recovery_required。已修改的图节点状态不做全事务回滚；保存失败发生在开闸之前。

### 15.6 Stop 后还能不能 Resume

当前不是“一旦 Stop 永远不能全局 Resume”。

Global Stop：

- 停 Scheduler；
- Running/Ready/Paused → Stopped；
- **Waiting 保持 Waiting**，其余终态保持；
- 会话 metadata stopped，清中断清单；
- 这里没有即时保存 graph，周期持久化仍可能继续。

之后：

- 若图里还有被摘要视为可调度的工作，会话 Resume 可以接受。
- Stopped 节点不在普通 global_resume 中自动复活，需要 Continue/Revise。
- 原本留下的 Waiting 则可能继续运行。
- 在暂停尚未完成时 Stop，Runtime 旧 pause_mode 未必清除，后续 Resume 仍可能被该前置条件拒绝。

### 15.7 Stop 不是“全部进程已停止”的证明

`Scheduler.stop()` 取消 Future、清追踪集合、线程池 shutdown(wait=False)。已经运行的 Future 不保证 cancel 成功，也没有在这条路径逐节点调用终止 CLI 的方法。

因此 metadata 中 restart_safe=True 是当前代码赋值，不能扩写成“外部进程、所有线程和所有文件写入都已完全退出”。

源码：[scheduler.py:223-240](../runtime/scheduler.py#L223-L240)、[runtime.py:1759-1802](../runtime/runtime.py#L1759-L1802)、[session_manager.py:596-611](../runtime/session_manager.py#L596-L611)。

---

## 16. 工作区、任务历史和共享产物

### 16.1 配置怎样进入节点

`_prepare_node_config()`：

- 可从 parent 继承未显式给出的连接信息/skills。
- 注入 `_workspace_root/_workspace_relpath/_shared_workspace/_output_path`。
- 应用 num_ccs 策略；固定资源策略可覆盖 Manager 规划的数量。
- 创建必要的共享输出分区。

默认结构通常为：

```text
workspace_root/
  shared/
    <output_path>/
  nodes/
    <node_id>/
```

事件目录应通过节点配置解析，不要把 shared 当成节点私有 workspace。

### 16.2 共享分区不是文件锁或硬隔离权限

- output_path 必须是合法相对路径，拒绝绝对路径/路径穿越。
- 多节点输出分区重叠目前只告警。
- Worker 提示词允许会话内读写共享区其他分区，强调先读当前内容、原子替换、任务分工。
- 没有因“专属分区”名称就自动建立文件级互斥锁。
- “先读再改”“不要访问外部路径”等 Agent 指令不能自动等同 OS 沙箱强制边界。

### 16.3 任务历史

task 是“当前任务”，Revise 后不再必然是最初任务。

`superseded_task_history()`：

- 新旧 task 相同 → 不追加，也不清已有历史。
- 改变 → 记录旧 task、时间、reason。
- reason 区分 manual/revise/retry/reuse/repair。
- 保留最多 20 条，单条 task 按长度上限截断。

因此追溯“是谁把任务改成失败修复指令”要看 reason=retry，而非误认为用户原始输入。

源码：[runtime.py:355-423](../runtime/runtime.py#L355-L423)、[worker.py:575-623](../executables/worker.py#L575-L623)、[models.py:90-129](../runtime/models.py#L90-L129)。

---

## 17. 持久化、进程恢复与关闭

### 17.1 Graph 什么时候保存

普通 mutation 增加 graph_version、设置 dirty；结构变化还更新 structure_version。

周期保存默认约每 2 秒：

```text
dirty → snapshot → graph.json.tmp → flush/fsync → rename 为 graph.json
```

- `_save_lock` 串行化图保存。
- 版本仍匹配才清 dirty，避免保存过程中新增变更被误认为已保存。
- 周期保存失败：记日志，下一轮继续。
- 显式 save 的异常可向上传播。
- pause/stop 不等于停止定时保存，shutdown 才停止它。
- 普通创建/编辑/Continue/Revise 不能一概宣称返回前已同步写入 graph.json。

### 17.2 加载顺序及坏数据

```text
主 graph.json 可解析且语义有效 → 使用主文件
否则尝试 graph.json.tmp
两份都不可用 → 返回 None 并记录错误
```

- 不因为 .tmp 更晚就优先使用它。
- 单个节点/边反序列化出错可能使整份候选快照失败，不是逐项跳过所有坏项。
- sessions.json 的索引读取没有同样的 .tmp 回退逻辑。
- events.json 是另一个独立文件，不能把图恢复自动当成事件完全恢复成功。

源码：[persistence.py:41-177](../runtime/persistence.py#L41-L177)、[196-236](../runtime/persistence.py#L196-L236)、[session_manager.py:863-890](../runtime/session_manager.py#L863-L890)。

### 17.3 Runtime 恢复图的顺序

1. load snapshot。
2. Planner 重放 nodes/edges，仍受 Graph 的端点存在检查。
3. 遍历 snapshot.crashed 中仍 Running 的节点：
   - safe_recovery=True → Paused，并加入 recovered_interrupted_node_ids；
   - 否则 → Failed。
4. 查找根 Manager。
5. 修正/推断 Manager phase。
6. 恢复持久化 graph_version，不把内部恢复计算成新的用户图版本。
7. 安全恢复先保存规整后的 graph。
8. 加载事件历史，但暂不恢复投递。
9. 如果 paused 参数为真，先 pause Scheduler，再 start。
10. 启动周期保存。

**没有恢复旧 Python Future、旧进程栈或原样 AICU 内存缓存。**

### 17.4 Manager 恢复的补偿分支

| 快照情况 | 恢复行为 |
|---|---|
| Waiting/waiting，子节点非空且全终态 | Ready/review，补偿崩溃前漏掉的完成回调 |
| phase 已有其他有效值 | 保留，不随意把 expand 改为 review |
| phase=None，有子节点 | 设置 review，但此分支本身不改 status |
| phase=None，无子节点 | 设置 expand，但此分支本身不改 status |

Waiting/waiting 的补偿依据 phase；即使残留 pending_revise 也可推进。相反，真正等待增量展开的 expand 不能被旧子节点终态抢先跳过。

源码：[runtime.py:219-354](../runtime/runtime.py#L219-L354)。

### 17.5 Session metadata 的恢复

历史 running/pausing/resuming：

- 非空图全 Completed → completed。
- 非空图全 Stopped → stopped。
- 其他 → recovery_required，desired=paused，合并旧中断清单和本次恢复清单。

其中全 Failed、混合终态、空图也属于“其他”，不能自动按摘要推测成 failed。

旧 paused/recovery_required：保留相应状态及暂停意图。

所有历史会话构造 Runtime 时都使用安全暂停 dispatch；即使恢复后有 Ready，也等待明确 Resume。

源码：[session_manager.py:777-815](../runtime/session_manager.py#L777-L815)、[863-890](../runtime/session_manager.py#L863-L890)。

### 17.6 多文件保存不是事务

graph.json、sessions.json、events.json 分别落盘，没有统一提交点。

pause/resume 的写入顺序降低风险，但不能保证任意崩溃点三个文件绝对一致。恢复正是靠安全降级、补偿 phase、重投 routed 等机制处理，而不是精确恢复每一次外部副作用。

### 17.7 关闭与删除会话

Runtime.shutdown：

1. `_shutdown=True`。
2. Scheduler.stop。
3. 释放外部输入等待。
4. 停周期保存。
5. 最后保存 graph，再尝试保存事件。

shutdown 不统一把所有节点改成 Paused/Stopped，也不能等同于完成一次安全 Drain。

进程信号入口第一次尝试优雅关闭，短间隔重复信号忽略；超时后的重复信号可强制退出。强制退出不能补齐未保存状态。

删除会话：

- 先从管理集合/索引移除；
- 关闭该 Runtime；
- 删除会话专属存储目录；
- 不额外删除会话目录之外的自定义 workspace/shared 目录。

源码：[runtime.py:1803-1846](../runtime/runtime.py#L1803-L1846)、[session_manager.py:691-707](../runtime/session_manager.py#L691-L707)、[main.py:49-146](../main.py#L49-L146)。

---

## 18. 完整场景推演与排查顺序

### 18.1 三条事件唤醒一个已完成 Worker

前提：B=Completed、未暂停、dependency 全部 Completed、无 planning barrier，**且图中还有其他活动工作使 `graph_idle=False`**。若整张图此刻已经空闲，第一条事件就会触发重激活，不必等三条（见 8.4、8.8）。

```text
A/C/D 分别发布三个不同协作事件
  → 格式验证、噪声过滤、event_id 去重
  → 每条分别经 Router 决策投 B
  → B/event_inbox 中累计三个相关 dict 文件
  → Scheduler 在 tick 中通过 scope 预筛
  → 数量达到 3
  → 合并 event_batch，activation_count+1
  → B: Completed → Waiting → Ready
  → 删除这三个 inbox 文件
  → 有槽位：B → Running
  → Worker 将 batch 转为任务上下文，清 pending_context
  → AICU 执行业务
  → 正常返回：B → Completed
  → 父 Manager 在满足条件后重新 Review
```

不是“三条事件合并成一次 Router 请求”；减少的是目标重激活次数，Router 仍可能对每条新事件分别调用。

### 18.2 满三条，但 B 的依赖 A 仍 Running

```text
达事件阈值
  → 批量转 pending_context
  → B 保持 Waiting
  → inbox 删除
  → 期间另一批事件到达并触发：追加到原 event_batch
  → A Completed 后，普通 dependency 门控放行 B
  → B 一次读取尚未执行的合并批次
```

事件可以已 consumed，而 B 还没真正执行。

### 18.3 Manager 收到不属于自己职责的事件

```text
M 原有子节点全部 Completed
  → 事件批量激活 M，expand + pending_revise
  → M 读取事件和可复用候选
  → LLM 返回 steps=[]、reason=不属于职责
  → 不重跑任何子节点
  → M Ready/review
  → Review 通过
  → M Completed
```

若子节点还在运行，则空计划后 M 回 Waiting/waiting；若本轮是 repair，空计划会失败。不存在统一“无关即在 Expand 直接 Completed”。

### 18.4 B 已 Running，只有一条新事件

- 立刻写 B 的持久 inbox 和 live 提醒。
- 不等 3 条就有机会由运行中 CC 读取。
- 不另起第二个 B Future。
- CC 是否读取由实际任务/提示词执行决定，不保证立即。
- 未读取便完成 → 文件还在，后续进入 Completed 批量等待路径。

### 18.5 节点一直 Waiting

依次检查：

1. Scheduler 是否 pause？
2. Waiting 是 Worker/未展开 Manager，还是 waiting-phase Manager？
3. dependency 中是否有 Failed/Stopped/Paused？
4. 是否在 scheduled retry 延迟中？
5. planning barrier 的 Manager 是否确实可展开？
6. 如果是事件：scope 是否被拒、图中是否仍有其他活动工作（否则空闲会立即触发）、数量是否不足、最老文件年龄是否足够？
7. 如果事件已进 pending_context：是否只是在等依赖，而不是消息丢失？
8. 如果是等待子节点：是否存在非终态子节点，或增量 expand 标记尚未消费？

### 18.6 事件存在，但节点没被唤醒

依次看：

```text
outbox 还在？ → JSON 是否可解析、有没有触发扫描
.invalid？ → objective 格式反馈
trace suppressed？ → 120秒窗口/相似度/限流
routed_to=[]？ → 无其他节点/历史重复/Router ignore/解析fail-closed
route_failed？ → Router ask 是否抛错，是否进入过恢复入口
routed？ → 目标丢失或部分投递失败
delivered？ → 查目标 inbox 和目标状态
Waiting/Completed inbox 未满？ → 图非空闲时等数量或最老年龄；图空闲时第一条即触发
Ready/Running？ → 不走批量桥，看执行器消费
Paused/Stopped/Failed？ → 不由事件自动复活
consumed？ → 查 pending_context、日志和业务状态，不能直接判业务已成功
```

### 18.7 事件导致反复重跑

检查：

- Producer 是否把正常完成通知也当协作事件发布？
- 是否每轮变换 objective 避开相似过滤？
- Router 是否错误选择职责不匹配节点？
- Worker 的 batch Prompt 是否缺少明确的“无关不改动”约定？
- Manager 是否原样重发旧 step 而不是空计划或增量 reuse？
- inbox 文件删除是否失败？
- 是否把 activation_count 当成了实际上不存在的硬预算？

以上是排查路径，不表示本次已经验证这些问题在某会话实际发生。

---

## 19. 源码与测试索引

### 19.1 从问题定位函数

| 问题 | 入口 |
|---|---|
| 事件何时发、是否必须发 | [Worker 发布 Prompt](../executables/worker.py#L624-L693) |
| 格式错误/近似重复/限流 | [AgentEventManager](../runtime/agent_events.py#L451-L610) |
| 为什么路由到该节点 | [Router Prompt 与解析](../runtime/router.py#L351-L541)，router_traces.jsonl |
| 为什么还没唤醒 | [批量桥](../runtime/scheduler.py#L1202-L1427) |
| scope 与职责判断差别 | [scope 预筛](../runtime/scheduler.py#L1202-L1280)、[Manager 事件上下文](../executables/manager.py#L1297-L1393) |
| 无关事件怎么结束 | [Manager 空计划](../executables/manager.py#L213-L340)、[Worker routing context](../executables/worker.py#L771-L844) |
| 为什么又开始 Review | [子节点完成检测](../runtime/scheduler.py#L917-L983)、[Continue](../runtime/runtime.py#L1367-L1449) |
| 为什么重新建了节点 | [revision/reuse 校验](../runtime/runtime.py#L1031-L1291) |
| 为什么自动重试 | [Scheduler 重试](../runtime/scheduler.py#L1444-L1603) |
| 为什么暂停后创建也不运行 | [start/resume 的差别](../runtime/scheduler.py#L206-L252) |
| 为什么 Stop 后还能恢复 | [会话 Resume 条件](../runtime/session_manager.py#L529-L595) |
| 重启后为什么 recovery_required | [会话恢复](../runtime/session_manager.py#L863-L890) |

### 19.2 后端测试阅读入口

以下是对应机制的测试文件索引，不代表本次执行通过：

- [test_event_reactivation_bridge.py](../tests/test_event_reactivation_bridge.py)：事件到重激活、批量触发。
- [test_event_queue_unified.py](../tests/test_event_queue_unified.py)：队列契约与消费。
- [test_agent_events.py](../tests/test_agent_events.py)：摄取、投递、持久化。
- [test_router_lifecycle.py](../tests/test_router_lifecycle.py)：Router 生命周期。
- [test_routing_reactivation.py](../tests/test_routing_reactivation.py)：重激活上下文。
- [test_dependency_gate.py](../tests/test_dependency_gate.py)：旁路激活仍遵循依赖。
- [test_scheduler.py](../tests/test_scheduler.py)、[test_scheduler_manager.py](../tests/test_scheduler_manager.py)：派发与父子推进。
- [test_scheduler_retry.py](../tests/test_scheduler_retry.py)、[test_scheduler_concurrency.py](../tests/test_scheduler_concurrency.py)：重试和并发。
- [test_manager.py](../tests/test_manager.py)、[test_manager_collaboration.py](../tests/test_manager_collaboration.py)：规划、评审、协作。
- [test_worker.py](../tests/test_worker.py)、[test_root_revise.py](../tests/test_root_revise.py)：Worker 与增量续跑。
- [test_session_server.py](../tests/test_session_server.py)：会话控制接口与恢复条件。
- [test_persistence_migration.py](../tests/test_persistence_migration.py)：持久化兼容。
- [test_concurrency_robustness.py](../tests/test_concurrency_robustness.py)、[test_main_shutdown.py](../tests/test_main_shutdown.py)：并发及退出。

### 19.3 最后记住这十二条

1. 创建节点先 Waiting；启动 tick 不等于打开会话暂停闸门。
2. parent 管组织与评审，dependency 管前置条件，event 管协作信息。
3. Router 只给目标 JSON；事件框架写 inbox；Scheduler 才决定事件何时激活节点。
4. 默认每目标累计 3 条或最老文件等待 1800 秒，触发一次性批处理；若整张图已空闲，第一条相关事件就立即触发。
5. 该阈值只约束非运行节点的事件重激活，不阻止正常任务启动或 Running 消费。
6. scope 预筛不是职责判断，且拒绝事件不改变节点原状态。
7. Manager 无行动不直接 Completed，而是等待子节点或进入 Review。
8. Worker 的职责判断是 Prompt 约定，不存在统一强制 decline→Completed 分支。
9. inbox 删除/consumed 不等于业务成功，pending_context 与业务完成是不同阶段。
10. Continue、Revise、自动重试、事件激活有不同字段变化和预算，不能混写。
11. Stop 不等于所有进程已退出，也不是永久禁止全局 Resume。
12. 持久化恢复是图与队列的安全重建，不是进程级精确断点恢复，也没有端到端 exactly-once 保证。
