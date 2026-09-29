"""tests/test_router_parse_retry.py — Router 解析失败 → 隔离会话后重试

Router 每次 ask 前先做一次 route 边界 reset；输出无法解析时再 reset 并重试一次，
而非静默空路由后一路退化。

严格区分两种「空」：
- 解析出 ``targets: []``（合法 ignore）→ 不重试，只保留一次 route 边界 reset。
- 解析失败（_RouteParseError）→ 额外 reset + 重试一次；重试仍失败 fail-closed。
"""

from runtime.graph import Graph
from runtime.models import EventMessage, Node, NodeStatus
from runtime.router import AICURouter


def _graph():
    g = Graph()
    g.add_node(Node(
        id="n1", exec_type="worker", role="dev", task="t", config={},
        status=NodeStatus.COMPLETED, step_ref="n1",
    ))
    g.add_node(Node(
        id="n2", exec_type="worker", role="dev", task="t", config={},
        status=NodeStatus.WAITING, step_ref="n2",
    ))
    return g


class _FakeCC:
    def __init__(self):
        self.reset_count = 0

    def reset_conversation(self):
        self.reset_count += 1


class _SeqAICU:
    """按序返回 ask() 结果，记录 reset 次数与调用次数。"""

    def __init__(self, results):
        self.ccs = [_FakeCC()]
        self.results = list(results)
        self.calls = 0

    def ask(self, prompt):
        i = self.calls
        self.calls += 1
        return self.results[i]


def _router(fake, graph):
    router = AICURouter(aicu_instance=fake)
    router.attach_graph(graph)
    return router


def _event():
    return EventMessage(event_id="e1", producer="n1", objective="hi")


def test_parse_failure_resets_and_retries():
    """解析失败 → 重置会话 + 重试一次 → 第二次命中目标。"""
    fake = _SeqAICU([
        "garbage non-json output",           # 第一次：解析失败
        {"targets": ["n2"], "reason": "交给 n2"},
    ])
    router = _router(fake, _graph())

    targets = router.route(_event())

    assert targets == ["n2"]
    assert fake.calls == 2, "解析失败应重试一次"
    assert fake.ccs[0].reset_count == 2, "解析失败应重置一次会话"


def test_empty_targets_no_reset():
    """合法空 targets（ignore）→ 不重置、不重试。"""
    fake = _SeqAICU([{"targets": [], "reason": "无需处理"}])
    router = _router(fake, _graph())

    targets = router.route(_event())

    assert targets == []
    assert fake.calls == 1, "合法空 targets 不应重试"
    assert fake.ccs[0].reset_count == 1, "合法空 targets 仅执行 route 边界 reset"


def test_retry_failure_fails_closed():
    """重试仍解析失败 → fail-closed（返回空），不无限重试，记录 parse_error。"""
    fake = _SeqAICU(["junk1", "junk2"])
    router = _router(fake, _graph())

    targets = router.route(_event())

    assert targets == []
    assert fake.calls == 2, "只重试一次"
    assert fake.ccs[0].reset_count == 2
    trace = router.decision_trace()
    assert "parse_failed_after_retry" in trace.get("parse_error", "")


def test_parse_failure_then_empty_targets_ok():
    """第一次失败重试后返回合法空 targets → 返回空，且是合法 ignore（非失败）。"""
    fake = _SeqAICU(["not json", {"targets": [], "reason": "ignore"}])
    router = _router(fake, _graph())

    targets = router.route(_event())

    assert targets == []
    assert fake.calls == 2
    assert fake.ccs[0].reset_count == 2
    assert router.decision_trace().get("decision_reason") == "ignore"
