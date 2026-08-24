"""Tests for EventBus (internal event bus for Runtime)."""

import time
import threading
from typing import List
from runtime.event_bus import SSEEvent, EventBus


class TestPublishAndSubscribe:
    """publish + subscribe 可正确传递事件负载"""

    def test_subscriber_receives_event_payload(self):
        bus = EventBus()
        received: List[SSEEvent] = []

        def handler(event: SSEEvent):
            received.append(event)

        bus.subscribe("node_status_changed", handler)
        event = SSEEvent(
            event_type="node_status_changed",
            sequence_id=1,
            timestamp=time.time(),
            payload={"node_id": "n1", "status": "running"},
        )
        bus.publish(event)

        assert len(received) == 1
        assert received[0] is event
        assert received[0].payload["node_id"] == "n1"


class TestUnsubscribe:
    """unsubscribe 停止接收事件"""

    def test_unsubscribed_subscriber_no_longer_receives_events(self):
        bus = EventBus()
        received: List[SSEEvent] = []

        def handler(event: SSEEvent):
            received.append(event)

        sid = bus.subscribe("log_appended", handler)
        e1 = SSEEvent("log_appended", 1, time.time(), {"msg": "first"})
        bus.publish(e1)
        assert len(received) == 1

        bus.unsubscribe(sid)

        e2 = SSEEvent("log_appended", 2, time.time(), {"msg": "second"})
        bus.publish(e2)
        assert len(received) == 1  # unchanged


class TestMultiSubscriber:
    """同一事件被多个订阅者接收"""

    def test_multiple_subscribers_receive_same_event(self):
        bus = EventBus()
        bag1: List[SSEEvent] = []
        bag2: List[SSEEvent] = []

        bus.subscribe("scheduler_tick", lambda e: bag1.append(e))
        bus.subscribe("scheduler_tick", lambda e: bag2.append(e))

        event = SSEEvent("scheduler_tick", 1, time.time(), {"tick": 42})
        bus.publish(event)

        assert len(bag1) == 1
        assert len(bag2) == 1
        assert bag1[0] is event
        assert bag2[0] is event


class TestEventTypeExtensibility:
    """事件类型可扩展"""

    def test_custom_event_type_delivered_correctly(self):
        bus = EventBus()
        received: List[SSEEvent] = []

        bus.subscribe("deployment.started", lambda e: received.append(e))
        event = SSEEvent("deployment.started", 1, time.time(), {"env": "prod"})
        bus.publish(event)

        assert len(received) == 1
        assert received[0].event_type == "deployment.started"


class TestWildcardSubscription:
    """通配符订阅"""

    def test_wildcard_star_matches_all(self):
        bus = EventBus()
        received: List[SSEEvent] = []

        bus.subscribe("*", lambda e: received.append(e))
        e1 = SSEEvent("node_status_changed", 1, time.time(), {})
        e2 = SSEEvent("log_appended", 2, time.time(), {})
        e3 = SSEEvent("system_alert", 3, time.time(), {})
        bus.publish(e1)
        bus.publish(e2)
        bus.publish(e3)

        assert len(received) == 3

    def test_prefix_wildcard_matches_subtypes(self):
        bus = EventBus()
        received: List[SSEEvent] = []

        bus.subscribe("node_status_changed.*", lambda e: received.append(e))
        e1 = SSEEvent("node_status_changed.running", 1, time.time(), {})
        e2 = SSEEvent("node_status_changed.failed", 2, time.time(), {})
        e3 = SSEEvent("log_appended", 3, time.time(), {})
        bus.publish(e1)
        bus.publish(e2)
        bus.publish(e3)

        assert len(received) == 2

    def test_prefix_wildcard_exact_match(self):
        """prefix.* 也应匹配不含后缀点的事件类型"""
        bus = EventBus()
        received: List[SSEEvent] = []

        bus.subscribe("system_alert.*", lambda e: received.append(e))
        e = SSEEvent("system_alert", 1, time.time(), {})
        bus.publish(e)

        assert len(received) == 1


class TestSequenceId:
    """全局单调递增 sequence_id"""

    def test_sequence_ids_are_monotonically_increasing(self):
        bus = EventBus()
        ids: List[int] = []

        def record(event: SSEEvent):
            ids.append(event.sequence_id)

        bus.subscribe("scheduler_tick", record)
        for i in range(10):
            bus.publish(SSEEvent("scheduler_tick", 0, time.time(), {}))

        # EventBus 应自动赋值 sequence_id，忽略用户传入值
        assert ids == list(range(1, 11)), f"实际IDs: {ids}"

    def test_sequence_ids_global_across_event_types(self):
        bus = EventBus()
        ids: List[int] = []

        bus.subscribe("*", lambda e: ids.append(e.sequence_id))
        bus.publish(SSEEvent("type_a", 0, time.time(), {}))
        bus.publish(SSEEvent("type_b", 0, time.time(), {}))
        bus.publish(SSEEvent("type_c", 0, time.time(), {}))

        assert ids == [1, 2, 3]

    def test_sequence_ids_thread_safe(self):
        bus = EventBus()
        ids: List[int] = []
        lock = threading.Lock()
        errors: List[str] = []

        def safe_record(event: SSEEvent):
            with lock:
                if ids and event.sequence_id <= ids[-1]:
                    errors.append(
                        f"non-monotonic: {ids[-1]} -> {event.sequence_id}"
                    )
                ids.append(event.sequence_id)

        bus.subscribe("*", safe_record)

        threads = []
        for i in range(20):
            t = threading.Thread(
                target=lambda: bus.publish(
                    SSEEvent("t", 0, time.time(), {})
                )
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert len(ids) == 20
        assert ids == sorted(ids), f"非单调递增: {ids}"
        assert not errors, f"检测到非单调: {errors}"


class TestExceptionIsolation:
    """订阅者异常隔离"""

    def test_exception_in_one_subscriber_does_not_affect_others(self):
        bus = EventBus()
        healthy_received: List[SSEEvent] = []

        def faulty(event: SSEEvent):
            raise ValueError("something went wrong")

        bus.subscribe("system_alert", faulty)
        bus.subscribe("system_alert", lambda e: healthy_received.append(e))

        event = SSEEvent("system_alert", 1, time.time(), {"msg": "test"})
        bus.publish(event)

        assert len(healthy_received) == 1
        assert healthy_received[0] is event
