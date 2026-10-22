"""中断分级：屏蔽线、同级排序、急事升级、P3 攒批、插件优先级收紧。"""

from datetime import timedelta
from functools import partial

from test_persona import T0, make

from koi.kernel import Batch, Event, EventBus, Priority, RealClock, Trust
from koi.kernel.bus import effective_priority
from koi.kernel.plugins import P1_PER_HOUR, PluginHost
from koi.persona import Status
from koi.protocol import LEGACY_PROTOCOL, PROTOCOL_VERSION, Manifest


def ev(
    priority: Priority,
    type_: str = "message",
    *,
    minutes: float = 0,
    text: str = "",
    trust: Trust = Trust.KNOWN,
) -> Event:
    return Event(
        type=type_,
        priority=priority,
        source="t",
        trust=trust,
        time=T0 + timedelta(minutes=minutes),
        payload={"text": text} if text else {},
    )


# ------------------------------------------------------------------ 总线


async def test_mask_lines_and_p4_never_interrupts():
    bus = EventBus()
    got: list[Event] = []

    async def on_interrupt(e: Event) -> None:
        got.append(e)

    bus.bind(on_interrupt)
    bus.set_mask(Priority.P3)
    await bus.publish(ev(Priority.P3))
    await bus.publish(ev(Priority.P4, "feed_update"))
    assert [e.priority for e in got] == [Priority.P3]
    bus.set_mask(Priority.P4)
    await bus.publish(ev(Priority.P4, "feed_update"))
    assert len(got) == 1
    assert len(bus.pending) == 2


async def test_loosening_the_mask_releases_queued_events():
    bus = EventBus()
    bus.bind(lambda e: _noop())
    bus.set_mask(Priority.P1)
    await bus.publish(ev(Priority.P2, "owner_message", trust=Trust.OWNER))
    await bus.publish(ev(Priority.P3))
    await bus.publish(ev(Priority.P4, "feed_update"))
    released = bus.set_mask(Priority.P2)
    assert [e.priority for e in released] == [Priority.P2]
    released = bus.set_mask(Priority.P3)
    assert [e.priority for e in released] == [Priority.P3]
    assert [e.priority for e in bus.pending] == [Priority.P4]
    assert not bus.ready()


async def _noop() -> None:
    return None


async def test_take_orders_by_type_then_simplicity_and_wait():
    bus = EventBus()
    bus.set_mask(Priority.P0)
    long = ev(Priority.P3, "message", text="长" * 1000, minutes=0)
    short = ev(Priority.P3, "message", text="嗯", minutes=1)
    mention = ev(Priority.P3, "mention", text="@你", minutes=0)
    feed = ev(Priority.P4, "feed_update", text="动态", minutes=0)
    for e in (feed, mention, long, short):
        await bus.publish(e)
    now = T0 + timedelta(minutes=2)
    got = bus.take(now, Batch(max_events=3))
    assert got == [short, long, mention]
    assert list(bus.pending) == [feed]


async def test_take_caps_by_chars_but_keeps_what_must_be_handled():
    bus = EventBus()
    bus.set_mask(Priority.P0)
    first = ev(Priority.P2, "owner_message", text="主人" * 2000, trust=Trust.OWNER)
    rest = [ev(Priority.P3, text="字" * 400, minutes=m) for m in range(5)]
    for e in rest:
        await bus.publish(e)
    got = bus.take(T0, Batch(max_chars=1000), first=[first])
    assert got == [first]
    got = bus.take(T0, Batch(max_chars=1000))
    assert len(got) == 2
    assert len(bus.pending) == 3


def test_long_waiting_events_are_promoted_but_not_above_p2():
    p = Batch(promote_after_min=30)
    old_p3 = ev(Priority.P3, minutes=0)
    old_p4 = ev(Priority.P4, "feed_update", minutes=0)
    now = T0 + timedelta(minutes=31)
    assert effective_priority(old_p3, now, p) == Priority.P2
    assert effective_priority(old_p4, now, p) == Priority.P3
    assert effective_priority(old_p3, T0 + timedelta(minutes=5), p) == Priority.P3


# ------------------------------------------------------------------ 人格体


async def test_urgent_words_wake_her_and_plain_owner_messages_wait():
    persona, _, sched, bus, mind = make()
    night = T0.replace(hour=3) + timedelta(days=1)
    plain = ev(Priority.P2, "owner_message", text="晚安", trust=Trust.OWNER)
    plain = plain.model_copy(update={"time": night - timedelta(minutes=30)})
    urgent = plain.model_copy(
        update={"time": night, "payload": {"text": "紧急！快醒醒"}, "id": "urgent"}
    )
    sched.call_at(plain.time, partial(bus.publish, plain))
    sched.call_at(urgent.time, partial(bus.publish, urgent))
    await persona.start()
    await sched.run_until(night - timedelta(minutes=1))
    assert persona.state.status == Status.SLEEPING
    assert [e.priority for e in bus.pending] == [Priority.P2]

    await sched.run_until(night + timedelta(minutes=1))
    ctx = mind.wakes[-1]
    assert [e.payload["text"] for e in ctx.events] == ["紧急！快醒醒", "晚安"]
    assert ctx.events[0].priority == Priority.P1
    assert ctx.events[0].payload["urgent"] is True


async def test_strangers_cannot_use_urgent_words():
    persona, _, _, _, _ = make()
    stranger = ev(Priority.P3, text="紧急！快醒醒", trust=Trust.EXTERNAL)
    assert persona._classify(stranger) is stranger


async def test_p3_events_are_gathered_into_one_beat():
    persona, _, sched, bus, mind = make()
    await persona.start()
    persona.state.fatigue = 0
    noon = T0.replace(hour=12)
    await sched.run_until(noon)
    before = len(mind.wakes)
    a = ev(Priority.P3, text="在吗").model_copy(update={"time": noon})
    b = ev(Priority.P3, text="看到回我").model_copy(
        update={"time": noon + timedelta(seconds=10), "id": "b"}
    )
    sched.call_at(a.time, partial(bus.publish, a))
    sched.call_at(b.time, partial(bus.publish, b))

    gather = persona.params.interrupts.p3_gather_s
    await sched.run_until(noon + timedelta(seconds=gather - 1))
    assert len(mind.wakes) == before
    await sched.run_until(noon + timedelta(seconds=gather + 1))
    assert len(mind.wakes) == before + 1
    assert [e.payload["text"] for e in mind.wakes[-1].events] == ["在吗", "看到回我"]

    # 下一条 P3 要等到最小间隔之后
    c = ev(Priority.P3, text="还在吗").model_copy(
        update={"time": noon + timedelta(minutes=1), "id": "c"}
    )
    sched.call_at(c.time, partial(bus.publish, c))
    gap = persona.params.interrupts.p3_min_gap_min
    await sched.run_until(noon + timedelta(minutes=gap) - timedelta(seconds=5))
    assert len(mind.wakes) == before + 1
    await sched.run_until(noon + timedelta(minutes=gap, seconds=gather + 5))
    assert mind.wakes[-1].events[0].payload["text"] == "还在吗"


async def test_owner_message_is_not_held():
    persona, _, sched, bus, mind = make()
    await persona.start()
    noon = T0.replace(hour=12)
    await sched.run_until(noon)
    msg = ev(Priority.P2, "owner_message", text="在吗", trust=Trust.OWNER)
    msg = msg.model_copy(update={"time": noon})
    sched.call_at(noon, partial(bus.publish, msg))
    await sched.run_until(noon + timedelta(seconds=1))
    assert mind.wakes[-1].events == [msg]


# ------------------------------------------------------------------ 插件


def _host() -> tuple[PluginHost, Manifest, Manifest]:
    host = PluginHost(EventBus(), RealClock())
    ui = Manifest(id="web", version="0", max_trust="owner", permissions=["owner_ui"])
    other = Manifest(id="bili", version="0", max_trust="owner")
    return host, ui, other


def test_only_owner_ui_plugins_can_send_p0():
    host, ui, other = _host()
    now = T0
    assert host._priority(ui, 0, Trust.OWNER, PROTOCOL_VERSION, now) == Priority.P0
    assert host._priority(other, 0, Trust.OWNER, PROTOCOL_VERSION, now) == Priority.P2
    assert host._priority(ui, 0, Trust.KNOWN, PROTOCOL_VERSION, now) == Priority.P3


def test_p2_is_reserved_for_the_owner():
    host, _, other = _host()
    assert host._priority(other, 2, Trust.OWNER, PROTOCOL_VERSION, T0) == Priority.P2
    assert host._priority(other, 2, Trust.EXTERNAL, PROTOCOL_VERSION, T0) == Priority.P3


def test_p1_is_rate_limited_per_plugin():
    host, _, other = _host()
    got = [
        host._priority(other, 1, Trust.KNOWN, PROTOCOL_VERSION, T0 + timedelta(minutes=m))
        for m in range(P1_PER_HOUR + 1)
    ]
    assert got == [Priority.P1] * P1_PER_HOUR + [Priority.P3]
    later = T0 + timedelta(hours=1, minutes=1)
    assert host._priority(other, 1, Trust.KNOWN, PROTOCOL_VERSION, later) == Priority.P1


def test_legacy_plugins_keep_their_old_meaning():
    host, _, other = _host()
    legacy = [host._priority(other, p, Trust.OWNER, LEGACY_PROTOCOL, T0) for p in range(4)]
    assert legacy == [Priority.P2, Priority.P3, Priority.P3, Priority.P4]
