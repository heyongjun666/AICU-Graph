# 执行会话全生命周期与行为说明

> 本文档基于 `final_solution` 当前磁盘源码整理，重点说明一次执行会话从进程启动、创建会话、创建节点、自动调度、执行、协作、暂停/恢复/重试，到持久化恢复和进程关闭的完整行为。
>
> 本文只读分析代码生成，未修改业务实现。源码行号可能随代码继续演进而变化；关键模块已使用相对文件链接标注。

---

## 1. 先记住的几个核心结论

### 1.1 当前没有独立的“开始调度”按钮

当前前端没有单独的 `start`、`dispatch` 或 `run` 按钮。

用户在新增节点弹窗中提交节点后，流程是：

1. 前端调用创建节点接口；
2. 后端创建一个 `Waiting` 节点；
3. `Runtime.create_manual_node()` 调用 `_ensure_scheduler_running()`；
4. Scheduler 的 tick 线程自动扫描图；
5. 节点满足依赖和调度条件后，自动从 `Waiting` 进入 `Ready`，再进入 `Running`。

因此，本项目的核心语义是：

> 创建节点就是把任务加入执行图；Scheduler 会自动接管，而不是等待用户再次点击“开始”。

顶部按钮主要用于会话级 `pause / resume / stop`，节点详情面板主要用于节点级 `pause / resume / stop / continue / revise`。

### 1.2 会话状态和节点状态是两套状态

节点状态：

```text
Waiting / Ready / Running / Paused / Completed / Failed / Stopped
```

会话生命周期状态：

```text
running / pausing / paused / resuming / recovery_required
stopped / completed / failed
```

因此可能出现：

```text
会话仍然是 running
某个节点已经 Failed
另一个节点仍然 Waiting
```

此时会话不会立即被判定为整体失败。

### 1.3 新建会话默认是空白图

新会话不会自动创建根 Manager，也不会自动执行传入的 task。前端创建会话时会传入空 task，用户需要之后手动创建 Manager 或 Worker 节点。

相关代码：

- [SessionStore.ts:128-141](../frontend/src/stores/SessionStore.ts#L128-L141)
- [session_server.py:632-648](../runtime/session_server.py#L632-L648)
- [session_manager.py:195-258](../runtime/session_manager.py#L195-L258)

### 1.4 手动创建节点的初始状态是 Waiting

手动节点不是 `Paused`，也不是 `Ready`，而是：

```python
NodeStatus.WAITING
```

Scheduler 后续负责把它推进到 `Ready`。

### 1.5 Agent Events 面板不是实时 SSE 面板

Graph 节点、边、状态、日志追加和会话生命周期使用 SSE 实时更新；Agent 协作事件通过 REST 按需加载，不会因为 `events.json` 新增事件而实时刷新。

### 1.6 当前没有统一的 Future 墙钟超时

Scheduler 没有一个统一的“Future 运行超过 N 分钟就杀掉”的计时器。当前 timeout 主要来源于 AICU/CLI/CC activity，随后进入普通失败和重试链。

---

## 2. 总体组件关系

一次多会话执行服务的主要对象关系如下：

```text
main.py
  └── SessionManager
        └── SessionHandle（每个会话一份）
              ├── SessionMetadata
              ├── Runtime
              │     ├── Graph
              │     ├── Scheduler
              │     ├── EventBus
              │     ├── Persistence
              │     ├── AgentEventManager
              │     └── Router
              └── SSEManager
```

职责划分：

| 组件 | 主要职责 |
|---|---|
| `SessionManager` | 管理多个隔离会话、会话元数据、会话级暂停/恢复/停止、恢复历史会话 |
| `Runtime` | 单会话执行中枢，连接 Graph、Scheduler、执行器、事件、工作区和持久化 |
| `Graph` | 保存 Node 和 Edge，并维护 parent/dependency 索引 |
| `Scheduler` | 周期性扫描 Graph，进行依赖门控、并发控制、Future 回收、重试和事件重激活 |
| `Manager` | Expand、Review、Repair、Revision，负责规划和验收 |
| `Worker` | 调用 AICU/CLI 执行具体任务，写入 workspace 和产物 |
| `EventBus` | Runtime 内部事件发布总线 |
| `SSEManager` | EventBus 到浏览器 SSE 的缓冲、过滤、续拉和快照兜底 |
| `Persistence` | 保存/加载 Graph 快照 |
| `AgentEventManager` | Agent 事件 outbox、路由、投递、重放、消费状态维护 |
| `Router` | 基于事件、producer 和组织图决定事件接收节点 |

---

## 3. 进程启动

入口在 [main.py:77-142](../main.py#L77-L142)。

### 3.1 注册执行器

启动时创建 `ExecutableRegistry`，注册：

```text
manager → Manager
worker  → Worker
```

代码：[main.py:86-91](../main.py#L86-L91)

Scheduler 执行节点时，会根据节点的 `exec_type` 从 Registry 创建对应执行器。

### 3.2 创建 SessionManager

代码：[main.py:93-98](../main.py#L93-L98)

SessionManager 初始化时会：

1. 创建 `runtime_sessions` 目录；
2. 读取全局配置；
3. 初始化 API Key/credential store；
4. 读取 `sessions.json`；
5. 恢复历史会话；
6. 为每个历史会话创建 Runtime 和 SSEManager。

### 3.3 创建 FastAPI 应用并启动 Uvicorn

```text
create_session_app(session_manager)
uvicorn.run(app, host, port)
```

默认监听：

```text
HOST=0.0.0.0
PORT=8080
```

代码：[main.py:137-142](../main.py#L137-L142)

### 3.4 信号关闭

SIGINT/SIGTERM 的行为：

1. 第一次信号执行优雅关闭；
2. `SessionManager.shutdown()` 停止各会话 SSEManager 并关闭 Runtime；
3. 关闭完成后使用 `os._exit(0)`，避免线程池 join 阻塞进程退出；
4. 优雅关闭开始后的 3 秒内再次收到信号，直接忽略；
5. 超过 3 秒仍未完成，再次收到信号则 `os._exit(130)` 强制退出。

代码：[main.py:49-132](../main.py#L49-L132)

---

## 4. 会话创建

### 4.1 前端加载已有会话

App 首次挂载时：

1. 从 `localStorage` 读取 active session id；
2. 请求 `GET /v1/sessions`；
3. 如果原会话仍存在，继续选中；
4. 否则选择第一个会话；
5. 如果没有会话，显示创建页面。

相关代码：

- [App.tsx:67-82](../frontend/src/App.tsx#L67-L82)
- [SessionStore.ts:25-75](../frontend/src/stores/SessionStore.ts#L25-L75)

切换会话时会先清空旧 Graph、旧选中节点和相关视图状态，防止出现“新会话 ID 配旧节点 ID”的中间状态。

### 4.2 创建会话请求

```http
POST /v1/sessions
```

默认请求类似：

```json
{
  "task": "",
  "title": "用户填写的标题"
}
```

后端路由：[session_server.py:632-648](../runtime/session_server.py#L632-L648)

### 4.3 SessionManager 创建过程

[session_manager.py:195-258](../runtime/session_manager.py#L195-L258) 会执行：

1. 生成 UUID；
2. 生成标题；
3. 过滤和合并会话配置；
4. 继承全局 CLI、model、API 地址、语言等配置；
5. 计算 `workspace_root`；
6. 校验显式 `shared_workspace`；
7. 创建显式外部 shared workspace；
8. 调用 `_build_handle()` 构造 Runtime；
9. 启动 Scheduler；
10. 启动定时持久化；
11. 启动 SSEManager；
12. 保存 `sessions.json`；
13. 返回会话摘要。

### 4.4 默认目录结构

```text
runtime_sessions/
  sessions.json
  config.json
  api_keys.json
  <session_id>/
    graph.json
    graph.json.tmp
    workspace/
      shared/
      nodes/
        <node_id>/
```

会话 Runtime 和工作区初始化在 [session_manager.py:757-823](../runtime/session_manager.py#L757-L823)。

### 4.5 会话恢复时的安全降级

如果持久化快照中某节点当时是 `Running`：

- `safe_recovery=True`：恢复为 `Paused`，记录到 `recovered_interrupted_node_ids`；
- 非安全恢复路径：降级为 `Failed`；
- 历史会话由 SessionManager 恢复时使用安全恢复，之后需要用户明确 resume。

Runtime 恢复代码：[runtime.py:227-350](../runtime/runtime.py#L227-L350)

---

## 5. 打开会话后的前端初始化

当 active session 确定后，App 会：

1. 重置 GraphStore；
2. 加载 Agent Events 历史；
3. SSEProvider 请求一次 Graph 快照；
4. 建立 EventSource；
5. 接收服务端快照或增量事件。

代码：

- [App.tsx:67-109](../frontend/src/App.tsx#L67-L109)
- [SSEProvider.tsx:58-134](../frontend/src/providers/SSEProvider.tsx#L58-L134)

---

## 6. 创建节点的完整流程

### 6.1 打开新增节点弹窗

用户点击新增节点后，前端会加载：

- 角色列表；
- Skill 列表；
- 可用父节点；
- 可用依赖节点。

父节点/依赖候选接口：

```http
GET /v1/sessions/{session_id}/graph/link-candidates
```

App 处理在 [App.tsx:84-109](../frontend/src/App.tsx#L84-L109)。

### 6.2 节点请求字段

```text
task
role
exec_type: manager / worker
parent_id
dependencies
config
```

后端模型：[session_server.py:161-168](../runtime/session_server.py#L161-L168)

### 6.3 前端提交前检查

前端会阻止：

- task 为空；
- role/skill 仍在加载；
- link candidates 加载失败；
- 选择了不在候选集合中的 parent/dependency；
- 当前提交按钮处于 busy 状态。

### 6.4 创建节点 HTTP 链路

```text
App.addNewNode()
  → api.createSessionNode()
  → POST /v1/sessions/{session_id}/nodes
  → session_server.create_node()
  → runtime.create_manual_node()
```

前端代码：[App.tsx:127-155](../frontend/src/App.tsx#L127-L155)

API 封装：[api.ts:455-471](../frontend/src/utils/api.ts#L455-L471)

后端路由：[session_server.py:799-817](../runtime/session_server.py#L799-L817)

### 6.5 后端创建节点前的校验

`Runtime.create_manual_node()` 在 [runtime.py:2690-2743](../runtime/runtime.py#L2690-L2743)。

创建前检查：

#### 角色和执行类型

如果 `exec_type=manager` 但 role 属于 Worker，或者反过来，会被拒绝。默认 role 和没有注入角色目录的兼容路径仍然保留。

#### parent

如果指定 `parent_id`：

- 父节点必须存在；
- 父节点必须是 Manager；
- 不能自己成为自己的父节点；
- 不能形成 parent 环。

#### dependencies

每个依赖节点必须已经存在。

依赖语义是：

```text
依赖节点 → 当前新节点
```

只有依赖节点 Completed，当前节点才会通过依赖门控。

#### shared config

节点的共享工作区配置会进行路径和冲突校验：

- `output_path` 不能是绝对路径；
- 不能使用 `..` 穿越；
- shared workspace 必须处于允许范围；
- 多节点使用同一输出分区时目前只告警，不再硬拒绝。

### 6.6 节点实际创建和回滚

创建过程：

1. 生成节点 UUID；
2. 构造 `Node(status=Waiting)`；
3. 写入 Graph；
4. 如果有 parent，创建 `PARENT` edge；
5. 如果有 dependencies，创建 `DEPENDENCY` edges；
6. 如果建边过程中发生异常，删除刚创建的节点回滚；
7. 调用 `_ensure_scheduler_running()`。

创建成功后，Graph mutation bridge 会发布：

```text
node_added
edge_added
```

### 6.7 前端创建成功后的刷新

因为 parent/dependency edge 的 UUID 由后端随机生成，前端无法只靠本地数据补全结构。创建成功后前端会：

1. 再请求完整 Graph 快照；
2. 用快照替换/合并当前 GraphStore；
3. 如果快照失败，降级为只插入返回节点；
4. 最后刷新会话摘要。

代码：[App.tsx:141-153](../frontend/src/App.tsx#L141-L153)

---

## 7. 节点编辑、删除和连边

### 7.1 编辑节点

接口：

```http
PATCH /v1/sessions/{session_id}/nodes/{node_id}
```

只有以下状态允许编辑：

```text
Waiting
Paused
```

可编辑字段：

```text
task
role
exec_type
config
```

代码：[runtime.py:2745-2781](../runtime/runtime.py#L2745-L2781)

修改 task 时会写入 `task_history`：

```text
旧 task → task_history
新 task → node.task
```

如果新旧 task 相同，不会重复记录历史。

有子节点的 Manager 不能改成 Worker，否则父节点无法继续承担 Manager 的评审职责。

### 7.2 删除节点

接口：[session_server.py:839-853](../runtime/session_server.py#L839-L853)

规则：

- 只允许删除叶节点；
- Manager 有子节点时拒绝；
- 不支持级联删除；
- Running 节点必须先停止；
- 节点仍被其他节点依赖时不能删除；
- 删除节点会清理关联边并发布 SSE。

Runtime 逻辑：[runtime.py:2854-2870](../runtime/runtime.py#L2854-L2870)

### 7.3 创建依赖边

接口：

```http
POST /v1/sessions/{session_id}/edges
```

Runtime 会检查：

- 不能自依赖；
- 不能形成有向环；
- 不能重复添加相同 dependency。

`Graph.add_edge()` 进一步检查 from/to 节点存在。

代码：[runtime.py:2825-2843](../runtime/runtime.py#L2825-L2843)

删除 dependency 时：

- 不存在返回错误；
- parent edge 不能通过此接口删除；
- 删除后会确保 Scheduler 重新运行。

---

## 8. Scheduler 自动调度流程

Scheduler tick 主循环在 [scheduler.py:337-427](../runtime/scheduler.py#L337-L427)，默认以约 0.5 秒周期运行。

一次 tick 的主要顺序：

```text
1. 恢复待处理 Agent events
2. 统计 Waiting / Ready / Running
3. 处理到期 scheduled retry
4. 回收已完成 Future
5. 扫描节点事件 outbox
6. 处理事件重激活
7. Waiting → Ready
8. Ready → Running
9. 发布 scheduler_tick 内部统计
```

如果没有活跃执行、就绪节点、可推进等待节点、scheduled retry 或待处理事件，Scheduler 可以自动停止 tick 线程。新建节点或修改图时，Runtime 会重新启动它。

### 8.1 Waiting → Ready

依赖门控位于 [scheduler.py:465-522](../runtime/scheduler.py#L465-L522)。

规则：

```text
所有 dependency 节点 status == Completed
```

才允许：

```text
Waiting → Ready
```

如果依赖节点 Failed 或 Stopped：

- 不会级联把当前节点标记为 Failed；
- 当前节点继续 Waiting；
- 需要用户 continue/revise 上游，或者调整依赖图。

### 8.2 Manager planning barrier

如果存在尚未展开的 Manager，Scheduler 会让普通 Worker 等待，避免 Worker 在 Manager 规划完成前抢先执行。

但依赖已满足的 Manager不会被同一屏障阻塞，否则容易出现 Manager/Worker 互相等待造成死锁。

### 8.3 Ready 节点选择和并发

Ready 选择在 [scheduler.py:562-626](../runtime/scheduler.py#L562-L626)。

优先级大致是：

1. waiting-phase Manager 的直接子节点；
2. 依赖深度更深的节点；
3. 其他 Ready 节点。

并发槽位：

```text
可用槽位 = max_concurrency - 当前有效运行数
```

如果槽位已满，Ready 节点不会失败，只是等待下一次 tick。

注意两个配置的区别：

| 配置 | 含义 |
|---|---|
| `max_concurrency` | 一个会话同时运行多少个节点，系统级配置 |
| `num_ccs` | 单个节点内部使用多少个 CC，节点/策略级配置 |

### 8.4 Ready → Running 和 Future

执行逻辑在 [scheduler.py:676-769](../runtime/scheduler.py#L676-L769)：

1. Ready → Running；
2. 构造 `ExecutableContext`；
3. Registry 创建 Manager 或 Worker；
4. 提交 ThreadPoolExecutor；
5. 保存 Future；
6. 下一次 tick 检查完成结果。

Future 结果处理：

- 正常返回：使用返回的 NodeStatus；
- `CancelledError`：映射为 Stopped；
- 未捕获异常：映射为 Failed；
- 如果节点已经被用户暂停或停止，旧 Future 的结果不能覆盖当前状态。

这个 stale Future 防护在 [scheduler.py:775-835](../runtime/scheduler.py#L775-L835)。

---

## 9. Worker 执行生命周期

Worker 负责具体任务执行，主要实现位于 [worker.py](../executables/worker.py)。

### 9.1 启动前构造上下文

Worker 会根据以下信息构造 AICU/CLI 执行上下文：

- 节点 task；
- role；
- exec_type；
- 节点私有 workspace；
- session shared workspace；
- `output_path` 分区；
- CLI 和 model；
- API Key 环境变量；
- Skill；
- MCP；
- `num_ccs`。

### 9.2 会话复用

同一节点再次激活时，Worker 尽量复用原有 AICU cache，并通过 resume 继续原会话。

以下情况可能丢弃旧 cache 并重新建立：

- role 变化；
- AICU/CLI 配置变化；
- workspace 变化；
- 节点上下文不再兼容。

### 9.3 Worker 成功

成功路径通常包括：

1. 调用 AICU/CLI；
2. 写入节点 workspace 或 shared output；
3. 记录 CC activity；
4. 保存 artifact；
5. 写入完成日志；
6. 节点变为 Completed；
7. flush 节点产生的 Agent events；
8. Scheduler 通知父 Manager 检查子节点是否全部终态。

### 9.4 Worker 失败

失败路径通常包括：

1. 节点变为 Failed；
2. 设置 `ErrorObject`；
3. 追加 ERROR 日志；
4. 记录具体异常；
5. 进入 Scheduler 自动重试判断；
6. flush 已产生的 Agent events。

CC activity 中的 `timeout` 会被映射成失败事件，但当前不是 Scheduler 统一 Future 墙钟超时。

---

## 10. Manager 生命周期：Expand → Waiting → Review

Manager 不是一次运行结束，而是两阶段执行器。

### 10.1 Expand

Manager 先调用 AICU 生成 `ExecutionPlan`。

计划中的 Step 主要包含：

```text
step_id
exec_type
role
task
task_summary
config
depends_on
reuse_step_ref
```

执行逻辑在 [manager.py:219-339](../executables/manager.py#L219-L339)。

#### 无子节点

走全量计划通道：

```text
Manager → ExecutionPlan → Planner → 创建子节点
```

Planner 会先校验，之后：

1. 为 Step 分配 UUID；
2. 创建 Waiting 子节点；
3. 创建 parent edges；
4. 创建 dependency edges；
5. 批量注入 Graph。

Planner 代码：[planner.py:62-161](../runtime/planner.py#L62-L161)

#### 已有子节点

不能再次全量重建，必须走 revision 通道：

- 通过 `reuse_step_ref` 复用已有节点；
- 只创建新增 Step 对应的节点；
- 重建激活节点之间的 dependency；
- 不删除未被新计划引用的旧节点；
- 保留已有 workspace 和产物。

代码：[planner.py:163-256](../runtime/planner.py#L163-L256)

#### Expand 完成

正常情况下：

```text
Manager.status = Waiting
Manager.manager_phase = waiting
```

表示 Manager 已规划完成，等待子节点。

空计划有特殊含义：

- 普通增量/事件场景：表示 Manager 判断本轮无需行动；
- repair 场景：空计划可能被视为失败，因为修复场景本应产生修复动作。

### 10.2 子节点全部终态

当 Manager 的子节点全部为：

```text
Completed / Failed / Stopped
```

Scheduler 通知 Runtime 将：

```text
Manager: Waiting → Ready
phase: waiting → review
```

然后 Scheduler 再次执行 Manager。

### 10.3 Review

Review 会收集：

- 子节点状态；
- 子节点日志；
- 子节点产物；
- 子节点职责；
- 协作台账；
- 迭代上下文。

代码：[manager.py:345-620](../executables/manager.py#L345-L620)

#### 全部通过

```text
Running(review) → Completed
```

并保存评审结果和闭环台账。

#### 存在失败项且仍有迭代预算

1. 提取失败子节点；
2. 调用 `submit_fix_plan`；
3. 通过的子节点保持不变；
4. 失败子节点原地续接，或创建同类型替代节点；
5. Manager 回到 `Waiting/waiting`；
6. 修复节点完成后重新 Review。

#### 达到 `max_iterations`

Manager 进入 Failed，并写入最大迭代错误。

### 10.4 Manager 自身 AICU 重试

Manager AICU 调用默认 `retries=2`，即最多三次调用：

```text
第一次调用 + 两次重试
```

异常后立即重试，没有指数退避和 sleep。

---

## 11. 状态机

状态定义在 [models.py:22-30](../runtime/models.py#L22-L30)。

### 11.1 普通转换

状态机在 [state_machine.py:39-61](../runtime/state_machine.py#L39-L61)。

```text
Waiting   → Ready / Stopped
Ready     → Running / Stopped
Running   → Completed / Failed / Stopped / Paused
Paused    → Running
Failed    → Waiting / Ready
Stopped   → Waiting / Ready
Completed → Waiting / Ready
```

以下直达转换不允许：

```text
Waiting → Running
Failed  → Running
Stopped → Running
```

必须先经过 `Ready`。

### 11.2 Manager 特殊转换

```text
Running(expand)
  → Waiting(waiting)
  → Ready(review)
  → Running(review)
  → Completed
```

不允许：

```text
Running(expand) → Completed
```

Review 不通过时：

```text
Running(review) → Running(expand)
```

---

## 12. 节点级控制

### 12.1 Pause 节点

接口：

```http
POST /v1/sessions/{session_id}/nodes/{node_id}/pause
```

仅允许当前节点是 Running：

```text
Running
  → 停止当前 Scheduler execution
  → Paused
```

会保留 AICU cache，方便后续 resume。

代码：[runtime.py:1332-1357](../runtime/runtime.py#L1332-L1357)

### 12.2 Resume 节点

接口：[session_server.py:864-871](../runtime/session_server.py#L864-L871)

仅允许 Paused：

- dependency 未满足 → Waiting；
- dependency 已满足 → Ready；
- 重新确保 Scheduler 运行。

代码：[runtime.py:1359-1386](../runtime/runtime.py#L1359-L1386)

### 12.3 Stop 节点

Stop 更接近将节点标记为：

```text
Stopped
```

它不是普通的“可恢复暂停点”。需要继续时，使用 Continue 或 Revise。

### 12.4 Continue 节点

接口：

```http
POST /v1/sessions/{session_id}/nodes/{node_id}/continue
```

只允许 Failed/Stopped。

#### Worker Continue

```text
Failed/Stopped
  → 清理 error 和时间字段
  → 保留产物
  → Waiting
  → 重新调度
```

#### Manager Continue

有子节点：

```text
Failed/Stopped
  → Ready
  → phase=review
  → 重新评审已有子节点
```

没有子节点：

```text
Failed/Stopped
  → Waiting
  → 重新 Expand
```

代码：[runtime.py:1388-1469](../runtime/runtime.py#L1388-L1469)

### 12.5 Revise 节点

Revise 可以作用于：

```text
Completed / Failed / Stopped / Paused
```

#### Manager Revise

- 保留已有子节点；
- 保留产物；
- 保留已有 Agent 会话；
- 设置 `manager_phase=expand`；
- 设置 `pending_revise=True`；
- 重新进入 Waiting 并由 Scheduler 调度；
- Planner 通过 `reuse_step_ref` 复用旧节点。

#### Worker Revise

尽量复用原 Worker 会话和产物；如果 role、配置或 workspace 变化导致上下文不兼容，则删除旧 cache 并重新建立。

代码：

- [runtime.py:1507-1572](../runtime/runtime.py#L1507-L1572)
- [runtime.py:1574-1633](../runtime/runtime.py#L1574-L1633)
- [runtime.py:1635-1657](../runtime/runtime.py#L1635-L1657)

---

## 13. 自动重试

自动重试位于 [scheduler.py:1408-1645](../runtime/scheduler.py#L1408-L1645)。

### 13.1 Worker 失败

Worker 优先走增量续跑：

- 尽量保留原 AICU session；
- 保留产物；
- 构造增量任务；
- 进入固定 retry delay；
- 到期后重新 Ready。

### 13.2 Manager 失败且子节点全终态

不重新 Expand，而是：

```text
Failed → Ready
phase = review
```

只重做 Review。

### 13.3 Manager 失败但仍有未终态子节点

回到：

```text
Waiting / waiting
```

不额外消耗迭代次数，等待子节点完成。

### 13.4 普通 Worker 或无子 Manager

一般会：

1. 清理 error；
2. 清理产物/运行态信息；
3. 回到 Waiting；
4. 设置 `ready_time = now + retry_delay`；
5. 到期后 Waiting → Ready；
6. 再次执行。

当前使用固定延迟，不是指数退避。

### 13.5 达到重试上限

达到 `max_iterations` 后不再自动重试，节点保持 Failed。

---

## 14. 会话级 Pause / Resume / Stop

### 14.1 Pause 会话

接口：

```http
POST /v1/sessions/{session_id}/graph/pause
```

参数：

```text
mode=drain
mode=immediate
```

SessionManager 逻辑：[session_manager.py:512-557](../runtime/session_manager.py#L512-L557)

#### Drain

```text
running
  → pausing
  → 不再派发新节点
  → 已运行 Future 自然结束
  → paused
```

特点：

- 不杀正在运行的 CLI；
- 等当前执行边界完成；
- 相对安全；
- 不容易重复文件写入。

#### Immediate

```text
running
  → pausing
  → 停止活跃执行
  → Running 节点转 Paused
  → 记录 interrupted_node_ids
  → paused
```

可能产生：

- 文件只写了一半；
- 外部副作用已经发生；
- 恢复时任务重复执行；
- 不能保证恢复到 CLI 内部精确断点。

同模式重复 pause 是幂等的；已经以一种模式 pausing 时，不能同时切换另一种模式。

### 14.2 Resume 会话

接口：

```http
POST /v1/sessions/{session_id}/graph/resume
```

主要流程：

```text
paused / recovery_required
  → resuming
  → 仅处理本次记录的 interrupted_node_ids
  → 重新检查依赖
  → Waiting 或 Ready
  → running
```

恢复前会先持久化，之后才重新启动 Scheduler。

如果恢复过程失败：

```text
resuming → recovery_required
```

### 14.3 Stop 会话

接口：

```http
POST /v1/sessions/{session_id}/graph/stop
```

行为：

- 停止 Scheduler；
- Running、Ready、Paused 节点转 Stopped；
- 会话生命周期转 stopped；
- 不保留像 Pause 那样的可恢复 interrupted 清单；
- 没有对应的全局 resume。

相关代码：[runtime.py:1675-1803](../runtime/runtime.py#L1675-L1803)

---

## 15. Agent 协作事件

Agent 事件和 Graph/SSE 事件不是同一种东西。

### 15.1 事件存储位置

实际运行会话的事件日志通常在：

```text
runtime_sessions/<session_id>/workspace/events.json
```

节点私有目录中还会看到：

```text
event_inbox/
events_inbox/
```

含义：

| 目录 | 含义 |
|---|---|
| `event_inbox/` | 持久化待消费队列，供 Scheduler/CC 消费 |
| `events_inbox/` | 仅供 Running 节点接收实时信号 |

### 15.2 Agent event 生命周期

```text
节点 outbox
  → Runtime 摄取
  → Router 路由
  → 写入目标 event_inbox
  → 目标消费
  → consumed
```

事件状态通常是：

```text
routed → delivered → consumed
```

如果 Router 失败：

```text
route_failed
```

后续允许重新 Router。

如果事件没有目标：

```text
routed_to = []
status = delivered
```

这是合法状态，表示已经完成路由/投递判定但没有接收者，不等于事件丢失。

### 15.3 Router 行为

Router 根据：

- 事件内容；
- producer 节点；
- 当前组织图；
- 节点职责；
- 节点层级；
- 节点状态；

调用 AICU 选择目标。

路由解析失败时会重试一次；仍然失败时 fail-closed，不随意猜测目标。

已写入 `routed_to` 的历史事件重启后不会重新调用 LLM，而是按已记录目标确定性重投。

### 15.4 Scheduler 如何消费 Agent events

Scheduler 只做机械处理，不解释 objective 的业务语义：

1. 按节点读取 `event_inbox`；
2. 对 current/subtree scope 做确定性预筛；
3. 其他 scope 放行给目标节点；
4. 达到数量阈值或超过最大等待年龄后批量消费；
5. 合并成 `event_batch`；
6. 写入 `pending_context`；
7. 重新激活目标节点。

Failed、Paused、Stopped 节点不会被 Agent event 自动复活。

---

## 16. 前端实时通信

前端实际使用三类通信方式。

### 16.1 Graph 和状态：SSE

入口：[SSEProvider.tsx:98-134](../frontend/src/providers/SSEProvider.tsx#L98-L134)

主要事件：

```text
node_added
node_updated
node_removed
edge_added
edge_removed
node_status_changed
log_appended
session_state_changed
graph_updated
```

`scheduler_tick` 仅用于内部统计，不逐条推送给浏览器。

### 16.2 SSE 首连、断线和快照兜底

首连时前端会先请求 REST Graph 快照，再建立 EventSource。

断线重连时携带已消费的 `sequence_id`：

```text
GET /v1/sessions/{session_id}/graph/events/since/{sequence_id}
```

服务端有三种情况：

1. 事件仍在环形缓冲区：补发增量；
2. 请求水位太旧，事件已经丢失：发送完整 `graph_updated` 快照；
3. 补发结束后继续进入实时流。

SSE 服务端内部每秒检查一次新事件，每 15 秒发送 keepalive。

服务端代码：[server.py:463-592](../runtime/server.py#L463-L592)

### 16.3 节点日志：REST + SSE + 局部轮询

NodeInspector 选中节点时会请求：

```text
GET /nodes/{node_id}
GET /nodes/{node_id}/logs
```

当同时满足：

```text
当前处于 Logs tab
节点状态为 Running
```

前端每 1500ms 轮询一次日志。

因此日志有三种来源：

```text
历史日志：REST
实时追加：SSE log_appended
运行中补偿：REST 轮询
```

前端会做日志去重。

### 16.4 Agent Events：按需 REST

Agent Events 不接入 SSE：

- 切换 active session 时加载；
- 切换到 Events tab 时加载；
- 打开 EventsDialog 时加载；
- 用户手动刷新时重新加载；
- 不会自动持续轮询。

因此 `events.json` 发生变化后，Agent Events 面板可能暂时还是旧内容。

### 16.5 Terminal：独立 WebSocket

终端使用：

```text
WebSocket /v1/sessions/{session_id}/terminal
```

后端会在 shared workspace 中启动 PTY/bash，转发输入输出，断开时杀掉进程组。

Terminal WebSocket 与 Graph/Scheduler/SSE 无关：

```text
终端里执行命令不会自动创建 Graph 节点；
Graph 状态也不会通过 Terminal WebSocket 传递。
```

---

## 17. Graph mutation 到 SSE 的事件桥

Runtime 注册 Graph mutation callback，把 Graph 变化转换成 EventBus 事件。

代码：[runtime.py:2873-2932](../runtime/runtime.py#L2873-L2932)

映射关系：

| Graph mutation | SSE event |
|---|---|
| `add_node` | `node_added` |
| `update_node` | `node_updated` |
| `remove_node` | `node_removed` |
| `add_edge` | `edge_added` |
| `remove_edge` | `edge_removed` |
|
如果 mutation payload 中包含 log，还会额外发布：

```text
log_appended
```

前端 `sseEvents.ts` 会按 event 类型更新 GraphStore、SessionStore 和日志。

---

## 18. 工作区、shared workspace 和产物

### 18.1 路径层级

```text
Session workspace root
  ├── shared/
  └── nodes/
        └── <node_id>/
```

语义：

| 路径 | 语义 |
|---|---|
| `shared/` | 会话级协作目录，所有节点可读取 |
| `nodes/<id>/` | 节点私有运行目录 |
| `shared/<output_path>/` | 节点声明的共享产出分区 |
| `nodes/<id>/output/` | 节点私有交付目录 |

Runtime 注入配置：[runtime.py:363-428](../runtime/runtime.py#L363-L428)

路径解析：[workspace_resolver.py:24-82](../runtime/workspace_resolver.py#L24-L82)

### 18.2 output_path

`output_path` 是相对 shared 根的分区路径，例如：

```text
output_path = "backend"
```

实际分区：

```text
<shared_workspace>/backend/
```

系统会拒绝：

- 绝对路径；
- `..` 路径穿越；
- 非法分区格式。

多个节点声明同一分区时当前只记录告警，因为共享文件系统允许多节点协作写入；文件级并发冲突由协作约定和产物 Gate 负责。

### 18.3 产物浏览和上传

会话/节点支持：

- 列目录；
- 读取文本文件；
- 下载文件；
- 上传文件；
- 复制真实路径；
- reveal 到 Finder；
- 打开会话终端。

上传策略：

1. 先校验目标目录和路径边界；
2. 校验文件名；
3. 再接收 body；
4. 超过大小限制返回 413；
5. 同名自动改名，不覆盖已有文件。

节点 results 上传只允许落在节点声明的产出目录，不会偷偷回退到节点 workspace。

---

## 19. 会话摘要状态计算

SessionManager 根据 Graph 中节点状态生成摘要，逻辑在 [session_manager.py:864-909](../runtime/session_manager.py#L864-L909)。

优先级大致是：

```text
有 Running       → Running
否则有 Paused    → Paused
没有活跃节点且有 Failed → Failed
所有节点 Completed → Completed
所有节点 Stopped   → Stopped
其他情况          → Waiting
```

同时还会返回：

```text
node_count
running_count
completed_count
failed_count
lifecycle_state
desired_state
restart_safe
recovery_required
interrupted_node_ids
state_version
```

前端通过 `state_version` 防止旧的生命周期事件覆盖较新的会话状态。

---

## 20. 持久化与恢复

### 20.1 Graph 保存

Persistence 保存采用原子写入：

```text
graph snapshot
  → graph.json.tmp
  → flush + fsync
  → rename 覆盖 graph.json
```

代码：[persistence.py:41-69](../runtime/persistence.py#L41-L69)

保存过程使用 `_save_lock`，避免定时线程、API 线程和 shutdown 同时写同一个临时文件。

### 20.2 Graph 加载

加载顺序：

```text
graph.json
  ↓ 语法或语义损坏
graph.json.tmp
  ↓ 也损坏
None + error log
```

损坏包括：

- JSON 语法错误；
- 非法 NodeStatus；
- datetime 无法解析；
- 缺少关键字段；
- Edge/Node 结构不合法。

### 20.3 Runtime 恢复

Runtime 启动恢复时会：

1. 加载 Graph snapshot；
2. 恢复 Node/Edge；
3. 恢复 Graph version；
4. 降级崩溃时 Running 节点；
5. 恢复 Manager phase；
6. 恢复 Agent events；
7. 以 paused/safe recovery 方式启动 Scheduler；
8. 启动定时保存。

如果 Manager 在崩溃前处于 waiting，且子节点已经全部终态，恢复时会补偿性推进到 `Ready/review`，避免永久卡在 Waiting。

---

## 21. 错误和异常分支总览

### 21.1 HTTP 层

| 场景 | 典型响应 |
|---|---|
| 会话不存在 | 404 |
| 节点不存在 | 404 |
| 参数格式不合法 | 400 或 Pydantic 422 |
| 编辑 Running 节点 | 409 |
| 删除有子节点的 Manager | 409 |
| 删除 Running 节点 | 409 |
| 依赖环 | 400 |
| 生命周期状态不允许操作 | 409 |
| 上传文件过大 | 413 |
| reveal 不支持 | 501 |
| 路径穿越 | 400 |

统一领域异常转换：[session_server.py:60-68](../runtime/session_server.py#L60-L68)

### 21.2 调度层

| 场景 | 行为 |
|---|---|
| 依赖未完成 | 当前节点保持 Waiting |
| 依赖 Failed/Stopped | 不级联失败，下游继续 Waiting |
| 并发槽位不足 | Ready 保留，等待后续 tick |
| Worker 异常 | Failed，进入重试判断 |
| Future 被取消 | 映射为 Stopped |
| 旧 Future 晚到 | 不覆盖已 Paused/Stopped 节点 |
| Manager 规划失败 | Manager Failed 或走自身重试 |
| Review 不通过 | 只修复失败子节点 |
| 达到 max_iterations | 保持 Failed |
| Router 失败 | route_failed，允许之后重路由 |
| 事件目标为空 | delivered，但 routed_to 为空 |

### 21.3 前端错误展示

前端错误来源包括：

- Dialog 表单错误；
- NodeInspector 局部错误；
- Header 控制错误；
- Toast；
- GraphCanvas 的原生 alert。

Header 和 Inspector 的错误提示通常 3 秒自动清除。已经被后端接受的 pause/resume/stop 命令，即使之后的 summary refresh 失败，也不会被前端错误地当成命令失败；SSE 会继续负责最终状态对齐。

---

## 22. 测试覆盖索引

### 会话和接口

- [tests/test_session_server.py](../tests/test_session_server.py)
- [tests/test_session_concurrency.py](../tests/test_session_concurrency.py)
- [tests/test_session_shared.py](../tests/test_session_shared.py)

### Scheduler

- [tests/test_scheduler.py](../tests/test_scheduler.py)
- [tests/test_scheduler_manager.py](../tests/test_scheduler_manager.py)
- [tests/test_scheduler_retry.py](../tests/test_scheduler_retry.py)
- [tests/test_scheduler_concurrency.py](../tests/test_scheduler_concurrency.py)
- [tests/test_dependency_gate.py](../tests/test_dependency_gate.py)

### Manager / Worker

- [tests/test_manager.py](../tests/test_manager.py)
- [tests/test_manager_collaboration.py](../tests/test_manager_collaboration.py)
- [tests/test_worker.py](../tests/test_worker.py)
- [tests/test_root_revise.py](../tests/test_root_revise.py)

### Agent events / Router

- [tests/test_agent_events.py](../tests/test_agent_events.py)
- [tests/test_event_bus.py](../tests/test_event_bus.py)
- [tests/test_event_queue_unified.py](../tests/test_event_queue_unified.py)
- [tests/test_event_reactivation_bridge.py](../tests/test_event_reactivation_bridge.py)
- [tests/test_routing_reactivation.py](../tests/test_routing_reactivation.py)
- [tests/test_router_lifecycle.py](../tests/test_router_lifecycle.py)

### 持久化和并发

- [tests/test_persistence_migration.py](../tests/test_persistence_migration.py)
- [tests/test_concurrency_robustness.py](../tests/test_concurrency_robustness.py)
- [tests/test_main_shutdown.py](../tests/test_main_shutdown.py)

### 前端和 SSE

- [frontend/src/providers/__tests__/SSEProvider.test.tsx](../frontend/src/providers/__tests__/SSEProvider.test.tsx)
- [frontend/src/providers/__tests__/sseEvents.test.ts](../frontend/src/providers/__tests__/sseEvents.test.ts)
- [frontend/src/stores/__tests__/SessionStore.test.ts](../frontend/src/stores/__tests__/SessionStore.test.ts)
- [frontend/src/stores/__tests__/GraphStore.test.ts](../frontend/src/stores/__tests__/GraphStore.test.ts)
- [frontend/src/components/__tests__/NodeInspector.test.tsx](../frontend/src/components/__tests__/NodeInspector.test.tsx)
- [frontend/src/utils/__tests__/sessionApi.test.ts](../frontend/src/utils/__tests__/sessionApi.test.ts)

---

## 23. 一次典型执行的简化时序

```text
启动服务
  ↓
SessionManager 恢复历史会话
  ↓
用户创建空白会话
  ↓
用户创建 Manager 节点
  ↓
Manager = Waiting
  ↓
Scheduler 检查依赖和 planning barrier
  ↓
Manager = Ready
  ↓
Manager = Running(expand)
  ↓
AICU 返回 ExecutionPlan
  ↓
Planner 创建 Worker/Manager 子节点
  ↓
Manager = Waiting(waiting)
  ↓
子节点依次或并行执行
  ↓
子节点 Completed / Failed / Stopped
  ↓
Manager = Ready(review)
  ↓
Manager = Running(review)
  ├── 全部通过 → Manager Completed
  └── 有失败项
        ├── 仍有迭代预算 → 修复失败子节点 → 再次 Review
        └── 达到上限 → Manager Failed
```

整个过程中：

```text
Graph mutation → EventBus → SSE → 前端 GraphStore
节点日志      → EventBus → SSE + 运行中 REST 轮询
Agent events  → events.json/inbox → Scheduler/Router
状态快照      → graph.json → 崩溃恢复
```

---

## 24. 最值得记住的设计边界

1. **创建节点不是立即执行**：创建后先是 Waiting，由 Scheduler 推进。
2. **没有独立调度按钮**：新增节点后自动进入调度体系。
3. **Manager 不是普通 Worker**：必须经历 Expand、等待子节点、Review。
4. **依赖失败不会级联失败**：下游会卡在 Waiting，等待人工处理上游。
5. **Pause 和 Stop 不一样**：Pause 有恢复语义，Stop 是终止语义。
6. **Drain 和 Immediate 不一样**：Drain 等边界完成，Immediate 可能中断 CLI 并造成重复副作用。
7. **Continue 和 Revise 不一样**：Continue 主要是恢复失败/停止执行，Revise 是改变任务并增量续跑。
8. **失败重试不是新建 Retry Node**：当前主路径使用原地重试；旧的 `create_retry_node()` 已废弃。
9. **Agent Events 与 SSE Graph Events 分离**：协作事件面板不会自动实时刷新。
10. **Terminal WebSocket 与执行图无关**：终端只是 shared workspace 下的交互 shell。
11. **恢复默认安全降级**：崩溃时 Running 节点不会直接自动重跑。
12. **当前没有统一 Future 墙钟超时**：需要单独设计节点最长执行时间时，必须补充进程终止、状态竞争、产物和重试语义。

---

## 25. 后续如果继续维护，建议优先阅读的文件

按推荐顺序：

1. [runtime/session_server.py](../runtime/session_server.py)：所有多会话 HTTP/WebSocket/SSE 接口；
2. [runtime/session_manager.py](../runtime/session_manager.py)：会话创建、配置、生命周期、恢复；
3. [runtime/runtime.py](../runtime/runtime.py)：单会话中枢和节点控制；
4. [runtime/scheduler.py](../runtime/scheduler.py)：自动调度、依赖、并发、重试和事件激活；
5. [executables/manager.py](../executables/manager.py)：规划和评审；
6. [executables/worker.py](../executables/worker.py)：具体执行和 AICU/CLI 会话；
7. [runtime/agent_events.py](../runtime/agent_events.py)：协作事件持久化、投递和恢复；
8. [runtime/router.py](../runtime/router.py)：事件路由；
9. [runtime/sse_manager.py](../runtime/sse_manager.py)：SSE 缓冲和续拉；
10. [frontend/src/App.tsx](../frontend/src/App.tsx)、[frontend/src/providers/SSEProvider.tsx](../frontend/src/providers/SSEProvider.tsx)：前端初始化和实时同步。
