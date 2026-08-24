"""
runtime/planner.py — ExecutionPlan → Node+Edge 翻译器

Planner 是 Graph 的唯一写入入口（除 restore 恢复路径外）。
接收 Manager 提交的 ExecutionPlan，校验后翻译为 Node+Edge 注入 Graph。

同时提供跨组件的共享工具方法（如 parent chain rewinding），
供 Runtime 和 Scheduler 共同使用，避免代码重复。
"""

import uuid
import warnings
from typing import Callable, Optional

from runtime.graph import Graph
from runtime.models import (
    Edge,
    EdgeType,
    ExecutionPlan,
    Node,
    NodeStatus,
)
from runtime.plan_validator import validate_errors as validate_plan_structure


class PlanValidationError(Exception):
    """ExecutionPlan 校验失败时抛出的异常。

    校验失败时不修改 Graph，保证原子性。

    字段:
        errors: 错误信息列表。
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class Planner:
    """ExecutionPlan → Node+Edge 翻译器。

    Graph 的唯一写入入口（除 restore 恢复路径外）。
    所有 Graph 的写操作（添加 Node/Edge）都必须经此组件。
    """

    VALID_EXEC_TYPES = {"manager", "worker"}
    MAX_DIRECT_WORKERS = 8  # 单层直接 worker 上限（安全网，prompt 层已约束为 ≤5）

    def __init__(self, graph: Graph):
        """初始化 Planner。

        Args:
            graph: 待操作的 Graph 实例。
        """
        self._graph = graph

    # ================================================================
    # Public API
    # ================================================================

    def translate(
        self,
        plan: ExecutionPlan,
        parent_node_id: str,
        iteration_count: int = 0,
    ) -> list[Node]:
        """校验 ExecutionPlan → 分配 UUID → 创建 Node+Edge → 注入 Graph。

        执行顺序：
          1. 校验 Plan，失败抛 PlanValidationError，不修改 Graph
          2. 为每个 step 分配全局 UUID
          3. 创建 Node（parent_id=parent_node_id, step_ref=step_id）
          4. 创建 parent Edge（from=parent_node_id, to=child Node）
          5. 创建 dependency Edge（from=被依赖方, to=依赖方）
          6. 注入 Graph

        Args:
            plan: Manager 提交的 ExecutionPlan。
            parent_node_id: 父 Manager 的 Node id。
            iteration_count: 迭代次数，>0 时 step_ref 追加 _v{iteration_count} 后缀。

        Returns:
            创建的 Node 列表。

        Raises:
            PlanValidationError: 校验失败，Graph 不会被修改。
        """
        # 1. 校验
        errors = self.validate_plan(plan)
        if errors:
            raise PlanValidationError(errors)

        # 2. 分配 UUID
        step_to_uuid: dict[str, str] = {}
        for step in plan.steps:
            step_to_uuid[step.step_id] = str(uuid.uuid4())

        # 3. 创建 Node
        nodes: list[Node] = []
        for step in plan.steps:
            step_ref = step.step_id
            if iteration_count > 0:
                step_ref = f"{step.step_id}_v{iteration_count}"

            node = Node(
                id=step_to_uuid[step.step_id],
                parent_id=parent_node_id,
                exec_type=step.exec_type,
                role=step.role,
                task=step.task,
                task_summary=step.task_summary or None,
                config=step.config,
                status=NodeStatus.WAITING,
                step_ref=step_ref,
            )
            nodes.append(node)

        # 4. 创建 parent Edge（Manager → 每个子 Node）
        parent_edges: list[Edge] = []
        for step in plan.steps:
            edge = Edge(
                id=str(uuid.uuid4()),
                type=EdgeType.PARENT,
                from_node=parent_node_id,
                to_node=step_to_uuid[step.step_id],
            )
            parent_edges.append(edge)

        # 5. 创建 dependency Edge（被依赖方 → 依赖方）
        dependency_edges: list[Edge] = []
        for step in plan.steps:
            if step.depends_on:
                for dep_id in step.depends_on:
                    edge = Edge(
                        id=str(uuid.uuid4()),
                        type=EdgeType.DEPENDENCY,
                        from_node=step_to_uuid[dep_id],
                        to_node=step_to_uuid[step.step_id],
                    )
                    dependency_edges.append(edge)

        # 6. 注入 Graph
        for node in nodes:
            self._graph.add_node(node)
        for edge in parent_edges:
            self._graph.add_edge(edge)
        for edge in dependency_edges:
            self._graph.add_edge(edge)

        return nodes

    def translate_revision(
        self,
        plan: ExecutionPlan,
        parent_node_id: str,
        reuse_map: dict[str, str],
    ) -> tuple[list[Node], list[Node]]:
        """选择性复用既有子节点 + 新建节点（增量续跑 Revise，不 _clear_children）。

        与 translate 的区别：不删除既有子节点，按 Manager 的增量决策选择性复用。

        Args:
            plan: Manager 提交的增量 ExecutionPlan。
            parent_node_id: 父 Manager 的 Node id。
            reuse_map: {step_id: 既有子节点 node_id} —— 仅对被复用的 step 提供；
                未在 reuse_map 中的 step 视为新建节点。

        Returns:
            (new_nodes, reused_nodes)：新建节点列表 + 被复用子节点列表。

        Raises:
            PlanValidationError: 校验失败，Graph 不会被修改。
        """
        # 1. 校验（任何图变更前拦截）
        errors = self.validate_plan(plan)
        if errors:
            raise PlanValidationError(errors)

        # 2. 分配 UUID：复用 step → 既有子节点 id；新 step → uuid4()
        step_to_uuid: dict[str, str] = {}
        reused_ids: set[str] = set()
        for step in plan.steps:
            if step.step_id in reuse_map:
                cid = reuse_map[step.step_id]
                step_to_uuid[step.step_id] = cid
                reused_ids.add(cid)
            else:
                step_to_uuid[step.step_id] = str(uuid.uuid4())

        # 3. 仅新建节点：创建 Node + PARENT 边（复用节点保留原 PARENT 边）
        new_nodes: list[Node] = []
        for step in plan.steps:
            cid = step_to_uuid[step.step_id]
            if cid in reused_ids:
                continue
            node = Node(
                id=cid,
                parent_id=parent_node_id,
                exec_type=step.exec_type,
                role=step.role,
                task=step.task,
                task_summary=step.task_summary or None,
                config=step.config,
                status=NodeStatus.WAITING,
                step_ref=step.step_id,
            )
            new_nodes.append(node)
            self._graph.add_node(node)
            self._graph.add_edge(Edge(
                id=str(uuid.uuid4()),
                type=EdgeType.PARENT,
                from_node=parent_node_id,
                to_node=cid,
            ))

        # 4. 重建 DEPENDENCY 边：只重建「被重新激活的子节点之间」（复用+新增）的依赖。
        #    先删这类节点间不再存在于新计划的旧边（add_edge 不去重，先删再加），
        #    再按 plan.depends_on 添加（seen 去重）；涉及未引用（保持 COMPLETED）子节点的边一律不动。
        activated_ids = set(step_to_uuid.values())
        for edge in self._graph.all_edges():
            if (
                edge.type == EdgeType.DEPENDENCY
                and edge.from_node in activated_ids
                and edge.to_node in activated_ids
            ):
                try:
                    self._graph.remove_edge(edge.id)
                except ValueError:
                    pass

        seen: set[tuple[str, str]] = set()
        for step in plan.steps:
            to_id = step_to_uuid[step.step_id]
            for dep_id in step.depends_on:
                from_id = step_to_uuid[dep_id]
                if (from_id, to_id) in seen:
                    continue
                seen.add((from_id, to_id))
                self._graph.add_edge(Edge(
                    id=str(uuid.uuid4()),
                    type=EdgeType.DEPENDENCY,
                    from_node=from_id,
                    to_node=to_id,
                ))

        reused_nodes = [self._graph.get_node(cid) for cid in reuse_map.values()]
        return new_nodes, reused_nodes

    def restore(
        self,
        nodes: list[Node],
        edges: list[Edge],
        skip_validation: bool = True,
        graph_version: int = 0,
    ) -> None:
        """崩溃恢复专用路径，跳过校验加载持久化状态。

        Args:
            nodes: 持久化的 Node 列表。
            edges: 持久化的 Edge 列表。
            skip_validation: 是否跳过校验（恢复路径默认 True）。
            graph_version: 快照保存时的 graph_version。重放 add_node/add_edge
                会从 0 重新累加版本号，必须在恢复后重设为持久化值，保证
                跨重启版本号单调一致（AC8 契约）。
        """
        for node in nodes:
            self._graph.add_node(node)
        for edge in edges:
            self._graph.add_edge(edge)
        if graph_version:
            self._graph.restore_version(graph_version)

    def create_retry_node(self, original_node: Node, reason: str) -> Node:
        """[DEPRECATED] 创建 Retry Node（新节点重试，已废弃）。

        此方法创建全新的 Node 而非原地重试，违反「节点不可控」原则。
        请改用原地重试机制：Runtime.continue_node() 或 Scheduler._try_auto_retry()，
        它们直接重置原节点状态（FAILED/STOPPED → WAITING），不创建新节点。

        保留此方法仅为向后兼容测试代码，新代码不应使用。
        """
        warnings.warn(
            "create_retry_node is deprecated. "
            "Use in-place retry (Runtime.continue_node) instead. "
            "This method creates new nodes which violates the 'controllable nodes' principle.",
            DeprecationWarning,
            stacklevel=2,
        )
        # 确定 step_ref
        step_ref = original_node.step_ref
        if original_node.iteration_count > 0:
            step_ref = f"{original_node.step_ref}_v{original_node.iteration_count}"

        # 在 task 中记录重试原因（先清除旧的 RETRY 前缀，避免重复拼接）
        import re as _re
        clean_task = _re.sub(r'\[RETRY:\s*[^\]]+\]\s*', '', original_node.task or '').strip()
        retry_task = f"[RETRY: {reason}]\n\n{clean_task}"

        # 创建 Retry Node
        retry_node = Node(
            id=str(uuid.uuid4()),
            parent_id=original_node.parent_id,
            exec_type=original_node.exec_type,
            role=original_node.role,
            task=retry_task,
            task_summary=getattr(original_node, 'task_summary', None),
            config=original_node.config,
            status=NodeStatus.WAITING,
            step_ref=step_ref,
            iteration_count=original_node.iteration_count,
            max_activations=getattr(original_node, 'max_activations', 3),
        )
        self._graph.add_node(retry_node)

        # parent Edge（Manager → Retry Node）
        if original_node.parent_id is not None:
            parent_edge = Edge(
                id=str(uuid.uuid4()),
                type=EdgeType.PARENT,
                from_node=original_node.parent_id,
                to_node=retry_node.id,
            )
            self._graph.add_edge(parent_edge)

        # dependency Edge 指向原 Node 的依赖
        original_deps = self._graph.dependencies(original_node.id)
        for dep_node in original_deps:
            edge = Edge(
                id=str(uuid.uuid4()),
                type=EdgeType.DEPENDENCY,
                from_node=dep_node.id,
                to_node=retry_node.id,
            )
            self._graph.add_edge(edge)

        return retry_node

    # ================================================================
    # Shared utilities (跨组件共享)
    # ================================================================

    def rewind_parent_chain_for_retry(
        self,
        parent_id: Optional[str],
        clear_notification: Callable[[str], None],
    ) -> None:
        """将已推进的父 Manager 链回退到等待子节点状态。

        当子节点被原地重试时，其父 Manager 可能已经接收过
        on_manager_children_completed 回调并推进到 review/completed 阶段。
        此时需要将其拉回 WAITING + manager_phase="waiting"，
        并清除通知记录，以便子节点全部再次完成时能重新触发回调。

        供 Runtime.continue_node / revise_manager_node 和 Scheduler._try_auto_retry 共用。

        Args:
            parent_id: 父节点 ID（可为 None，表示已到根节点）。
            clear_notification: 清除通知记录的回调，
                Runtime 传入 self.scheduler.clear_manager_notification，
                Scheduler 传入 self.clear_manager_notification。
        """
        cursor = parent_id
        while cursor is not None:
            parent = self._graph.get_node(cursor)
            if parent is None or parent.exec_type != "manager":
                break

            if parent.status != NodeStatus.WAITING:
                self._graph.update_node(
                    cursor,
                    status=NodeStatus.WAITING,
                    manager_phase="waiting",
                )
            # 无条件清除通知记录（含已 WAITING 的 Manager）：评审刚结束的父 Manager
            # 典型状态是 WAITING + 已通知。若不清除，子节点再次完成时
            # _check_manager_children_completed 会因 parent_id in _notified_managers
            # 提前返回，导致父 Manager 永远无法重新触发评审。clear 用 set.discard
            # 对非成员幂等，安全。
            clear_notification(cursor)

            cursor = parent.parent_id

    # ================================================================
    # Validation
    # ================================================================

    def validate_plan(self, plan: ExecutionPlan) -> list[str]:
        """校验 ExecutionPlan 返回错误列表（空列表=合法）。

        校验项：
          - steps 非空
          - 每个 step 必填字段（step_id, exec_type, role, task）
          - exec_type 为合法值（manager/worker）
          - step_id 唯一
          - depends_on 引用存在
          - depends_on 无循环依赖（拓扑排序检测）
          - config.num_ccs 合法性（由 PlanValidator 补充）

        Args:
            plan: 待校验的 ExecutionPlan。

        Returns:
            错误信息列表，空列表表示校验通过。
        """
        errors: list[str] = []

        if not plan.steps:
            errors.append("ExecutionPlan has no steps")
            return errors

        step_ids: set[str] = set()
        for i, step in enumerate(plan.steps):
            prefix = f"step[{i}]"

            # step_id
            if not step.step_id:
                errors.append(f"{prefix}: step_id is required")
            else:
                if step.step_id in step_ids:
                    errors.append(f"Duplicate step_id: '{step.step_id}'")
                step_ids.add(step.step_id)

            # exec_type
            if not step.exec_type:
                errors.append(f"{prefix}: exec_type is required")
            elif step.exec_type not in self.VALID_EXEC_TYPES:
                errors.append(
                    f"{prefix}: invalid exec_type '{step.exec_type}', "
                    f"must be one of {sorted(self.VALID_EXEC_TYPES)}"
                )

            # role
            if not step.role:
                errors.append(f"{prefix}: role is required")

            # task
            if not step.task:
                errors.append(f"{prefix}: task is required")

        # 校验 depends_on 引用（在所有 step_ids 收集完成后）
        for step in plan.steps:
            if step.depends_on:
                for dep in step.depends_on:
                    if dep == step.step_id:
                        errors.append(f"step '{step.step_id}' depends on itself")
                    elif dep not in step_ids:
                        errors.append(
                            f"step '{step.step_id}' depends_on unknown step '{dep}'"
                        )

        # 循环依赖检测（拓扑排序 — Kahn 算法）
        if not errors:
            # 构建邻接表：依赖方 → [被依赖方列表]
            dep_graph: dict[str, list[str]] = {sid: [] for sid in step_ids}
            for step in plan.steps:
                if step.depends_on:
                    for dep in step.depends_on:
                        if dep in step_ids and dep != step.step_id:
                            # step 依赖 dep → 边 dep → step
                            dep_graph.setdefault(dep, []).append(step.step_id)

            # Kahn 算法：统计入度（每个节点被依赖的次数）
            in_degree: dict[str, int] = {sid: 0 for sid in step_ids}
            for _from, to_list in dep_graph.items():
                for _to in to_list:
                    if _to in in_degree:
                        in_degree[_to] = in_degree[_to] + 1

            # 入度为 0 的节点入队
            queue = [sid for sid in step_ids if in_degree[sid] == 0]
            visited_count = 0

            while queue:
                node = queue.pop(0)
                visited_count += 1
                for neighbor in dep_graph.get(node, []):
                    in_degree[neighbor] = in_degree[neighbor] - 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)

            # 有环：visited_count < total_nodes
            if visited_count != len(step_ids):
                # 找出构成环的节点
                cyclic_nodes = [sid for sid in step_ids if in_degree[sid] > 0]
                errors.append(
                    f"Circular dependency detected among steps: {cyclic_nodes}"
                )

        # 单层直接 worker 数量上限校验（安全网，prompt 层已约束为 ≤5）
        if not errors:
            worker_count = sum(1 for s in plan.steps if s.exec_type == "worker")
            if worker_count > self.MAX_DIRECT_WORKERS:
                errors.append(
                    f"Too many direct workers ({worker_count}) in a single layer. "
                    f"Max {self.MAX_DIRECT_WORKERS} allowed. "
                    f"Group related workers under sub-manager nodes instead."
                )

        # PlanValidator 确定性结构校验（num_ccs 合法性）。
        # 此处不提供角色集 → exec_type↔role 一致性由 Runtime 在图形变更前补充校验
        # （roles_dir 未注入时跳过，validate_plan 保持纯净）。
        errors.extend(validate_plan_structure(plan))

        return errors
