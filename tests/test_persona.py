import random
from datetime import datetime, timedelta, timezone
from functools import partial

from koi.kernel import Event, EventBus, Priority, Scheduler, SimClock, Store, Trust
from koi.persona import (
    Consolidation,
    ConsolidationInput,
    Perception,
    Persona,
    PersonaParams,
    PersonaState,
    Status,
    WakeContext,
    WakeOutcome,
    WakeReason,
    WillDecision,
)
from koi.persona.core import STATE_KEY
from koi.persona.params import AffectParams, SleepParams

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 19, 8, tzinfo=TZ)

# 去掉随机自醒与噪声，使测试确定
PARAMS = PersonaParams(
    sleep=SleepParams(self_wake_rate_per_h=0, near_wake_rate_per_h=0, disturb_wake_prob=0),
    affect=AffectParams(noise=0),
)


class StubMind:
    def __init__(self) -> None:
        self.wakes: list[WakeContext] = []

    async def perceive(self, ctx: WakeContext) -> Perception:
        return Perception()

    async def act(self, ctx: WakeContext, decisions: list[WillDecision]) -> WakeOutcome:
        self.wakes.append(ctx)
        return WakeOutcome(energy_cost=0.5)

    async def consolidate(self, inp: ConsolidationInput) -> Consolidation:
        return Consolidation()

    async def refine_notebook(self, notebook: str, notes: list[str]) -> str:
        return notebook


def sleeps(persona: Persona) -> int:
    return sum(e.kind == "sleep" for e in persona.journal.recent(1000))


def make(store: Store | None = None, state: PersonaState | None = None, start=T0):
    clock = SimClock(start)
    sched = Scheduler(clock)
    bus = EventBus()
    mind = StubMind()
    persona = Persona(
        PARAMS,
        clock=clock,
        scheduler=sched,
        bus=bus,
        mind=mind,
        rng=random.Random(0),
        state=state,
        store=store,
    )
    return persona, clock, sched, bus, mind


def owner_msg(t: datetime) -> Event:
    return Event(type="owner_message", priority=Priority.P0, source="t", trust=Trust.OWNER, time=t)


async def test_undisturbed_day_has_one_night_of_sleep():
    persona, _, sched, _, mind = make()
    await persona.start()
    await sched.run_until(T0 + timedelta(days=2))
    nights = [ep for ep in persona.state.episodes if ep.wake is not None]
    assert len(nights) == 2
    for ep in nights:
        assert ep.onset.hour in (21, 22, 23, 0)
        assert 7 <= (ep.wake - ep.onset).total_seconds() / 3600 <= 10
        assert ep.wake_reason == WakeReason.NATURAL
    assert sleeps(persona) == 2
    assert any(w.reason == WakeReason.AUTONOMOUS for w in mind.wakes)


async def test_owner_wakes_at_night_and_queued_events_are_delivered():
    persona, _, sched, bus, mind = make()
    night = T0.replace(hour=3) + timedelta(days=1)
    reply = Event(
        type="bili_reply",
        priority=Priority.P2,
        source="t",
        trust=Trust.EXTERNAL,
        time=night - timedelta(hours=1),
    )
    sched.call_at(reply.time, partial(bus.publish, reply))
    sched.call_at(night, partial(bus.publish, owner_msg(night)))
    await persona.start()
    await sched.run_until(night - timedelta(minutes=1))
    assert persona.state.status == Status.SLEEPING
    valence_before = persona.state.affect.emotion_v

    await sched.run_until(night + timedelta(minutes=1))
    ctx = mind.wakes[-1]
    assert ctx.reason == WakeReason.OWNER
    assert [e.type for e in ctx.events] == ["owner_message", "bili_reply"]
    assert persona.state.affect.emotion_v < valence_before  # 起床气
    assert persona.state.interrupted_sleep

    # 聊完安静下来后会睡回笼觉
    await sched.run_until(night + timedelta(hours=1))
    assert persona.state.status == Status.SLEEPING
    assert persona.state.episodes[-1].resumed


async def test_alarm_wakes_from_sleep():
    persona, _, sched, _, mind = make()
    await persona.start()
    at = T0.replace(hour=5, minute=30) + timedelta(days=1)
    persona.set_alarm(at, "早起赶车")
    await sched.run_until(at + timedelta(minutes=1))
    alarm_wakes = [w for w in mind.wakes if w.reason == WakeReason.ALARM]
    assert len(alarm_wakes) == 1
    assert alarm_wakes[0].events[0].payload["reason"] == "早起赶车"
    assert not persona.state.alarms


async def test_host_suspend_is_caught_up_in_one_jump():
    persona, clock, sched, _, _ = make()
    await persona.start()
    evening = T0.replace(hour=20)
    await sched.run_until(evening)
    clock.advance_to(evening + timedelta(hours=14))  # 电脑合盖一整夜
    await sched.run_until(evening + timedelta(hours=14, minutes=6))
    assert persona.state.status != Status.SLEEPING
    ep = persona.state.episodes[-1]
    assert ep.wake is not None and ep.wake_reason == WakeReason.NATURAL
    assert sleeps(persona) == 1


async def test_state_persists_and_resumes(tmp_path):
    store = Store(tmp_path / "koi.db")
    persona, clock, sched, _, _ = make(store=store)
    await persona.start()
    await sched.run_until(T0 + timedelta(hours=20))
    saved = store.get_model(STATE_KEY, PersonaState)
    assert saved is not None and saved.updated_at == persona.state.updated_at

    resumed, _, sched2, _, _ = make(state=saved, start=clock.now())
    await resumed.start()
    await sched2.run_until(clock.now() + timedelta(hours=14))
    assert resumed.state.episodes
    store.close()


async def test_phase_shifts_later_after_late_nights():
    persona, _, sched, bus, _ = make()
    for d in range(4):
        t = T0.replace(hour=23, minute=30) + timedelta(days=d)
        while t < T0.replace(hour=2, minute=30) + timedelta(days=d + 1):
            sched.call_at(t, partial(bus.publish, owner_msg(t)))
            t += timedelta(minutes=3)
    await persona.start()
    await sched.run_until(T0 + timedelta(days=5))
    assert persona.state.circadian_peak_hour > PARAMS.sleep.circadian_peak_hour + 0.5
