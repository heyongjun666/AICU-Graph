"""Dependency gate: 重激活路径不得在依赖未满足时提前放行节点。

覆盖统一依赖守卫 ``Scheduler.dependencies_unsatisfied`` 及其在各条
「绕过 Waiting→Ready 门控、直接把节点置 READY」的旁路上的应用：

- reactivate(target_status=READY) 依赖未满足 → 降级 WAITING
- _process_scheduled_retries 到期时依赖未满足 → 保持 WAITING
- Manager review / waiting 阶段不受依赖门控（review 不依赖 DEPENDENCY 上游）

这些路径原本直接置 READY、不查依赖，导致「依赖未满足却提前执行」；
修复后统一回落到 _process_waiting_nodes 的依赖门控。
"""

import time

import pytest

from runtime.graph import Graph
from runtime.models import Edge, EdgeType, ManagerPhase, Node, NodeStatus
from runtime.scheduler import Scheduler
from executables.base import Executable, ExecutableContext, SchedulerDelegate, RuntimeGateway
from executables.registry import ExecutableRegistry


class _NoopExecutable:
    pass


class _Delegate(SchedulerDelegate):
    def on_node_status_changed(self, node_id: str, status: NodeStatus) -> None:
        pass

    def on_manager_children_completed(self, manager_node_id: str) -> None:
        pass


class _Gateway(RuntimeGateway):
    pass


def _make_node(node_id, status=NodeStatus.WAITING, exec_type="worker",
               manager_phase=None, config=None):
    return Node(
        id=node_id,
        exec_type=exec_type,
        role="dev",
        task=f"Task {node_id}",
        config=config or {},
        status=status,
        parent_id=None,
        manager_phase=manager_phase,
        max_iterations=3,
    )


def _add_dep(graph, from_node, to_node):
    """to_node 依赖 from_node。"""
    graph.add_edge(Edge(
        id=f"e_{from_node}_{to_node}",
        type=EdgeType.DEPENDENCY,
        from_node=from_node,
        to_node=to_node,
    ))


def _make_scheduler(graph):
    return Scheduler(
        graph=graph,
        registry=ExecutableRegistry(),
        delegate=_Delegate(),
        runtime_gateway=_Gateway(),
        max_concurrency=4,
    )


# ============================================================================
# dependencies_unsatisfied 语义
# ============================================================================

class TestDependenciesUnsatisfied:

    def test_worker_with_unsatisfied_dependency(self):
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.RUNNING))
        graph.add_node(_make_node("B", status=NodeStatus.WAITING))
        _add_dep(graph, "A", "B")
        sched = _make_scheduler(graph)
        assert sched.dependencies_unsatisfied("B") is True

    def test_worker_with_all_dependencies_completed(self):
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.COMPLETED))
        graph.add_node(_make_node("B", status=NodeStatus.WAITING))
        _add_dep(graph, "A", "B")
        sched = _make_scheduler(graph)
        assert sched.dependencies_unsatisfied("B") is False

    def test_worker_without_dependencies(self):
        graph = Graph()
        graph.add_node(_make_node("B", status=NodeStatus.WAITING))
        sched = _make_scheduler(graph)
        assert sched.dependencies_unsatisfied("B") is False

    def test_manager_review_not_gated(self):
        """review 阶段的 Manager 不依赖 DEPENDENCY 上游。"""
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.RUNNING))
        graph.add_node(_make_node(
            "M", status=NodeStatus.WAITING, exec_type="manager",
            manager_phase=ManagerPhase.REVIEW.value,
        ))
        _add_dep(graph, "A", "M")
        sched = _make_scheduler(graph)
        assert sched.dependencies_unsatisfied("M") is False

    def test_manager_waiting_not_gated(self):
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.RUNNING))
        graph.add_node(_make_node(
            "M", status=NodeStatus.WAITING, exec_type="manager",
            manager_phase=ManagerPhase.WAITING.value,
        ))
        _add_dep(graph, "A", "M")
        sched = _make_scheduler(graph)
        assert sched.dependencies_unsatisfied("M") is False

    def test_manager_expand_is_gated(self):
        """尚未 expand 的 Manager（phase=expand）仍需依赖门控。"""
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.RUNNING))
        graph.add_node(_make_node(
            "M", status=NodeStatus.WAITING, exec_type="manager",
            manager_phase=ManagerPhase.EXPAND.value,
        ))
        _add_dep(graph, "A", "M")
        sched = _make_scheduler(graph)
        assert sched.dependencies_unsatisfied("M") is True

    def test_manager_none_phase_is_gated(self):
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.RUNNING))
        graph.add_node(_make_node(
            "M", status=NodeStatus.WAITING, exec_type="manager",
            manager_phase=None,
        ))
        _add_dep(graph, "A", "M")
        sched = _make_scheduler(graph)
        assert sched.dependencies_unsatisfied("M") is True

    def test_unknown_node_not_unsatisfied(self):
        graph = Graph()
        sched = _make_scheduler(graph)
        assert sched.dependencies_unsatisfied("missing") is False


# ============================================================================
# reactivate(target_status=READY) 依赖降级
# ============================================================================

class TestReactivateGate:

    def test_worker_reactivate_ready_downgraded_to_waiting(self):
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.RUNNING))
        graph.add_node(_make_node("B", status=NodeStatus.FAILED))
        _add_dep(graph, "A", "B")
        sched = _make_scheduler(graph)

        sched.reactivate("B", target_status=NodeStatus.READY)

        assert graph.get_node("B").status == NodeStatus.WAITING

    def test_worker_reactivate_ready_when_deps_satisfied(self):
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.COMPLETED))
        graph.add_node(_make_node("B", status=NodeStatus.FAILED))
        _add_dep(graph, "A", "B")
        sched = _make_scheduler(graph)

        sched.reactivate("B", target_status=NodeStatus.READY)

        assert graph.get_node("B").status == NodeStatus.READY

    def test_manager_review_reactivate_ready_not_downgraded(self):
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.RUNNING))
        graph.add_node(_make_node(
            "M", status=NodeStatus.FAILED, exec_type="manager",
            manager_phase=ManagerPhase.REVIEW.value,
        ))
        _add_dep(graph, "A", "M")
        sched = _make_scheduler(graph)

        sched.reactivate("M", target_status=NodeStatus.READY)

        assert graph.get_node("M").status == NodeStatus.READY


# ============================================================================
# _process_scheduled_retries 到期依赖门控
# ============================================================================

class TestScheduledRetryGate:

    def test_retry_due_but_deps_unsatisfied_stays_waiting(self):
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.RUNNING))
        graph.add_node(_make_node("B", status=NodeStatus.WAITING))
        _add_dep(graph, "A", "B")
        sched = _make_scheduler(graph)
        sched._scheduled_retries["B"] = time.time() - 1  # 已到期

        sched._process_scheduled_retries()

        assert graph.get_node("B").status == NodeStatus.WAITING

    def test_retry_due_and_deps_satisfied_goes_ready(self):
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.COMPLETED))
        graph.add_node(_make_node("B", status=NodeStatus.WAITING))
        _add_dep(graph, "A", "B")
        sched = _make_scheduler(graph)
        sched._scheduled_retries["B"] = time.time() - 1

        sched._process_scheduled_retries()

        assert graph.get_node("B").status == NodeStatus.READY


# ============================================================================
# _schedule_ready_nodes 兜底门控（执行前最后收口点）
# ============================================================================

class _ImmediateExecutable(Executable):
    """立即返回 COMPLETED 的最小可执行体，供验证「未被降级」的节点确实进入执行。"""

    def __init__(self, context: ExecutableContext, **kwargs):
        pass

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.COMPLETED


class TestScheduleReadyNodesGate:
    """无论哪条唤醒路径把节点置成 READY，执行前最后一关都查依赖。

    此前各旁路各自记得检查依赖，一旦新增一条唤醒路径漏查，依赖未满足的
    READY 节点就会被直接执行。现在把门控下沉到 _schedule_ready_nodes ——
    所有 READY 节点执行前唯一的必经收口点，漏查的旁路在这里被兜底降级。
    """

    def _register(self, sched: Scheduler) -> Scheduler:
        sched.registry.register("worker", _ImmediateExecutable)
        sched.registry.register("manager", _ImmediateExecutable)
        return sched

    def test_ready_worker_with_unsatisfied_dep_downgraded_to_waiting(self):
        """被某条旁路硬置成 READY 的 worker，依赖未满足 → 执行前降回 WAITING。"""
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.RUNNING))
        graph.add_node(_make_node("B", status=NodeStatus.READY))
        _add_dep(graph, "A", "B")
        sched = self._register(_make_scheduler(graph))

        sched._schedule_ready_nodes()

        assert graph.get_node("B").status == NodeStatus.WAITING

    def test_ready_worker_with_satisfied_dep_is_executed(self):
        """依赖已满足的 READY worker 不受兜底门控影响，照常进入执行。"""
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.COMPLETED))
        graph.add_node(_make_node("B", status=NodeStatus.READY))
        _add_dep(graph, "A", "B")
        sched = self._register(_make_scheduler(graph))

        sched._schedule_ready_nodes()

        assert graph.get_node("B").status == NodeStatus.RUNNING

    def test_ready_manager_review_not_downgraded(self):
        """review 阶段的 Manager 不依赖 DEPENDENCY 上游，兜底门控不得误伤。"""
        graph = Graph()
        graph.add_node(_make_node("A", status=NodeStatus.RUNNING))
        graph.add_node(_make_node(
            "M", status=NodeStatus.READY, exec_type="manager",
            manager_phase=ManagerPhase.REVIEW.value,
        ))
        _add_dep(graph, "A", "M")
        sched = self._register(_make_scheduler(graph))

        sched._schedule_ready_nodes()

        # 不降级：review 阶段由子节点完成驱动（进入执行），而非被依赖门控打回 WAITING。
        assert graph.get_node("M").status != NodeStatus.WAITING


class TestPlanningBarrier:
    """改动 4：计划先行屏障——worker 等所有 manager 展开后再执行。

    _process_waiting_nodes 与 _schedule_ready_nodes 两处都在存在未展开 manager
    （exec_type=manager 且 manager_phase ∈ {None,"expand"} 且非终态）时拦住 worker，
    manager 自身不受拦。终态（FAILED/COMPLETED/STOPPED）的 manager 不算未展开，
    以免一个 expand 失败的 manager 永久阻塞所有 worker。
    """

    def _mgmt(self, node_id, phase=None, status=NodeStatus.WAITING):
        return _make_node(node_id, status=status, exec_type="manager",
                          manager_phase=phase)

    def test_worker_held_while_manager_unexpanded(self):
        graph = Graph()
        graph.add_node(self._mgmt("M", phase=None))
        graph.add_node(_make_node("W", status=NodeStatus.WAITING))
        sched = _make_scheduler(graph)
        sched._process_waiting_nodes()

        assert graph.get_node("W").status == NodeStatus.WAITING, "worker 被屏障拦住"
        assert graph.get_node("M").status == NodeStatus.READY, "manager 自身不受拦"

    def test_worker_released_when_unexpanded_manager_waits_on_dependency(self):
        """不可展开的 Manager 不得阻塞其依赖链上的 worker。"""
        graph = Graph()
        graph.add_node(_make_node("UPSTREAM", status=NodeStatus.WAITING))
        graph.add_node(self._mgmt("M", phase=ManagerPhase.EXPAND.value))
        graph.add_node(_make_node("W", status=NodeStatus.WAITING))
        _add_dep(graph, "UPSTREAM", "M")
        sched = _make_scheduler(graph)

        sched._process_waiting_nodes()

        assert graph.get_node("M").status == NodeStatus.WAITING
        assert graph.get_node("W").status == NodeStatus.READY

    def test_worker_released_after_managers_expanded(self):
        graph = Graph()
        graph.add_node(self._mgmt("M", phase=ManagerPhase.WAITING.value))
        graph.add_node(_make_node("W", status=NodeStatus.WAITING))
        sched = _make_scheduler(graph)

        sched._process_waiting_nodes()

        assert graph.get_node("W").status == NodeStatus.READY, "无未展开 manager → 放行"

    def test_failed_manager_does_not_block(self):
        graph = Graph()
        graph.add_node(self._mgmt("M", phase="expand", status=NodeStatus.FAILED))
        graph.add_node(_make_node("W", status=NodeStatus.WAITING))
        sched = _make_scheduler(graph)

        sched._process_waiting_nodes()

        assert graph.get_node("W").status == NodeStatus.READY, "FAILED 终态不算未展开"

    def test_completed_manager_does_not_block(self):
        graph = Graph()
        graph.add_node(self._mgmt("M", phase="expand", status=NodeStatus.COMPLETED))
        graph.add_node(_make_node("W", status=NodeStatus.WAITING))
        sched = _make_scheduler(graph)

        sched._process_waiting_nodes()

        assert graph.get_node("W").status == NodeStatus.READY

    def test_plan_before_execute_disabled(self):
        graph = Graph()
        graph.add_node(self._mgmt("M", phase=None))
        graph.add_node(_make_node("W", status=NodeStatus.WAITING))
        sched = _make_scheduler(graph)
        sched.plan_before_execute = False

        sched._process_waiting_nodes()

        assert graph.get_node("W").status == NodeStatus.READY, "关掉屏障 → 现状行为"

    def test_ready_worker_downgraded_under_barrier(self):
        """旁路把 worker 置 READY 后，_schedule_ready_nodes 兜底降回 WAITING。"""
        graph = Graph()
        graph.add_node(self._mgmt("M", phase=None))
        graph.add_node(_make_node("W", status=NodeStatus.READY))
        sched = _make_scheduler(graph)
        sched.plan_before_execute = True
        sched.registry.register("worker", _ImmediateExecutable)

        sched._schedule_ready_nodes()

        assert graph.get_node("W").status == NodeStatus.WAITING

    def test_worker_executed_when_no_unexpanded_manager(self):
        """无未展开 manager 时，READY worker 正常执行（不被误降级）。"""
        graph = Graph()
        graph.add_node(_make_node("W", status=NodeStatus.READY))
        sched = _make_scheduler(graph)
        sched.registry.register("worker", _ImmediateExecutable)

        sched._schedule_ready_nodes()

        assert graph.get_node("W").status == NodeStatus.RUNNING

