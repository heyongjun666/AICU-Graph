"""
tests/test_agent_events.py — Agent 事件总线（Node → Router → Node）测试

覆盖方案三（独立 AICU Router）：
  1. EventMessage round-trip 与缺失字段容忍（{event_id, producer, content, created_at}）
  2. events.json save/load 恢复日志 + 去重（不再有 type 校验）
  3. 发布入口摄取（写 events/*.json → scan_node/flush → 全局日志 + Router 投递 + 源文件删除）
  4. scan_all 扫描所有节点发布入口
  5. Router 投递（content → 目标 inbox；未命中 → 不投递）
  6. 非法 config.events 形状拒绝（_prepare_node_config 归一化，兼容残留）
  7. 状态投递矩阵（WAITING/READY → 仅 event_inbox；RUNNING → + events_inbox；
     COMPLETED/FAILED/STOPPED/PAUSED → 仅 event_inbox；live 只是实时信号）
  8. 无订阅重放（replay 恒 False、新节点不补历史事件）
  9. Worker 上下文事件块（publish/inbox 指令 + 列出待处理事件，content-only）
  10. Manager expand prompt 事件协议（领域级、无 Runtime 内部词汇）
  11. update_node_status 时间戳填充（RUNNING → started_at；终态 → finished_at）
  12. Worker.run 完成路径 flush 兜底摄取事件
  13. Router 决策投递（V3.1 方案三主链路：事件 content → Router 桩 → 目标 inbox；
      producer 为 step_ref 兼容解析；inbox 事件保留 4 字段无 target/type）
"""

import json
import os
import re

import pytest

from executables.base import Executable, ExecutableContext
from executables.manager import Manager
from executables.worker import Worker
from runtime.agent_events import AgentEventManager
from runtime.models import EventMessage, ExecutionPlan, NodeStatus, Step
from runtime.runtime import Runtime


class _TestExecutable(Executable):
    """Minimal executable (auto-completes immediately)."""

    def __init__(self, context=None, **kwargs):
        super().__init__()

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.COMPLETED

    def stop(self, ctx: ExecutableContext) -> None:
        pass

    def pause(self, ctx: ExecutableContext) -> None:
        pass

    def resume(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.RUNNING

    def cleanup_resources(self, ctx: ExecutableContext) -> None:
        pass


class _RouteMapStub:
    """测试专用 Router 桩：按 content 关键词 → 目标 step_ref 映射决策。

    桩处理 Router prompt（``## Route Event``），从「事件内容」段提取 content，
    用测试专用映射表返回 ``{"targets": [...]}``。映射表是测试代码，
    **绝不解析 content 中的目标声明**。
    """

    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    def ask(self, prompt: str) -> dict:
        content = ""
        m = re.search(
            r"### 事件描述（objective）\s*(.*?)\s*### 当前组织图", prompt, re.S
        )
        if m:
            content = m.group(1)
        for kw, targets in self.mapping.items():
            if kw in content:
                return {"targets": list(targets)}
        return {"targets": []}


def _make_runtime(tmp_path, route_map=None):
    rt = Runtime(
        workspace_root=str(tmp_path),
        router_aicu_instance=_RouteMapStub(route_map),
    )
    rt.registry.register("manager", _TestExecutable)
    rt.registry.register("worker", _TestExecutable)
    return rt


@pytest.fixture
def runtime(tmp_path):
    """Real Runtime with registered test executables and a temp workspace root."""
    return _make_runtime(tmp_path)


def _step(step_id: str, config: dict, **kw) -> Step:
    kw.setdefault("exec_type", "worker")
    kw.setdefault("role", "default")
    kw.setdefault("task", f"task {step_id}")
    kw.setdefault("task_summary", f"summary {step_id}")
    kw.setdefault("depends_on", [])
    return Step(step_id=step_id, config=config, **kw)


def _inject_pair(runtime, pub_cfg, sub_cfg):
    """注入 生产+接收 两个节点，返回 (root, producer, subscriber)。"""
    root = runtime.submit_root_manager("build app")
    nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
        _step("producer", pub_cfg),
        _step("subscriber", sub_cfg),
    ]))
    return root, nodes[0], nodes[1]


# ============================================================================
# 1. EventMessage
# ============================================================================

class TestEventMessage:
    def test_round_trip(self):
        m = EventMessage(
            event_id="evt_1", producer="backend",
            objective="api 已就绪：swagger 契约已生成",
            created_at="2026-08-08T10:00:00",
        )
        m2 = EventMessage.from_dict(m.to_dict())
        assert m2 == m
        assert m2.objective == "api 已就绪：swagger 契约已生成"

    def test_from_dict_tolerates_missing(self):
        m = EventMessage.from_dict({})
        assert m.event_id == ""
        assert m.producer == ""
        assert m.objective == ""
        assert m.created_at == ""


# ============================================================================
# 2. events.json 持久化 + 去重
# ============================================================================

class TestEventLogPersistence:
    def test_save_load_restores_log(self, tmp_path):
        mgr = AgentEventManager(workspace_root=str(tmp_path))
        ids = mgr.publish_events("producer", [{"content": "c1"}, {"content": "c2"}])
        mgr.save()

        mgr2 = AgentEventManager(workspace_root=str(tmp_path))
        mgr2.load()
        assert mgr2.event_count() == 2
        assert [e.event_id for e in mgr2.list_events()] == ids

    def test_events_file_written(self, tmp_path):
        mgr = AgentEventManager(workspace_root=str(tmp_path))
        mgr.publish_events("producer", [{"content": "c1"}])
        mgr.save()
        path = os.path.join(str(tmp_path), "events.json")
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, list) and len(data) == 1
        assert data[0]["objective"] == "c1"
        # 结构化字段：objective/intent/affected_roles/scope/action 为生产者参考信号；
        # routed_to/status 为框架元数据
        assert set(data[0].keys()) == {
            "event_id", "producer", "objective", "intent", "affected_roles",
            "scope", "action", "created_at", "routed_to", "status",
        }

    def test_events_file_persisted_on_publish_no_explicit_save(self, tmp_path):
        """摄取新事件后立即落盘 events.json（无需显式 save()）。"""
        mgr = AgentEventManager(workspace_root=str(tmp_path))
        path = os.path.join(str(tmp_path), "events.json")
        assert not os.path.exists(path)
        mgr.publish_events("producer", [{"content": "c1"}, {"content": "c2"}])
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, list) and len(data) == 2
        assert {e["objective"] for e in data} == {"c1", "c2"}
        # 重复发布带同一 event_id → 去重不产生新事件，events.json 内容不变
        first = mgr.list_events()[0]
        mgr.publish_events("producer", [{"event_id": first.event_id, "content": "c1"}])
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 2

    def test_dedup_same_event_id(self, tmp_path):
        mgr = AgentEventManager(workspace_root=str(tmp_path))
        ev = {"event_id": "evt_dup", "content": "c", "producer": "p"}
        mgr.publish_events("p", [ev])
        ret = mgr.publish_events("p", [dict(ev)])
        assert ret == []
        assert mgr.event_count() == 1

    def test_auto_fill_producer_and_ids(self, tmp_path):
        mgr = AgentEventManager(workspace_root=str(tmp_path))
        mgr.publish_events("producer_id", [{"content": "c"}])
        ev = mgr.list_events()[0]
        assert ev.producer == "producer_id"
        assert ev.event_id.startswith("evt_")
        assert ev.created_at != ""


# ============================================================================
# 3. 发布入口摄取
# ============================================================================

class TestScanIngest:
    def test_publish_file_ingested_route_and_deleted(self, tmp_path):
        runtime = _make_runtime(tmp_path, {"api_ready": ["subscriber"]})
        root, producer, subscriber = _inject_pair(
            runtime,
            {"events": {"publish": ["api_ready"]}},
            {"events": {"subscribe": ["api_ready"]}},
        )
        bws = runtime.get_node_workspace(producer.id)
        events_dir = os.path.join(bws, "events")
        os.makedirs(events_dir, exist_ok=True)
        ev_file = os.path.join(events_dir, "evt_x.json")
        with open(ev_file, "w", encoding="utf-8") as f:
            json.dump({
                "content": "api_ready 契约已就绪，可供联调",
            }, f)

        n = runtime.flush_node_events(producer.id)
        assert n == 1

        # 源文件删除（摄取后清理）
        assert not os.path.exists(ev_file)

        # 全局日志
        events = [e for e in runtime.agent_events.list_events() if e.objective]
        assert len(events) == 1
        assert "api_ready" in events[0].objective

        # Router 桩路由到订阅方 event_inbox
        fws = runtime.get_node_workspace(subscriber.id)
        inbox = os.path.join(fws, "event_inbox")
        assert os.path.isdir(inbox)
        assert len(os.listdir(inbox)) == 1

        # 生产者未被路由（Router 未选择它）→ 无 inbox
        assert not os.path.exists(os.path.join(bws, "event_inbox"))

    def test_scan_tolerates_missing_dir(self, runtime):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[_step("a", {})]))
        assert runtime.flush_node_events(nodes[0].id) == 0

    def test_scan_ignores_non_json_files(self, runtime):
        root, producer, subscriber = _inject_pair(
            runtime, {}, {"events": {"subscribe": ["t"]}},
        )
        bws = runtime.get_node_workspace(producer.id)
        os.makedirs(os.path.join(bws, "events"), exist_ok=True)
        with open(os.path.join(bws, "events", "junk.txt"), "w") as f:
            f.write("not json")
        assert runtime.flush_node_events(producer.id) == 0


# ============================================================================
# 4. scan_all
# ============================================================================

class TestScanAll:
    def test_scan_all_ingests_all_publish_dirs(self, tmp_path):
        runtime = _make_runtime(tmp_path, {"ta": ["s"], "tb": ["s"]})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("a", {"events": {"publish": ["ta"]}}),
            _step("b", {"events": {"publish": ["tb"]}}),
            _step("s", {"events": {"subscribe": ["*"]}}),
        ]))
        a, b, s = nodes
        for n in (a, b):
            d = os.path.join(runtime.get_node_workspace(n.id), "events")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "ev.json"), "w", encoding="utf-8") as f:
                json.dump({"content": "ta event" if n is a else "tb event"}, f)
        assert runtime.scan_agent_events() == 2
        inbox = os.path.join(runtime.get_node_workspace(s.id), "event_inbox")
        assert len(os.listdir(inbox)) == 2

    def test_scan_persists_events_file(self, tmp_path):
        """scan 摄取发布入口后立即落盘 events.json（无需显式 save()）。"""
        runtime = _make_runtime(tmp_path, {"ta": ["s"]})
        root = runtime.submit_root_manager("app")
        a, s = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("a", {"events": {"publish": ["ta"]}}),
            _step("s", {"events": {"subscribe": ["*"]}}),
        ]))
        ev_file = runtime.agent_events.events_file
        assert not os.path.exists(ev_file)
        d = os.path.join(runtime.get_node_workspace(a.id), "events")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "ev.json"), "w", encoding="utf-8") as f:
            json.dump({"content": "ta event"}, f)
        assert runtime.scan_agent_events() == 1
        assert os.path.exists(ev_file)
        with open(ev_file, encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 1 and data[0]["objective"] == "ta event"


# ============================================================================
# 5. Router 投递
# ============================================================================

class TestRouterRouting:
    def test_route_by_content_delivered(self, tmp_path):
        """事件 content 命中 Router 桩映射 → 投递到目标 inbox。"""
        runtime = _make_runtime(tmp_path, {"a": ["subscriber"]})
        _, producer, subscriber = _inject_pair(runtime, {}, {})
        runtime.publish_events(producer.id, [{"content": "a 信号"}])
        runtime.publish_events(producer.id, [{"content": "b 信号"}])
        inbox = os.path.join(runtime.get_node_workspace(subscriber.id), "event_inbox")
        assert len(os.listdir(inbox)) == 1

    def test_no_route_not_delivered(self, runtime):
        """Router 桩映射未命中 → 不投递（无人接收）。"""
        _, producer, subscriber = _inject_pair(runtime, {}, {})
        runtime.publish_events(producer.id, [{"content": "a 信号"}])
        inbox = os.path.join(runtime.get_node_workspace(subscriber.id), "event_inbox")
        assert not os.path.exists(inbox) or os.listdir(inbox) == []

    def test_exact_content_matched(self, tmp_path):
        runtime = _make_runtime(tmp_path, {"api_ready": ["subscriber"]})
        _, producer, subscriber = _inject_pair(runtime, {}, {})
        runtime.publish_events(producer.id, [{"content": "api_ready 就绪"}])
        inbox = os.path.join(runtime.get_node_workspace(subscriber.id), "event_inbox")
        assert len(os.listdir(inbox)) == 1


# ============================================================================
# 6. config.events 归一化校验
# ============================================================================

class TestEventsConfigValidation:
    @pytest.mark.parametrize("cfg", [
        {"events": "not-a-dict"},
        {"events": {"publish": "api_ready"}},          # publish 非列表
        {"events": {"publish": [""]}},                  # 空字符串占位（历史事故根因）
        {"events": {"publish": [{"id": "x", "content": "c"}]}},  # dict 项（LLM 自然产出）
        {"events": {"subscribe": [""]}},                # 空字符串
        {"events": {"subscribe": [123]}},               # 非字符串
    ])
    def test_malformed_events_config_tolerated(self, runtime, cfg):
        """方案三：events 配置已非必需，任何畸形形状都不得让 inject_plan 崩溃。

        宽容归一化：非 dict → {}，非 list → []，列表内只保留非空字符串项。
        """
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[_step("x", cfg)]))
        events = (nodes[0].config or {}).get("events", {})
        assert isinstance(events, dict)
        assert events.get("publish", []) == []
        assert events.get("subscribe", []) == []

    def test_valid_events_config_normalized(self, runtime):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("x", {"events": {"publish": ["api_ready"], "subscribe": ["sig"]}}),
        ]))
        cfg = nodes[0].config["events"]
        assert cfg["publish"] == ["api_ready"]
        assert cfg["subscribe"] == ["sig"]


# ============================================================================
# 7. 状态投递矩阵
# ============================================================================

class TestDeliveryMatrix:
    @pytest.mark.parametrize("status,expect_live", [
        (NodeStatus.WAITING, False),
        (NodeStatus.READY, False),
        (NodeStatus.RUNNING, True),
        (NodeStatus.COMPLETED, False),
        (NodeStatus.FAILED, False),
        (NodeStatus.STOPPED, False),
        (NodeStatus.PAUSED, False),
    ])
    def test_delivery_matrix(self, tmp_path, status, expect_live):
        runtime = _make_runtime(tmp_path, {"t": ["subscriber"]})
        _, producer, subscriber = _inject_pair(runtime, {}, {})
        runtime.update_node_status(subscriber.id, status)
        runtime.publish_events(producer.id, [{"content": "t signal"}])
        fws = runtime.get_node_workspace(subscriber.id)
        # 恒投递 event_inbox（唯一待消费队列）
        assert os.path.exists(os.path.join(fws, "event_inbox"))
        # 仅 RUNNING 额外写实时 events_inbox 信号（非第二份待消费副本）
        live_dir = os.path.join(fws, "events_inbox")
        assert os.path.isdir(live_dir) == expect_live
        if expect_live:
            assert len(os.listdir(live_dir)) >= 1


# ============================================================================
# 8. 无订阅重放
# ============================================================================

class TestNoReplay:
    def test_no_replay_to_new_node_on_creation(self, tmp_path):
        runtime = _make_runtime(tmp_path)
        root = runtime.submit_root_manager("app")
        runtime.publish_events(root, [{"content": "sig 事件", "producer": "somewhere"}])
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("sub", {}),
        ]))
        sub = nodes[0]
        # 方案三：事件投递即完成，新节点不自动获得历史事件
        inbox = os.path.join(runtime.get_node_workspace(sub.id), "event_inbox")
        assert not os.path.exists(inbox)

    def test_explicit_replay_noop(self, runtime):
        root = runtime.submit_root_manager("app")
        ids = runtime.publish_events(root, [{"content": "sig", "producer": "p"}])
        ev_id = ids[0]
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("a", {}),
        ]))
        a = nodes[0]
        # 订阅语义已删：replay 恒 False，不重新投递
        assert runtime.replay_agent_event(ev_id) is False
        adir = os.path.join(runtime.get_node_workspace(a.id), "event_inbox")
        assert not os.path.exists(adir)

    def test_replay_unknown_event_false(self, runtime):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("a", {}),
        ]))
        assert runtime.replay_agent_event("evt_nonexistent") is False


# ============================================================================
# 9. Worker 上下文事件块
# ============================================================================

class TestWorkerContextEventBlocks:
    def test_publish_block_present(self, runtime):
        _, producer, _ = _inject_pair(
            runtime,
            {"events": {"publish": ["api_ready"]}},
            {"events": {"subscribe": ["api_ready"]}},
        )
        ctx = ExecutableContext(node=producer, runtime=runtime, config=producer.config)
        text = Worker._build_worker_context(ctx)
        assert "事件发布" in text
        assert f"{runtime.get_node_workspace(producer.id)}/events" in text
        # 结构化字段指令：示例 JSON 用 objective/intent/scope/action，无 target/type
        assert "objective" in text
        assert "不要指定目标" in text
        assert "os.replace" in text or "原子" in text

    def test_inbox_block_present(self, tmp_path):
        runtime = _make_runtime(tmp_path, {"sig": ["subscriber"]})
        _, producer, subscriber = _inject_pair(runtime, {}, {})
        runtime.publish_events(producer.id, [{"content": "sig 信号"}])
        ctx = ExecutableContext(node=subscriber, runtime=runtime, config=subscriber.config)
        text = Worker._build_worker_context(ctx)
        assert "事件收件箱" in text
        assert "event_inbox" in text or "收件箱" in text
        assert "轮询" in text

    def test_no_events_config_publish_default_on_inbox_absent(self, runtime):
        """方案三：发布默认开启（无 events 配置也注入发布块）；无待处理事件则收件箱块缺失。"""
        root = runtime.submit_root_manager("app")
        n2 = runtime.create_manual_node(task="x", role="default", parent_id=root)
        ctx = ExecutableContext(node=n2, runtime=runtime, config=n2.config)
        text = Worker._build_worker_context(ctx)
        assert "事件发布" in text
        assert "事件收件箱" not in text

    def test_inbox_lists_pending_events(self, tmp_path):
        runtime = _make_runtime(tmp_path, {"sig": ["sub"]})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("sub", {}),
        ]))
        sub = nodes[0]
        # 先建节点再发布：Router 需要目标节点已存在于组织图中才能路由投递
        runtime.publish_events(root, [{
            "content": "sig 事件：/ws/shared/app/backend/api.yaml",
        }])
        ctx = ExecutableContext(node=sub, runtime=runtime, config=sub.config)
        text = Worker._build_worker_context(ctx)
        assert "sig" in text
        assert "/ws/shared/app/backend/api.yaml" in text


# ============================================================================
# 10. Manager expand prompt 事件协议
# ============================================================================

class TestManagerExpandPromptEventProtocol:
    def test_event_protocol_not_hardcoded_in_expand_prompt(self, runtime):
        root = runtime.submit_root_manager("build fullstack app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("sub", {}, exec_type="manager"),
            _step("be", {"shared_workspace": "app", "output_path": "backend"}),
        ]))
        sub = next(n for n in nodes if n.step_ref == "sub")
        mgr = Manager.__new__(Manager)
        ctx = ExecutableContext(node=sub, runtime=runtime, config=sub.config)
        prompt = mgr._build_expand_prompt(ctx)
        # 事件协作协议已从代码硬编码块移入 roles/Manager/default.yaml 的 generate_instruction
        # （aicu 每次 ask 统一注入），expand prompt 只含上下文快照 + task。
        for moved in ("事件协作协议", "events.publish", "events.subscribe", "不建 depends_on"):
            assert moved not in prompt
        # 领域级：不暴露 Runtime 内部机制词汇
        for forbidden in ("events_inbox", "event_inbox", "events.json", "scan"):
            assert forbidden not in prompt
        # expand prompt 仍含快照 + task 主体
        assert "执行上下文快照" in prompt
        assert "build fullstack app" in prompt

    def test_event_protocol_lives_in_yaml_generate_instruction(self):
        import os
        import yaml
        roles_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "roles"
        )
        with open(os.path.join(roles_dir, "Manager", "default.yaml")) as f:
            cfg = yaml.safe_load(f)
        gi = cfg["generate_instruction"]
        assert "事件" in gi
        assert "events.publish" in gi
        # 订阅语义已删：Router 是唯一路由，yaml 不再暴露 events.subscribe
        assert "events.subscribe" not in gi
        assert "不建 depends_on" in gi
        # 领域级：yaml 协议不暴露 Runtime 内部机制词汇
        for forbidden in ("events_inbox", "event_inbox", "events.json", "scan"):
            assert forbidden not in gi


# ============================================================================
# 11. update_node_status 时间戳填充
# ============================================================================

class TestTimestampFill:
    def test_running_fills_started_at(self, runtime):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[_step("w", {})]))
        w = nodes[0]
        assert w.started_at is None
        runtime.update_node_status(w.id, NodeStatus.RUNNING)
        assert w.started_at is not None

    def test_terminal_fills_finished_at(self, runtime):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[_step("w", {})]))
        w = nodes[0]
        runtime.update_node_status(w.id, NodeStatus.RUNNING)
        assert w.finished_at is None
        runtime.update_node_status(w.id, NodeStatus.COMPLETED)
        assert w.finished_at is not None


# ============================================================================
# 12. Worker.run 完成路径 flush 兜底
# ============================================================================

class _EventPublishingStub:
    """Stub AICU：从任务文本中提取 events 目录并写入一个 content 事件文件。"""

    def __init__(self):
        self.events_dir = None

    def ask(self, task: str):
        m = re.search(r"`(/[^`]*?/events)`", task)
        if m:
            self.events_dir = m.group(1)
        if self.events_dir:
            os.makedirs(self.events_dir, exist_ok=True)
            with open(os.path.join(self.events_dir, "stub_evt.json"), "w", encoding="utf-8") as f:
                json.dump({"content": "api_ready 后端已就绪"}, f)
        return {"final_output_path": ""}


class TestWorkerRunFlush:
    def test_completion_flushes_published_events(self, tmp_path):
        rt = _make_runtime(tmp_path, {"api_ready": ["frontend"]})
        rt.registry.register("worker", Worker)
        root = rt.submit_root_manager("app")
        nodes = rt.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {"events": {"publish": ["api_ready"]}}),
            _step("frontend", {"events": {"subscribe": ["api_ready"]}}),
        ]))
        backend, frontend = nodes
        stub = _EventPublishingStub()
        worker = Worker(ExecutableContext(node=backend, runtime=rt, config=backend.config), aicu_instance=stub)
        status = worker.run(ExecutableContext(node=backend, runtime=rt, config=backend.config))
        assert status == NodeStatus.COMPLETED

        # 事件已被 flush 摄取进全局日志
        events = [e for e in rt.agent_events.list_events() if "api_ready" in e.objective]
        assert len(events) == 1

        # 发布文件已被摄取删除
        bws = rt.get_node_workspace(backend.id)
        assert not os.path.exists(os.path.join(bws, "events", "stub_evt.json"))

        # Router 桩路由到 frontend inbox
        inbox = os.path.join(rt.get_node_workspace(frontend.id), "event_inbox")
        assert os.path.isdir(inbox)
        assert len(os.listdir(inbox)) == 1

        # 后端节点 COMPLETED
        node = rt.graph.get_node(backend.id)
        assert node.status == NodeStatus.COMPLETED


# ============================================================================
# 13. Router 决策投递（V3.1 方案三 Node→Node 主链路）
# ============================================================================

class TestRouterDelivery:
    def test_content_routed_to_target_delivered(self, tmp_path):
        """事件 content 由 Router 桩路由 → 投递到目标 inbox（事件 4 字段无 target/type）。"""
        runtime = _make_runtime(tmp_path, {"api_ready": ["frontend"]})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
            _step("frontend", {}),
        ]))
        backend, frontend = nodes
        runtime.publish_events(backend.id, [
            {"content": "api_ready 契约已就绪，可供联调"},
        ])
        inbox = os.path.join(runtime.get_node_workspace(frontend.id), "event_inbox")
        assert len(os.listdir(inbox)) == 1
        with open(os.path.join(inbox, os.listdir(inbox)[0]), encoding="utf-8") as fh:
            ev = json.load(fh)
        assert ev["producer"] == backend.id
        assert "api_ready" in ev["objective"]
        # 结构化事件：无 target/type/artifact/payload
        assert set(ev.keys()) == {
            "event_id", "producer", "objective", "intent", "affected_roles",
            "scope", "action", "created_at", "routed_to", "status",
        }

    def test_target_by_node_id_delivered(self, tmp_path):
        """Router 目标可用 node_id（Router 桩直接返回 id）投递。"""
        runtime = _make_runtime(tmp_path)
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
            _step("frontend", {}),
        ]))
        backend, frontend = nodes
        # Router 桩直接返回目标 node_id（经 _parse_targets 解析为图中 id 投递）
        runtime.router._aicu.mapping = {"t": [frontend.id]}
        runtime.publish_events(backend.id, [{"content": "t 信号"}])
        inbox = os.path.join(runtime.get_node_workspace(frontend.id), "event_inbox")
        assert len(os.listdir(inbox)) == 1

    def test_no_route_skipped(self, runtime):
        """Router 决策为空 → 不投递（无人接收）。"""
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
            _step("frontend", {}),
            _step("observer", {}),
        ]))
        backend, frontend, observer = nodes
        runtime.publish_events(backend.id, [{"content": "t 信号"}])
        # 无路由 → 所有节点 inbox 均空
        for n in (backend, frontend, observer):
            inbox = os.path.join(runtime.get_node_workspace(n.id), "event_inbox")
            assert not os.path.exists(inbox) or os.listdir(inbox) == []

    def test_router_target_unknown_skipped(self, tmp_path):
        """Router 目标在图内不存在 → 已摄取但未投递（告警）。"""
        runtime = _make_runtime(tmp_path, {"t": ["ghost"]})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[_step("backend", {})]))
        backend = nodes[0]
        runtime.publish_events(backend.id, [{"content": "t 信号"}])
        # 事件进入全局日志（已摄取）
        assert runtime.agent_events.event_count() == 1
        # 任何节点 inbox 未收到
        assert not os.path.exists(
            os.path.join(runtime.get_node_workspace(backend.id), "event_inbox")
        )

    def test_no_target_uses_router_only(self, tmp_path):
        """无订阅广播：Router 是唯一路由形态，target/type 完全缺席。"""
        runtime = _make_runtime(tmp_path, {"api_ready": ["frontend", "other"]})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
            _step("frontend", {"events": {"subscribe": ["api_ready"]}}),
            _step("other", {"events": {"subscribe": ["api_ready"]}}),
        ]))
        backend, frontend, other = nodes
        runtime.publish_events(backend.id, [{"content": "api_ready 就绪"}])
        for n in (frontend, other):
            inbox = os.path.join(runtime.get_node_workspace(n.id), "event_inbox")
            assert len(os.listdir(inbox)) == 1

    def test_manager_as_router_target_delivered(self, tmp_path):
        """Router 目标可以是 Manager 节点（任意 Node 均合法）。"""
        runtime = _make_runtime(tmp_path, {"task_done": ["mgr"]})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("mgr", {}, exec_type="manager"),
            _step("worker_a", {}),
        ]))
        mgr = next(n for n in nodes if n.step_ref == "mgr")
        worker_a = next(n for n in nodes if n.step_ref == "worker_a")
        runtime.publish_events(worker_a.id, [{"content": "task_done 完成"}])
        inbox = os.path.join(runtime.get_node_workspace(mgr.id), "event_inbox")
        assert len(os.listdir(inbox)) == 1

    def test_cross_scope_manager_target_delivered(self, tmp_path):
        """跨 scope：Router 可直接把事件投递给兄弟 Manager（跨 Manager 直接通信）。"""
        runtime = _make_runtime(tmp_path, {"sig": ["mgr_b"]})
        root = runtime.submit_root_manager("app")
        plan1 = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("mgr_a", {}, exec_type="manager"),
            _step("mgr_b", {}, exec_type="manager"),
        ]))
        mgr_a = next(n for n in plan1 if n.step_ref == "mgr_a")
        mgr_b = next(n for n in plan1 if n.step_ref == "mgr_b")
        plan2 = runtime.inject_plan(mgr_a.id, ExecutionPlan(steps=[
            _step("worker_x", {}),
        ]))
        worker_x = plan2[0]
        # 事件跨 Manager 直接投递到 mgr_b 的 inbox
        runtime.publish_events(worker_x.id, [{"content": "sig 信号"}])
        inbox = os.path.join(runtime.get_node_workspace(mgr_b.id), "event_inbox")
        assert len(os.listdir(inbox)) == 1

    def test_inbox_persists_content_only_event(self, tmp_path):
        """投递的事件 JSON 只保留 4 字段（收件方从 content 理解语义）。"""
        runtime = _make_runtime(tmp_path, {"t": ["frontend"]})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
            _step("frontend", {}),
        ]))
        backend, frontend = nodes
        runtime.publish_events(backend.id, [{"content": "t 信号"}])
        inbox = os.path.join(runtime.get_node_workspace(frontend.id), "event_inbox")
        with open(os.path.join(inbox, os.listdir(inbox)[0]), encoding="utf-8") as fh:
            ev = json.load(fh)
        assert set(ev.keys()) == {
            "event_id", "producer", "objective", "intent", "affected_roles",
            "scope", "action", "created_at", "routed_to", "status",
        }
        assert "target" not in ev

    def test_file_publish_routed_delivered(self, tmp_path):
        """真实主链路：CC 写 {node_ws}/events/*.json（content）→ 摄取 → Router → 目标 inbox。"""
        runtime = _make_runtime(tmp_path, {"api_ready": ["frontend"]})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
            _step("frontend", {}),
        ]))
        backend, frontend = nodes
        bws = runtime.get_node_workspace(backend.id)
        events_dir = os.path.join(bws, "events")
        os.makedirs(events_dir, exist_ok=True)
        with open(os.path.join(events_dir, "evt_real.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "content": "api_ready 契约已生成：/ws/shared/app/backend/api.yaml",
            }, fh)
        assert runtime.flush_node_events(backend.id) == 1
        inbox = os.path.join(runtime.get_node_workspace(frontend.id), "event_inbox")
        assert len(os.listdir(inbox)) == 1

    def test_file_publish_with_step_ref_producer_delivered(self, tmp_path):
        """模型在事件里写 producer=step_ref（人性化名）→ Router 兼容层仍能解析并投递。

        回归：CC 写文件可能把 producer 写成可读的 step_ref；``_resolve_producer_node``
        兼容解析成 node_id 后继续路由，不静默丢弃。
        """
        runtime = _make_runtime(tmp_path, {"api_ready": ["frontend"]})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
            _step("frontend", {}),
        ]))
        backend, frontend = nodes
        bws = runtime.get_node_workspace(backend.id)
        events_dir = os.path.join(bws, "events")
        os.makedirs(events_dir, exist_ok=True)
        with open(os.path.join(events_dir, "evt_ref.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "content": "api_ready 就绪", "producer": "backend",
            }, fh)
        assert runtime.flush_node_events(backend.id) == 1
        inbox = os.path.join(runtime.get_node_workspace(frontend.id), "event_inbox")
        assert len(os.listdir(inbox)) == 1


class _RaisingStub:
    """Router 桩：任何 ask() 调用都失败（用于证明 load() 恢复不重跑 LLM）。"""

    def ask(self, prompt: str):  # noqa: ARG002
        raise AssertionError("Router must not be re-invoked during load() redelivery")


class TestEventStatusAndRedelivery:
    """事件状态（routed → delivered → consumed）与路由记录（routed_to）语义。

    核心保证：路由决策落回事件本身（``routed_to``），重启恢复按记录确定性重投
    （幂等写 inbox），**不重跑 LLM**。
    """

    def test_routing_records_targets_and_status(self, tmp_path):
        """路由后事件记录 routed_to 目标 node_id 列表，投递完成 → status=delivered。"""
        runtime = _make_runtime(tmp_path, {"api_ready": ["frontend"]})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
            _step("frontend", {}),
        ]))
        backend, frontend = nodes
        [ev_id] = runtime.publish_events(backend.id, [{"content": "api_ready 就绪"}])
        ev = runtime.agent_events.get_event(ev_id)
        assert ev.routed_to == [frontend.id]
        assert ev.status == EventMessage.EVENT_STATUS_DELIVERED

    def test_empty_route_is_delivered_terminal(self, tmp_path):
        """Router 决策为空（无人接收）→ routed_to=[] 且直接 delivered 终态。"""
        runtime = _make_runtime(tmp_path, {})  # 无映射 → 空决策
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
            _step("frontend", {}),
        ]))
        backend = nodes[0]
        [ev_id] = runtime.publish_events(backend.id, [{"content": "无人接收的信号"}])
        ev = runtime.agent_events.get_event(ev_id)
        assert ev.routed_to == []
        assert ev.status == EventMessage.EVENT_STATUS_DELIVERED

    def test_restart_redelivers_from_record_without_llm(self, tmp_path):
        """重启 load()：已路由未投递的事件按 routed_to 记录重投（不重跑 LLM）。

        构造持久化现场：events.json 中事件 status=routed、routed_to=[frontend]、
        inbox 无文件（等价于 Router 已决策但 crash 在投递前）。load() 恢复时
        把 Router 桩换成 ask() 必抛的桩——若恢复路径重跑 Router 则测试失败。
        """
        runtime = _make_runtime(tmp_path, {"api_ready": ["frontend"]})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
            _step("frontend", {}),
        ]))
        backend, frontend = nodes
        # 直接写 events.json（不经 save()，避免 reconcile 干扰现场）
        ev = EventMessage(
            event_id="evt_crash", producer=backend.id, objective="api_ready 就绪",
            created_at="2026-01-01T00:00:00",
            routed_to=[frontend.id], status=EventMessage.EVENT_STATUS_ROUTED,
        )
        with open(runtime.agent_events.events_file, "w", encoding="utf-8") as fh:
            json.dump([ev.to_dict()], fh)
        runtime.agent_events._events = {}

        runtime.router._stub = _RaisingStub()  # ask() 必抛：证明恢复不重跑 LLM
        runtime.agent_events.load()

        loaded = runtime.agent_events.get_event("evt_crash")
        assert loaded.status == EventMessage.EVENT_STATUS_DELIVERED
        inbox = os.path.join(runtime.get_node_workspace(frontend.id), "event_inbox")
        assert os.listdir(inbox) == ["evt_crash.json"]  # 从记录确定性重投

    def test_consumed_reconciled_on_save(self, tmp_path):
        """delete-on-consume 后 save() 校正 status → consumed。"""
        runtime = _make_runtime(tmp_path, {"api_ready": ["frontend"]})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
            _step("frontend", {}),
        ]))
        backend, frontend = nodes
        [ev_id] = runtime.publish_events(backend.id, [{"content": "api_ready 就绪"}])
        ev = runtime.agent_events.get_event(ev_id)
        assert ev.status == EventMessage.EVENT_STATUS_DELIVERED

        # 消费方 delete-on-consume：删除目标 inbox 文件（等价 Scheduler 桥/CC 消费）
        inbox = os.path.join(runtime.get_node_workspace(frontend.id), "event_inbox")
        for f in os.listdir(inbox):
            os.remove(os.path.join(inbox, f))

        runtime.agent_events.save()
        assert ev.status == EventMessage.EVENT_STATUS_CONSUMED

    def test_legacy_four_field_log_tolerated(self, tmp_path):
        """旧 4 字段事件日志 load 兼容（routed_to 缺省空 → delivered 终态）。"""
        workspace = str(tmp_path)
        with open(os.path.join(workspace, "events.json"), "w", encoding="utf-8") as fh:
            json.dump([{
                "event_id": "evt_legacy",
                "producer": "backend",
                "content": "旧格式事件",
                "created_at": "2026-01-01T00:00:00",
            }], fh)
        mgr = AgentEventManager(workspace_root=workspace)
        mgr.load()
        ev = mgr.get_event("evt_legacy")
        assert ev is not None
        assert ev.routed_to == []
        # 无目标 → 恢复时 reconcile 为 delivered 终态
        assert ev.status == EventMessage.EVENT_STATUS_DELIVERED


# ============================================================================
# 15. 事件格式校验（产出即验证：content 必须为非空字符串）
# ============================================================================

class TestEventFormatValidation:
    """方案三事件格式契约：content 必须为非空字符串。

    - dict/list 等非字符串 content → 自动规范化（json.dumps），兜底修复前端白屏。
    - 空字符串 / null / 缺失 content → 拒收，源文件 .invalid 留存，投递反馈事件
      给生产者 inbox（事件→重激活桥据此让生产者重新生成）。
    """

    def test_dict_content_normalized_to_json_string(self, tmp_path):
        """publish_events 传 dict content → 摄取后 content 为 JSON 字符串。"""
        runtime = _make_runtime(tmp_path, {})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("mgr", {"exec_type": "manager", "role": "dev_member"}),
        ]))
        mgr = nodes[0]
        raw = {"content": {"event_type": "review.completed", "approved": True,
                           "defects": []}}
        [ev_id] = runtime.publish_events(mgr.id, [raw])
        ev = runtime.agent_events.get_event(ev_id)
        assert isinstance(ev.objective, str)
        parsed = json.loads(ev.objective)
        assert parsed["event_type"] == "review.completed"
        assert parsed["approved"] is True

    def test_publish_empty_content_rejected_no_ingest(self, tmp_path):
        """publish_events 空 content → 拒收（不摄入、不生成 event_id）。"""
        runtime = _make_runtime(tmp_path, {})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
            _step("frontend", {}),
        ]))
        backend = nodes[0]
        ids = runtime.publish_events(backend.id, [{"content": ""}])
        assert ids == []
        assert runtime.agent_events.list_events() == []

    def test_missing_content_field_rejected(self, tmp_path):
        """publish_events 缺失 content 字段 → 拒收。"""
        runtime = _make_runtime(tmp_path, {})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
            _step("frontend", {}),
        ]))
        backend = nodes[0]
        ids = runtime.publish_events(backend.id, [{"event_id": "evt_no_content"}])
        assert ids == []
        assert runtime.agent_events.get_event("evt_no_content") is None

    def test_scan_node_invalid_content_marked_and_feedback(self, tmp_path):
        """scan_node：空 content 发布文件 → 不摄取、改名 .invalid、反馈投递生产者 inbox。"""
        runtime = _make_runtime(tmp_path, {})
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {}),
        ]))
        backend = nodes[0]
        ws = runtime.get_node_workspace(backend.id)
        outbox = os.path.join(ws, "events")
        os.makedirs(outbox, exist_ok=True)
        pub_file = os.path.join(outbox, "evt_bad.json")
        with open(pub_file, "w", encoding="utf-8") as fh:
            json.dump({"event_id": "evt_bad", "content": ""}, fh)

        assert runtime.agent_events.scan_node(backend.id) == 0  # 未摄取
        assert os.path.exists(pub_file + ".invalid")             # 原文件留痕
        # 反馈事件投递到生产者 inbox（触发事件→重激活桥让生产者重新生成）
        inbox = os.path.join(ws, "event_inbox")
        feedback = [f for f in os.listdir(inbox) if f.startswith("evt_fmt_invalid_")]
        assert len(feedback) == 1
        with open(os.path.join(inbox, feedback[0]), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert "EVENT_FORMAT_INVALID" in data["objective"]
        assert "evt_bad" in data["objective"]
        assert runtime.agent_events.get_event("evt_bad") is None
