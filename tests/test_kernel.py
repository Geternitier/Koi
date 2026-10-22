from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from koi.kernel import Event, EventBus, Priority, Scheduler, SimClock, Store, Trust
from koi.kernel.llm import LLMConfig, LLMError, LLMGateway

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)


def ev(priority: Priority, minutes: int = 0, type_: str = "x") -> Event:
    return Event(
        type=type_,
        priority=priority,
        source="test",
        trust=Trust.SYSTEM,
        time=T0 + timedelta(minutes=minutes),
    )


async def test_scheduler_runs_in_time_order_and_honours_keys():
    clock = SimClock(T0)
    sched = Scheduler(clock)
    seen: list[str] = []
    sched.call_at(T0 + timedelta(minutes=30), lambda: seen.append("b"))
    sched.call_at(T0 + timedelta(minutes=10), lambda: seen.append("a"))
    sched.call_at(T0 + timedelta(minutes=20), lambda: seen.append("old"), key="k")
    sched.call_at(T0 + timedelta(minutes=40), lambda: seen.append("new"), key="k")
    sched.call_at(T0 + timedelta(minutes=50), lambda: seen.append("gone"), key="c")
    sched.cancel("c")

    async def nested():
        seen.append("nested")

    sched.call_at(T0 + timedelta(minutes=35), nested)
    await sched.run_until(T0 + timedelta(hours=1))
    assert seen == ["a", "b", "nested", "new"]
    assert clock.now() == T0 + timedelta(hours=1)


async def test_bus_masks_and_drains_by_priority():
    bus = EventBus(max_pending_ambient=2)
    got: list[Event] = []

    async def on_interrupt(e: Event) -> None:
        got.append(e)

    bus.bind(on_interrupt)
    assert bus.set_mask(Priority.P0) == []
    await bus.publish(ev(Priority.P0))
    await bus.publish(ev(Priority.P2, 1))
    await bus.publish(ev(Priority.P1, 2))
    await bus.publish(ev(Priority.P3, 3))
    for m in range(3):
        await bus.publish(ev(Priority.P4, 10 + m))
    assert [e.priority for e in got] == [Priority.P0]
    drained = bus.drain()
    assert [e.priority for e in drained] == [
        Priority.P1,
        Priority.P2,
        Priority.P3,
        Priority.P4,
        Priority.P4,
    ]
    assert drained[-1].time == T0 + timedelta(minutes=12)
    assert bus.drain() == []


def test_store_roundtrip(tmp_path):
    class M(BaseModel):
        a: int
        b: str

    store = Store(tmp_path / "koi.db")
    store.put_model("m", M(a=1, b="中文"))
    store.put_model("m", M(a=2, b="中文"))
    assert store.get_model("m", M) == M(a=2, b="中文")
    assert store.get_model("missing", M) is None
    e = ev(Priority.P2, 5)
    store.append_event(e)
    assert store.events_between(T0, T0 + timedelta(hours=1)) == [e]
    store.close()


class _FakeCompletions:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=10,
            prompt_cache_hit_tokens=80,
            prompt_cache_miss_tokens=20,
        )
        msg = SimpleNamespace(content=self.replies.pop(0))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage, model="m")


class Reply(BaseModel):
    ok: bool


def _gateway(replies: list[str]) -> tuple[LLMGateway, _FakeCompletions]:
    comp = _FakeCompletions(replies)
    client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    return LLMGateway(LLMConfig(api_key="k"), client=client), comp


async def test_llm_json_retries_once_with_error_feedback():
    gw, comp = _gateway(["not json", '{"ok": true}'])
    reply, usage = await gw.chat_json([{"role": "user", "content": "回答 json"}], Reply)
    assert reply.ok
    assert usage.prompt_tokens == 200
    assert comp.calls[1]["messages"][-1]["role"] == "user"
    assert comp.calls[0]["response_format"] == {"type": "json_object"}
    assert gw.meter.calls == 2
    assert gw.meter.cache_hit_rate == pytest.approx(0.8)


async def test_llm_json_gives_up_after_second_failure():
    gw, _ = _gateway(["{}", "{}"])
    with pytest.raises(LLMError):
        await gw.chat_json([{"role": "user", "content": "json"}], Reply)
