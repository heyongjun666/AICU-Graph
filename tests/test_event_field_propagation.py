"""
tests/test_event_field_propagation.py — Event 字段透传链路测试（验收硬门槛）

锁死三处断裂点，确保结构化字段从 outbox 走到 Router prompt 全程不丢失：

  a. ``EventMessage.from_dict`` 不丢弃新字段（objective/intent/affected_roles/
     scope/action）—— 否则 Router 拿不到（断裂点②）；
  b. ``AICURouter._build_prompt`` 渲染全部新字段（而非仅 content）—— 否则对象
     带上了也进不了 LLM/桩（断裂点③）；
  c. ``_prepare_event_raws`` 对「只写 objective 不写 content」判合法、不拒收
     （断裂点①），且缺失 objective 仍拒收。

这些字段全部是 Router 的参考信号，不指定目标、不硬约束路由。
"""

from runtime.agent_events import AgentEventManager
from runtime.models import EventMessage
from runtime.router import AICURouter


def _full_field_event() -> EventMessage:
    """构造一条全字段事件（objective/intent/affected_roles/scope/action 全有值）。"""
    return EventMessage(
        event_id="evt_full",
        producer="producer_node",
        objective="login API 契约已变更",
        intent="collaboration",
        affected_roles=[{"role": "frontend_dev", "duty": "对齐接口契约"}],
        scope="related",
        action="更新实现并重新验证",
    )


class TestFieldPropagation:
    def test_from_dict_preserves_all_new_fields(self):
        raw = {
            "event_id": "evt_full",
            "producer": "producer_node",
            "objective": "login API 契约已变更",
            "intent": "collaboration",
            "affected_roles": [{"role": "frontend_dev", "duty": "对齐接口契约"}],
            "scope": "related",
            "action": "更新实现并重新验证",
        }
        msg = EventMessage.from_dict(raw)
        assert msg.objective == "login API 契约已变更"
        assert msg.intent == "collaboration"
        assert msg.affected_roles == [{"role": "frontend_dev", "duty": "对齐接口契约"}]
        assert msg.scope == "related"
        assert msg.action == "更新实现并重新验证"

    def test_from_dict_backward_compat_content_to_objective(self):
        """老事件日志只有 content → 加载时迁入 objective，历史会话可重启恢复。"""
        msg = EventMessage.from_dict({"content": "老格式事件", "producer": "p"})
        assert msg.objective == "老格式事件"

    def test_to_dict_round_trip_full_fields(self):
        msg = _full_field_event()
        data = msg.to_dict()
        assert data["objective"] == "login API 契约已变更"
        assert data["intent"] == "collaboration"
        assert data["scope"] == "related"
        assert data["action"] == "更新实现并重新验证"
        assert data["affected_roles"] == [{"role": "frontend_dev", "duty": "对齐接口契约"}]
        # content 字段已移除，不再序列化
        assert "content" not in data
        # round-trip 一致
        assert EventMessage.from_dict(data) == msg

    def test_prepare_raws_accepts_objective_without_content(self):
        """只写 objective（不写 content）的事件判合法、不拒收（断裂点①）。"""
        mgr = AgentEventManager(workspace_root=".")
        ok, bad = mgr._prepare_event_raws("p", [{"objective": "仅 objective 无 content"}])
        assert len(ok) == 1
        assert bad == []

    def test_prepare_raws_rejects_missing_objective(self):
        """objective 缺失（且无 content）→ 仍拒收。"""
        mgr = AgentEventManager(workspace_root=".")
        ok, bad = mgr._prepare_event_raws("p", [{"event_id": "evt_x"}])
        assert ok == []
        assert len(bad) == 1

    def test_build_prompt_renders_all_new_fields(self):
        """Router prompt 渲染全部新字段（而非仅 content）（断裂点③）。"""
        router = AICURouter(mode="stub")
        msg = _full_field_event()
        prompt = router._build_prompt(msg, "producer_node", [])
        assert "login API 契约已变更" in prompt
        assert "collaboration" in prompt
        assert "related" in prompt
        assert "更新实现并重新验证" in prompt
        assert "frontend_dev" in prompt
        assert "对齐接口契约" in prompt


class TestObjectiveBudget:
    """objective 简短性硬约束：产出即验证，就几句话（≤3 句 / ≤80 字），超出拒收（非截断）。

    对应「必须限制 objective（限制为几句话，不是手动截断）」——框架不修改内容，
    而是判定过长/超句数后打回，让生产者重写。
    """

    def _mgr(self):
        return AgentEventManager(workspace_root=".")

    def test_accepts_brief_objective(self):
        ok, bad = self._mgr()._prepare_event_raws(
            "p", [{"objective": "后端登录功能已完成，需要前端进行联调。"}],
        )
        assert len(ok) == 1
        assert bad == []

    def test_rejects_objective_over_char_budget(self):
        """objective 超过 80 字 → 拒收（不截断、不摄入）。"""
        long_obj = "这是超长描述。" * 12  # 每句 7 字 × 12 = 84 字
        ok, bad = self._mgr()._prepare_event_raws("p", [{"objective": long_obj}])
        assert ok == []
        assert len(bad) == 1
        assert "80" in bad[0]["reason"]

    def test_rejects_objective_over_sentence_budget(self):
        """objective 超过 3 句话（即便总字数很短）→ 拒收。"""
        four_sent = "第一句。第二句。第三句。第四句。"
        ok, bad = self._mgr()._prepare_event_raws("p", [{"objective": four_sent}])
        assert ok == []
        assert len(bad) == 1
        assert "3" in bad[0]["reason"]

    def test_budget_reason_returns_none_for_brief(self):
        """简短 objective 通过预算检查（返回 None）。"""
        from runtime import agent_events as ae
        assert ae._objective_budget_reason("简短描述。") is None

    def test_budget_constants(self):
        """简短性约束常量存在且自洽。"""
        from runtime import agent_events as ae
        assert ae.MAX_OBJECTIVE_SENTENCES == 3
        assert ae.MAX_OBJECTIVE_CHARS == 80

