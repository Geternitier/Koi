"""离散事件模拟：用规则 Mind 和模拟主人跑若干天，输出曲线与作息摘要。

uv run python sim/week.py --scenario normal --days 7 --seed 1
uv run python sim/week.py --scenario late --days 10
"""

import argparse
import asyncio
import random
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import partial
from pathlib import Path

from koi.kernel import Event, EventBus, Priority, Scheduler, SimClock, Trust
from koi.persona import (
    Appraisal,
    Consolidation,
    ConsolidationInput,
    DiaryDraft,
    EventAppraisal,
    Perception,
    Persona,
    PersonaParams,
    Snapshot,
    Status,
    WakeContext,
    WakeOutcome,
    WakeReason,
    WillDecision,
)

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 10, 19, 8, 0, tzinfo=TZ)  # 周一
OUT = Path(__file__).parent / "out"


# ---------------------------------------------------------------- 模拟主人


@dataclass
class Scenario:
    name: str
    # 熬夜聊天的日子（从 0 开始）
    late_nights: set[int] = field(default_factory=set)
    # (第几天, 小时)：半夜叫醒
    night_calls: list[tuple[int, float]] = field(default_factory=list)


SCENARIOS = {
    "normal": Scenario("normal", night_calls=[(2, 3.2)]),
    "late": Scenario("late", late_nights={0, 1, 2, 3}),
    "quiet": Scenario("quiet"),
}


def owner_events(sc: Scenario, days: int, rng: random.Random) -> list[Event]:
    events: list[Event] = []

    def session(start: datetime, n: int, gap: tuple[float, float], urgent: bool = False) -> None:
        t = start
        for i in range(n):
            text = "醒醒，有急事" if urgent and i == 0 else "在吗"
            events.append(
                Event(
                    type="owner_message",
                    priority=Priority.P2,
                    source="sim.web",
                    trust=Trust.OWNER,
                    time=t,
                    payload={"text": text},
                )
            )
            t += timedelta(minutes=rng.uniform(*gap))

    day0 = START.replace(hour=0)
    for d in range(days):
        day = day0 + timedelta(days=d)
        weekend = day.weekday() >= 5
        windows = [(10.0, 23.5)] if weekend else [(12.0, 13.5), (19.0, 23.5)]
        for lo, hi in windows:
            n_sessions = _poisson(rng, 0.5 * (hi - lo))
            for _ in range(n_sessions):
                start = day + timedelta(hours=rng.uniform(lo, hi - 0.2))
                session(start, rng.randint(3, 12), (0.5, 4.0))
        if d in sc.late_nights:
            session(day + timedelta(hours=23.6), rng.randint(35, 50), (2.0, 3.5))
        for cd, hour in sc.night_calls:
            if cd == d:
                session(day + timedelta(days=1, hours=hour), 3, (1.0, 2.0), urgent=True)
        # 他人私聊与回复通知（都是 P3），全天随机
        for prio, rate, typ, trust in (
            (Priority.P3, 0.08, "friend_message", Trust.KNOWN),
            (Priority.P3, 0.3, "bili_reply", Trust.EXTERNAL),
        ):
            for _ in range(_poisson(rng, rate * 24)):
                events.append(
                    Event(
                        type=typ,
                        priority=prio,
                        source="sim",
                        trust=trust,
                        time=day + timedelta(hours=rng.uniform(0, 24)),
                    )
                )
    return [e for e in events if e.time > START]


def _poisson(rng: random.Random, lam: float) -> int:
    n, p, limit = 0, 1.0, pow(2.718281828, -lam)
    while True:
        p *= rng.random()
        if p < limit:
            return n
        n += 1


# ---------------------------------------------------------------- 规则 Mind


@dataclass
class ThinkRecord:
    time: datetime
    reason: WakeReason
    engaged: bool
    owner_msgs: int


class RuleMind:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.thinks: list[ThinkRecord] = []
        self.consolidations: list[date] = []

    async def perceive(self, ctx: WakeContext) -> Perception:
        cost, appraisals = 0.0, []
        for i, e in enumerate(ctx.events, 1):
            r = self.rng.random()
            if e.type == "owner_message":
                cost += 0.25
                if r < 0.35:
                    intensity = self.rng.choice(["slight", "medium"])
                    appraisals.append(
                        EventAppraisal(event=i, valence="positive", intensity=intensity)
                    )
                elif r > 0.9:
                    appraisals.append(
                        EventAppraisal(event=i, valence="negative", intensity="medium")
                    )
            elif e.type != "alarm":
                cost += 0.12
                if r < 0.2:
                    appraisals.append(EventAppraisal(event=i, valence="positive"))
                elif r > 0.9:
                    appraisals.append(EventAppraisal(event=i, valence="negative"))
        return Perception(appraisals=appraisals, energy_cost=cost)

    async def act(self, ctx: WakeContext, decisions: list[WillDecision]) -> WakeOutcome:
        s = ctx.snapshot
        cost, appraisals, engaged = 0.0, [], False
        owner = [e for e in ctx.events if e.type == "owner_message"]
        if not owner and ctx.reason in (
            WakeReason.AUTONOMOUS,
            WakeReason.NATURAL,
            WakeReason.SELF,
        ):
            p = min(0.9, max(0.05, 0.25 + s.energy / 200 + 0.3 * s.emotion_v))
            engaged = self.rng.random() < p
            if engaged:
                # 自己找事做（多用工具），做成了会高兴一点
                cost += 1.0
                if self.rng.random() < 0.6:
                    appraisals.append(
                        Appraisal(valence="positive", arousal="exciting", intensity="slight")
                    )
            else:
                cost += 0.1
        self.thinks.append(ThinkRecord(s.time, ctx.reason, engaged, len(owner)))
        return WakeOutcome(energy_cost=cost, appraisals=appraisals, engaged=engaged)

    async def consolidate(self, inp: ConsolidationInput) -> Consolidation:
        self.consolidations.append(inp.day)
        n = sum(e.kind == "in" for e in inp.entries)
        return Consolidation(diary=DiaryDraft(text=f"今天收到 {n} 条消息。", summary=f"{n} 条消息"))

    async def refine_notebook(self, notebook: str, notes: list[str]) -> str:
        return notebook


# ---------------------------------------------------------------- 运行


async def simulate(
    scenario: Scenario, days: int, seed: int, params: PersonaParams
) -> tuple[Persona, RuleMind, list[Snapshot], list[Event]]:
    rng = random.Random(seed)
    clock = SimClock(START)
    scheduler = Scheduler(clock)
    bus = EventBus()
    mind = RuleMind(random.Random(seed + 1))
    persona = Persona(
        params,
        clock=clock,
        scheduler=scheduler,
        bus=bus,
        mind=mind,
        rng=random.Random(seed + 2),
    )
    samples: list[Snapshot] = []

    def sample() -> None:
        samples.append(persona.snapshot())
        scheduler.call_later(timedelta(minutes=10), sample, key="sample")

    events = owner_events(scenario, days, rng)
    for e in events:
        scheduler.call_at(e.time, partial(bus.publish, e))
    await persona.start()
    scheduler.call_later(timedelta(minutes=10), sample, key="sample")
    await scheduler.run_until(START + timedelta(days=days))
    return persona, mind, samples, events


def report(persona: Persona, mind: RuleMind, samples: list[Snapshot]) -> None:
    print("\n睡眠记录")
    print(f"  {'入睡':<12}{'醒来':<12}{'时长':>6}  醒因")
    for ep in persona.state.episodes:
        if ep.wake is None:
            print(f"  {ep.onset:%m-%d %H:%M}  （仍在睡）")
            continue
        dur = (ep.wake - ep.onset).total_seconds() / 3600
        tag = "  回笼觉" if ep.resumed else ""
        print(
            f"  {ep.onset:%m-%d %H:%M}  {ep.wake:%m-%d %H:%M}  {dur:5.1f}h  {ep.wake_reason}{tag}"
        )

    print("\n每日统计")
    print(
        f"  {'日期':<8}{'思考次数':>8}{'自主醒来':>8}{'有事做':>6}{'最低精力':>8}"
        f"{'平均效价':>8}{'消耗倍率':>8}{'回充余额':>8}{'节律峰值':>8}"
    )
    days = sorted({s.time.date() for s in samples})
    for d in days:
        th = [t for t in mind.thinks if t.time.date() == d]
        auto = [t for t in th if t.reason == WakeReason.AUTONOMOUS]
        day_samples = [s for s in samples if s.time.date() == d]
        awake = [s for s in day_samples if s.status != Status.SLEEPING] or day_samples
        print(
            f"  {d:%m-%d}  {len(th):>8}{len(auto):>8}{sum(t.engaged for t in auto):>6}"
            f"{min(s.energy for s in awake):>8.0f}"
            f"{statistics.fmean(s.emotion_v for s in awake):>8.2f}"
            f"{statistics.fmean(s.energy_factor for s in awake):>8.2f}"
            f"{statistics.fmean(s.recharge_credit for s in awake):>8.1f}"
            f"{day_samples[-1].circadian_peak_hour:>8.2f}"
        )

    print("\n日记")
    print(f"  {'日期':<8}{'平均效价':>8}{'情绪峰值':>8}  摘要")
    for diary in persona.memory.diaries:
        m = diary.meta
        print(f"  {m.date:%m-%d}  {m.mood_v:>8.2f}{m.mood_peak:>8.2f}  {m.summary}")


def plot(
    name: str, persona: Persona, mind: RuleMind, samples: list[Snapshot], events: list[Event]
) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "PingFang SC",
        "Heiti SC",
        "Noto Sans CJK SC",
        "SimHei",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False

    t = [s.time for s in samples]
    fig, axes = plt.subplots(4, 1, figsize=(16, 11), sharex=True)
    ax_s, ax_e, ax_m, ax_act = axes

    for ep in persona.state.episodes:
        end = ep.wake or samples[-1].time
        for ax in axes:
            ax.axvspan(ep.onset, end, color="#3b4a8c", alpha=0.12, lw=0)

    ax_s.plot(t, [s.sleep_pressure for s in samples], label="睡眠压力 S", color="#3b4a8c")
    ax_s.plot(t, [s.upper_threshold for s in samples], "--", label="入睡阈值", color="#c0392b")
    ax_s.plot(t, [s.lower_threshold for s in samples], "--", label="自然醒阈值", color="#27ae60")
    ax_s.set_ylabel("睡眠")
    ax_s.legend(loc="upper right", fontsize=8)

    ax_e.plot(t, [s.energy for s in samples], color="#e67e22", label="精力")
    ax_e.plot(t, [s.fatigue for s in samples], color="#7f8c8d", lw=0.8, label="活动疲劳")
    ax_e.set_ylim(-2, 102)
    ax_e.set_ylabel("精力")
    ax_e.legend(loc="upper right", fontsize=8)

    ax_m.axhline(0, color="#bbb", lw=0.6)
    ax_m.plot(t, [s.emotion_v for s in samples], color="#e74c3c", lw=0.9, label="情绪效价")
    ax_m.plot(t, [s.emotion_a for s in samples], color="#8e44ad", lw=0.9, label="情绪唤醒度")
    ax_m.plot(t, [s.mood_v for s in samples], "--", color="#e74c3c", lw=1.4, label="心境效价")
    ax_m.plot(t, [s.mood_a for s in samples], "--", color="#8e44ad", lw=1.4, label="心境唤醒度")
    ax_m.set_ylim(-1, 1)
    ax_m.set_ylabel("心情")
    ax_m.legend(loc="upper right", fontsize=8, ncol=2)

    owner_t = [e.time for e in events if e.type == "owner_message"]
    ax_act.scatter(owner_t, [3] * len(owner_t), s=6, color="#c0392b", label="主人消息")
    for typ, y, color, label in (
        ("friend_message", 2.5, "#2980b9", "他人私聊"),
        ("bili_reply", 2.2, "#95a5a6", "B站回复"),
    ):
        ts = [e.time for e in events if e.type == typ]
        ax_act.scatter(ts, [y] * len(ts), s=6, color=color, label=label)
    auto = [r for r in mind.thinks if r.reason == WakeReason.AUTONOMOUS]
    ax_act.scatter(
        [r.time for r in auto if r.engaged],
        [1.5] * sum(r.engaged for r in auto),
        s=10,
        marker="^",
        color="#27ae60",
        label="自主醒来·有事做",
    )
    ax_act.scatter(
        [r.time for r in auto if not r.engaged],
        [1.2] * sum(not r.engaged for r in auto),
        s=10,
        marker="v",
        color="#7f8c8d",
        label="自主醒来·发呆",
    )
    ax_act.plot(t, [s.heat for s in samples], color="#f39c12", lw=0.8, label="交互热度")
    ax_act.set_ylim(0, 3.5)
    ax_act.set_ylabel("活动")
    ax_act.legend(loc="upper right", fontsize=8, ncol=3)

    ax_act.xaxis.set_major_locator(mdates.DayLocator(tz=TZ))
    ax_act.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18], tz=TZ))
    ax_act.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %a", tz=TZ))
    for ax in axes:
        ax.grid(True, which="major", alpha=0.3)
    fig.suptitle(f"Koi 生活周期模拟：{name}")
    fig.tight_layout()
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{name}.png"
    fig.savefig(path, dpi=110)
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="normal")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    scenario = SCENARIOS[args.scenario]
    persona, mind, samples, events = asyncio.run(
        simulate(scenario, args.days, args.seed, PersonaParams())
    )
    report(persona, mind, samples)
    if not args.no_plot:
        print(f"\n曲线：{plot(scenario.name, persona, mind, samples, events)}")


if __name__ == "__main__":
    main()
