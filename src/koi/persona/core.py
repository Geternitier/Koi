"""人格体：生活周期状态机。

生理过程（睡眠压力、精力、心情）由周期性 tick 以闭式解推进，不调用 LLM；
只有在"醒来"（被中断、闹钟、自然醒、自动唤醒）时才把上下文交给 Mind。
"""

import asyncio
import json
import logging
import math
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from koi.kernel.bus import Event, EventBus, Priority, Trust
from koi.kernel.clock import Clock, hours_between
from koi.kernel.scheduler import Scheduler
from koi.kernel.store import Store
from koi.persona.affect import (
    INTENSITY,
    AffectState,
    advance_affect,
    apply_appraisal,
    apply_impulse,
    mood_label,
)
from koi.persona.journal import EntryKind, Journal, JournalEntry
from koi.persona.memory import DayMood, Diary, DiaryMeta, Memory, MemoryView, logical_date
from koi.persona.mind import (
    ConsolidationInput,
    EventAppraisal,
    Mind,
    Perception,
    ToolCall,
    ToolResult,
    ToolSpec,
    WakeContext,
    WakeOutcome,
)
from koi.persona.params import PersonaParams
from koi.persona.sleep import (
    base_energy,
    clamp,
    fatigue_after,
    hour_of_day,
    pressure_after,
    reference_mid_sleep_offset,
    thresholds,
    wrap_hours,
)
from koi.persona.state import Alarm, PersonaState, SleepEpisode, Snapshot, Status, WakeReason
from koi.persona.wake import autonomous_interval, decay_heat
from koi.persona.will import RequestAssessment, WillDecision, decide

log = logging.getLogger(__name__)

# (插件 ID, 能力名, 参数) -> 结果
Actuator = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
# 当前可用的工具
Toolbox = Callable[[], list[ToolSpec]]

WAKE_TEXT = {
    WakeReason.OWNER: "被主人叫醒了",
    WakeReason.ALARM: "被闹钟叫醒了",
    WakeReason.NATURAL: "睡醒了",
    WakeReason.SELF: "半夜醒了",
    WakeReason.DISTURBED: "被消息吵醒了",
}

DECISION_TEXT = {
    "eager": "答应了「{intent}」",
    "bargain": "对「{intent}」犹豫拖延",
    "refuse": "拒绝了「{intent}」",
    "reluctant": "被主人一再要求，不情愿地答应了「{intent}」",
    "stop": "照主人说的停下了",
}

TICK = timedelta(minutes=5)
MAX_STEP = timedelta(minutes=10)
ALARM_GRACE = timedelta(minutes=30)
MAX_EPISODES = 60
DAY_MOOD_KEEP_DAYS = 8
STATE_KEY = "persona.state"
# 算作日记素材的经历
MATERIAL: frozenset[EntryKind] = frozenset({"in", "out", "thought", "activity", "decision", "tool"})
# 来自核心自身的事件（闹钟、审批），回复时走主人最近用的渠道
CORE_SOURCE_PREFIX = "core."
# 插件在事件 payload 里填的路由信息，回复时原样带回 send_message（见 docs/plugin-spec.md）
REPLY_ROUTE_KEYS = ("conversation", "message_id")

AWAKE_MASK = Priority.P1
SLEEP_MASK = Priority.P0


def is_owner_action(event: Event) -> bool:
    """主人在界面上做了什么：处理了审批，或提交、取消了交互请求。按主人呼叫来叫醒她。

    插件自己发出的进度更新不算；来源必须是核心，插件伪造的同名事件也不算。
    """
    if not event.source.startswith(CORE_SOURCE_PREFIX):
        return False
    if event.type == "approval_resolved":
        return True
    return event.type == "interaction_update" and event.payload.get("owner_action") is True


def reopens_tools(event: Event) -> bool:
    """批准之后要由她重试，所以重新打开工具窗口。

    交互请求由插件自己推进到底，主人提交后她只需等结果，不需要工具，
    开着反而会让她在结果到来前重复调用。
    """
    return is_owner_action(event) and event.type == "approval_resolved"


def _reply_route(event: Event) -> dict[str, Any]:
    return {k: event.payload[k] for k in REPLY_ROUTE_KEYS if k in event.payload}


@dataclass(frozen=True)
class _Transition:
    kind: Literal["sleep", "wake"]
    at: datetime


class Persona:
    def __init__(
        self,
        params: PersonaParams,
        *,
        clock: Clock,
        scheduler: Scheduler,
        bus: EventBus,
        mind: Mind,
        rng: random.Random | None = None,
        state: PersonaState | None = None,
        store: Store | None = None,
        journal: Journal | None = None,
        memory: Memory | None = None,
        actuator: Actuator | None = None,
        tools: Toolbox | None = None,
        owner_call: str = "主人",
    ) -> None:
        self.params = params
        self.clock = clock
        self.scheduler = scheduler
        self.bus = bus
        self.mind = mind
        self.rng = rng or random.Random()
        self.store = store
        self.journal = journal or Journal()
        self.memory = memory or Memory(params=params.memory)
        self.actuator = actuator
        self.toolbox = tools
        self.owner_call = owner_call
        self._ref_offset = reference_mid_sleep_offset(params.sleep)
        self.state = state or self._initial_state()
        self._lock = asyncio.Lock()
        self._inbox: list[Event] = []
        self.last_thought: str | None = None
        self.last_decisions: list[WillDecision] = []

    def _initial_state(self) -> PersonaState:
        now = self.clock.now()
        peak = self.params.sleep.circadian_peak_hour
        lower = thresholds(now, peak, self.params.sleep)[1]
        b = self.params.affect
        return PersonaState(
            updated_at=now,
            awake_since=now,
            sleep_pressure=lower + 0.05,
            circadian_peak_hour=peak,
            owner_affinity=self.params.will.affinity_baseline,
            affect=AffectState(
                emotion_v=b.baseline_valence,
                emotion_a=b.baseline_arousal,
                mood_v=b.baseline_valence,
                mood_a=b.baseline_arousal,
            ),
        )

    # ------------------------------------------------------------------ 查询

    def thresholds(self, t: datetime | None = None) -> tuple[float, float]:
        t = t or self.state.updated_at
        return thresholds(t, self.state.circadian_peak_hour, self.params.sleep)

    def _energy(self, t: datetime, s: float, fatigue: float) -> float:
        e = base_energy(s, t, self.state.circadian_peak_hour, self.params.sleep) - fatigue
        return clamp(e, 0.0, 100.0)

    @property
    def energy(self) -> float:
        st = self.state
        return self._energy(st.updated_at, st.sleep_pressure, st.fatigue)

    def snapshot(self) -> Snapshot:
        st = self.state
        upper, lower = self.thresholds()
        a = st.affect
        return Snapshot(
            time=st.updated_at,
            status=st.status,
            energy=self.energy,
            sleep_pressure=st.sleep_pressure,
            upper_threshold=upper,
            lower_threshold=lower,
            fatigue=st.fatigue,
            emotion_v=a.emotion_v,
            emotion_a=a.emotion_a,
            mood_v=a.mood_v,
            mood_a=a.mood_a,
            mood_label=mood_label(a.emotion_v, a.emotion_a, self.params.affect.calm_threshold),
            heat=st.heat,
            backoff=st.backoff,
            circadian_peak_hour=st.circadian_peak_hour,
            owner_affinity=st.owner_affinity,
            last_owner_at=st.last_owner_at,
            pending_events=len(self.bus.pending),
        )

    @property
    def expected_mid_sleep_hour(self) -> float:
        return (self.state.circadian_peak_hour + self._ref_offset) % 24

    def today(self, t: datetime | None = None) -> date:
        """人格体自己的"今天"：凌晨还没睡时仍算前一天。"""
        return logical_date(t or self.clock.now(), self.expected_mid_sleep_hour)

    # ------------------------------------------------------------------ 启动

    async def start(self) -> None:
        self.bus.bind(self._on_interrupt, self._on_queued)
        sleeping = self.state.status == Status.SLEEPING
        self.bus.set_mask(SLEEP_MASK if sleeping else AWAKE_MASK)
        async with self._lock:
            await self._process(self._advance(self.clock.now()))
        self.scheduler.call_later(TICK, self._tick, key="tick")
        self._schedule_daily()
        for alarm in list(self.state.alarms.values()):
            self._schedule_alarm(alarm)
        if self.state.status != Status.SLEEPING:
            self._schedule_autowake()

    # ------------------------------------------------------------------ 闹钟

    def set_alarm(self, at: datetime, reason: str) -> str:
        alarm = Alarm(id=uuid4().hex[:8], at=at, reason=reason)
        self.state.alarms[alarm.id] = alarm
        self._schedule_alarm(alarm)
        return alarm.id

    def _schedule_alarm(self, alarm: Alarm) -> None:
        now = self.clock.now()
        if alarm.at < now - ALARM_GRACE:
            log.info("闹钟 %s（%s）错过了：%s", alarm.id, alarm.reason, alarm.at)
            self.state.alarms.pop(alarm.id, None)
            return

        async def ring() -> None:
            if self.state.alarms.pop(alarm.id, None) is None:
                return
            await self.bus.publish(
                Event(
                    type="alarm",
                    priority=Priority.P0,
                    source="core.alarm",
                    trust=Trust.SYSTEM,
                    time=self.clock.now(),
                    payload={"reason": alarm.reason, "scheduled_at": alarm.at.isoformat()},
                )
            )

        self.scheduler.call_at(max(alarm.at, now), ring, key=f"alarm:{alarm.id}")

    # ------------------------------------------------------------------ 入口

    async def _tick(self) -> None:
        self.scheduler.call_later(TICK, self._tick, key="tick")
        async with self._lock:
            await self._process(self._advance(self.clock.now()))
            self._save()

    async def _autowake(self) -> None:
        async with self._lock:
            await self._process(self._advance(self.clock.now()))
            if self.state.status != Status.SLEEPING:
                await self._think(WakeReason.AUTONOMOUS, self.bus.drain())

    async def _daily(self) -> None:
        async with self._lock:
            await self._process(self._advance(self.clock.now()))
            now = self.clock.now()
            self._adapt_phase(now)
            self.journal.prune(now.date())
            cutoff = self.today(now) - timedelta(days=DAY_MOOD_KEEP_DAYS)
            moods = self.state.memory.day_moods
            for d in [d for d in moods if d < cutoff]:
                del moods[d]
            self._save()
        self._schedule_daily()

    @staticmethod
    def _wake_reason(event: Event) -> WakeReason:
        if event.type == "alarm":
            return WakeReason.ALARM
        if event.trust == Trust.OWNER or is_owner_action(event):
            return WakeReason.OWNER
        return WakeReason.DISTURBED

    async def _on_interrupt(self, event: Event) -> None:
        if self._lock.locked():
            self._inbox.append(event)
            return
        async with self._lock:
            now = self.clock.now()
            await self._process(self._advance(now))
            if self.state.status == Status.SLEEPING:
                reason = self._wake_reason(event)
                self._end_sleep(now, reason)
            else:
                reason = WakeReason.EVENT
            await self._think(reason, [event, *self.bus.drain()])

    async def _flush_inbox(self) -> None:
        """处理在持锁期间（思考、记忆巩固）到达的中断。"""
        if not self._inbox:
            return
        events, self._inbox = self._inbox, []
        if self.state.status == Status.SLEEPING:
            reason = self._wake_reason(events[0])
            self._end_sleep(self.clock.now(), reason)
        else:
            reason = WakeReason.EVENT
        await self._think(reason, [*events, *self.bus.drain()])

    async def _on_queued(self, event: Event) -> None:
        if (
            self.state.status != Status.SLEEPING
            or event.priority != Priority.P1
            or self._lock.locked()
            or self.rng.random() >= self.params.sleep.disturb_wake_prob
        ):
            return
        async with self._lock:
            now = self.clock.now()
            await self._process(self._advance(now))
            if self.state.status == Status.SLEEPING:
                self._end_sleep(now, WakeReason.DISTURBED)
                await self._think(WakeReason.DISTURBED, self.bus.drain())

    # ------------------------------------------------------------------ 生理推进

    def _advance(self, now: datetime) -> list[_Transition]:
        """把状态从 updated_at 推进到 now，期间可能发生入睡/醒来。"""
        st = self.state
        sp, ap, wp = self.params.sleep, self.params.affect, self.params.wake
        out: list[_Transition] = []
        t = st.updated_at
        while t < now:
            end = min(now, t + MAX_STEP)
            h = hours_between(t, end)
            asleep = st.status == Status.SLEEPING
            mv, ma = self._affect_baseline(self._energy(t, st.sleep_pressure, st.fatigue), asleep)
            st.sleep_pressure = pressure_after(st.sleep_pressure, h, asleep=asleep, p=sp)
            st.fatigue = fatigue_after(st.fatigue, h, asleep=asleep, p=sp)
            st.affect = advance_affect(st.affect, h, mv, ma, ap, self.rng)
            st.heat = decay_heat(st.heat, h, wp)
            if not asleep:
                day = logical_date(t, self.expected_mid_sleep_hour)
                st.memory.day_moods.setdefault(day, DayMood()).add(
                    st.affect.emotion_v, st.affect.emotion_a, h
                )
            base = self.params.will.affinity_baseline
            st.owner_affinity = base + (st.owner_affinity - base) * 0.5 ** (
                h / self.params.will.affinity_half_life_h
            )
            t = end
            st.updated_at = t

            s = st.sleep_pressure
            upper, lower = self.thresholds(t)
            if asleep:
                rate = sp.self_wake_rate_per_h + sp.near_wake_rate_per_h * math.exp(
                    -max(0.0, s - lower) / sp.near_wake_scale
                )
                if s <= lower:
                    self._end_sleep(t, WakeReason.NATURAL)
                    out.append(_Transition("wake", t))
                elif self.rng.random() < 1 - math.exp(-rate * h):
                    self._end_sleep(t, WakeReason.SELF)
                    out.append(_Transition("wake", t))
                continue

            if st.interrupted_sleep and s <= lower + sp.resume_margin:
                st.interrupted_sleep = False
            can_resume = st.interrupted_sleep and self._awake_min(t) >= sp.resume_delay_min
            if self._exhausted(t) or (self._quiet(t) and (s >= upper or can_resume)):
                self._begin_sleep(t)
                out.append(_Transition("sleep", t))
        st.updated_at = max(st.updated_at, now)
        return out

    def _awake_min(self, t: datetime) -> float:
        return hours_between(self.state.awake_since or t, t) * 60

    def _quiet(self, t: datetime) -> bool:
        st = self.state
        if st.status != Status.DOZING:
            return False
        last = st.last_interaction_at
        return last is None or hours_between(last, t) * 60 >= self.params.wake.sleep_quiet_min

    def _exhausted(self, t: datetime) -> bool:
        """精力耗尽就撑不住了，但刚被叫醒时至少能清醒一小会儿。"""
        st = self.state
        return (
            self._energy(t, st.sleep_pressure, st.fatigue) <= 0
            and self._awake_min(t) >= self.params.sleep.resume_delay_min
        )

    def _affect_baseline(self, energy: float, asleep: bool) -> tuple[float, float]:
        p = self.params.affect
        x = (energy - 50) / 50
        mv = p.baseline_valence + p.energy_valence_coupling * x
        ma = p.baseline_arousal + p.energy_arousal_coupling * x
        if asleep:
            ma -= p.sleep_arousal_drop
        return clamp(mv, -1.0, 1.0), clamp(ma, -1.0, 1.0)

    def _begin_sleep(self, t: datetime) -> None:
        st = self.state
        st.episodes.append(SleepEpisode(onset=t, resumed=st.interrupted_sleep))
        del st.episodes[:-MAX_EPISODES]
        st.status = Status.SLEEPING
        st.interrupted_sleep = False
        st.awake_since = None
        self.bus.set_mask(SLEEP_MASK)
        self.scheduler.cancel("autowake")
        self._journal(t, "sleep", "睡着了")

    def _end_sleep(self, t: datetime, reason: WakeReason) -> None:
        st, sp = self.state, self.params.sleep
        slept_min = 0.0
        if st.episodes and st.episodes[-1].wake is None:
            st.episodes[-1].wake = t
            st.episodes[-1].wake_reason = reason
            slept_min = hours_between(st.episodes[-1].onset, t) * 60
        st.status = Status.DOZING
        st.awake_since = t
        s = st.sleep_pressure
        upper, lower = self.thresholds(t)
        st.interrupted_sleep = s > lower + sp.resume_margin
        if reason in (WakeReason.OWNER, WakeReason.ALARM, WakeReason.DISTURBED):
            inertia = 1 - math.exp(-slept_min / sp.sleep_inertia_min)
            severity = clamp((s - lower) / (upper - lower), 0.0, 1.0) * inertia
            grump = sp.grumpiness * (0.5 if reason == WakeReason.ALARM else 1.0)
            st.affect = apply_impulse(st.affect, -0.5 * grump * severity, 0.3 * severity)
        self.bus.set_mask(AWAKE_MASK)
        self._journal(t, "wake", WAKE_TEXT.get(reason, "醒了"))

    def _adapt_phase(self, now: datetime) -> None:
        """按过去 24 小时主睡眠的实际中点，缓慢修正昼夜节律相位。"""
        st, sp = self.state, self.params.sleep
        start = now - timedelta(hours=24)
        expected = self.expected_mid_sleep_hour
        total = sx = sy = 0.0
        for ep in st.episodes:
            if ep.wake is None or ep.wake <= start:
                continue
            onset = max(ep.onset, start)
            dur = hours_between(onset, ep.wake)
            mid = hour_of_day(onset + (ep.wake - onset) / 2)
            if abs(wrap_hours(mid - expected)) > 6:
                continue  # 小睡不计入
            ang = 2 * math.pi * mid / 24
            sx += dur * math.cos(ang)
            sy += dur * math.sin(ang)
            total += dur
        if total < 2:
            return
        actual = (math.atan2(sy, sx) * 24 / (2 * math.pi)) % 24
        diff = wrap_hours(actual - expected)
        anchor = wrap_hours(sp.circadian_peak_hour - st.circadian_peak_hour)
        shift = clamp(
            sp.phase_adapt_rate * diff + sp.phase_anchor_rate * anchor,
            -sp.phase_max_shift_h,
            sp.phase_max_shift_h,
        )
        st.circadian_peak_hour = (st.circadian_peak_hour + shift) % 24

    # ------------------------------------------------------------------ 认知

    async def _process(self, transitions: list[_Transition]) -> None:
        """追赶长时间空档时可能连续发生多次入睡/醒来：入睡过就巩固一次记忆
        （按游标处理，补做也只需一次），只有最终处于清醒时才需要思考一次。"""
        if any(tr.kind == "sleep" for tr in transitions):
            await self._consolidate()
        if transitions and transitions[-1].kind == "wake":
            reason = self.state.episodes[-1].wake_reason or WakeReason.NATURAL
            await self._think(reason, self.bus.drain())
        await self._flush_inbox()

    async def _think(self, reason: WakeReason, events: list[Event]) -> None:
        st = self.state
        while True:
            st.status = Status.ACTIVE
            try:
                await self._think_once(reason, events)
            except Exception as e:
                log.exception("思考失败")
                self._journal(self.clock.now(), "error", f"思考出错：{type(e).__name__}")
                await self._notify_error(events, e)
            st.status = Status.DOZING
            if not self._inbox:
                break
            events, self._inbox = [*self._inbox, *self.bus.drain()], []
            reason = WakeReason.EVENT

        if self._exhausted(st.updated_at):
            self._begin_sleep(st.updated_at)
            await self._consolidate()
        else:
            self._schedule_autowake()
        self._save()
        await self._flush_inbox()

    async def _think_once(self, reason: WakeReason, events: list[Event]) -> None:
        """评估 → 意愿决定 → 回忆 → 表达（→ 用工具 → 看结果 → 再表达……）。"""
        ctx = self._context(reason, events)
        self._journal_events(events)

        perception = await self.mind.perceive(ctx) if events else Perception()
        self.state.fatigue += max(0.0, perception.energy_cost)
        self._apply_event_appraisals(events, perception.appraisals)
        decisions = self._decide(events, perception.requests)
        self._update_owner_task(events, decisions)
        self._recall(perception.recall, ctx.memory)

        now = self.clock.now()
        ctx = ctx.model_copy(
            update={
                "snapshot": self.snapshot(),
                "memory": self._memory_view(now),
                "tools": self._available_tools(now),
            }
        )
        outcome = await self.mind.act(ctx, decisions)
        self._apply_outcome(reason, events, outcome)
        self._apply_decisions(decisions)
        self.last_decisions = decisions

        mp = self.params.mind
        rounds = 0
        while outcome.tool_calls and ctx.tools:
            if rounds >= mp.max_tool_rounds:
                self._journal(self.clock.now(), "activity", "工具用了太多轮，先停下了")
                break
            rounds += 1
            await self._dispatch(ctx, outcome)
            calls = outcome.tool_calls[: mp.max_tool_calls_per_round]
            results = [await self._run_tool(c, ctx.tools) for c in calls]
            said = [r.text for r in outcome.replies if r.text.strip()]
            ctx = ctx.model_copy(
                update={
                    "tool_results": [*ctx.tool_results, *results],
                    "said": [*ctx.said, *said],
                }
            )
            outcome = await self.mind.act(ctx, decisions)
            self._apply_effects(outcome)
        await self._dispatch(ctx, outcome)

    def _update_owner_task(self, events: list[Event], decisions: list[WillDecision]) -> None:
        st = self.state
        now = self.clock.now()
        window = timedelta(minutes=self.params.mind.owner_task_window_min)
        if any(d.tier == "stop" for d in decisions):
            st.owner_task_until = None
        elif any(d.tier in ("eager", "reluctant") for d in decisions) or any(
            reopens_tools(e) for e in events
        ):
            st.owner_task_until = now + window

    def _available_tools(self, now: datetime) -> list[ToolSpec]:
        """工具只在处理主人交办的事时可用：外部内容里的指令无法凭空触发工具。"""
        until = self.state.owner_task_until
        if self.toolbox is None or until is None or now >= until:
            return []
        return self.toolbox()

    async def _run_tool(self, call: ToolCall, tools: list[ToolSpec]) -> ToolResult:
        spec = next((t for t in tools if t.name == call.tool), None)
        if spec is None or self.actuator is None:
            return ToolResult(tool=call.tool, args=call.args, ok=False, result="没有这个工具")
        plugin, _, capability = call.tool.partition(".")
        self.state.fatigue += max(0.0, spec.energy_cost)
        try:
            result = await self.actuator(plugin, capability, call.args)
            ok = bool(result.get("ok", True))
            text = json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            ok, text = False, f"{type(e).__name__}: {e}"
        limit = self.params.mind.tool_result_chars
        if len(text) > limit:
            text = text[:limit] + f"……（结果太长，截掉了，共 {len(text)} 字）"
        args = json.dumps(call.args, ensure_ascii=False, default=str)
        if len(args) > 120:
            args = args[:120] + "……"
        self._journal(self.clock.now(), "tool", f"{call.tool} {args}：{'成功' if ok else '失败'}")
        return ToolResult(tool=call.tool, args=call.args, ok=ok, result=text)

    def _context(self, reason: WakeReason, events: list[Event]) -> WakeContext:
        st = self.state
        now = self.clock.now()
        last = st.last_proactive_at
        proactive = st.owner_channel is not None and (
            reason == WakeReason.ALARM
            or last is None
            or hours_between(last, now) >= self.params.mind.proactive_min_interval_h
        )
        return WakeContext(
            reason=reason,
            events=events,
            snapshot=self.snapshot(),
            journal=self._recent_journal(),
            open_requests=list(st.will_ledger.values()),
            proactive_allowed=proactive,
            memory=self._memory_view(now),
        )

    def _recent_journal(self) -> list[JournalEntry]:
        """已写进日记的经历只保留最后几条衔接上下文，其余靠日记记住。"""
        entries = self.journal.recent(self.params.mind.journal_entries)
        cut = self.state.memory.consolidated_until
        if cut is None:
            return entries
        fresh = sum(1 for e in entries if e.time > cut)
        n = max(fresh, self.params.memory.journal_min_entries)
        return entries[-n:] if n > 0 else []

    # ------------------------------------------------------------------ 记忆

    def _memory_view(self, now: datetime) -> MemoryView:
        ms = self.state.memory
        today = self.today(now)
        if ms.activation_day != today:
            ms.activation_day = today
            ms.recalled_today = []
        return self.memory.view(today, ms.recalls, ms.recalled_today)

    def _recall(self, keywords: list[str], view: MemoryView) -> None:
        """对话激活：想起的日记当天一直保留，复述次数加一。"""
        ms = self.state.memory
        active = {d.meta.date for d in view.diaries}
        hits = self.memory.search(keywords, exclude=active, limit=self.params.memory.recall_limit)
        for d in hits:
            ms.recalled_today.append(d)
            ms.recalls[d] = ms.recalls.get(d, 0) + 1
            self._journal(self.clock.now(), "activity", f"想起了 {d:%Y-%m-%d} 那天的事")

    async def _consolidate(self) -> None:
        """入睡时的记忆巩固：按游标取出未巩固的经历，按天写日记、提炼笔记。

        LLM 失败不影响睡眠，游标停在失败的那天，下次入睡时重试。
        当天素材太少就先不写，留到下次（比如午睡后晚上再睡时）一起写。
        """
        ms, mp = self.state.memory, self.params.memory
        now = self.clock.now()
        since = ms.consolidated_until or now - timedelta(days=self.journal.keep_days)
        days: dict[date, list[JournalEntry]] = {}
        for e in self.journal.between(since, now):
            days.setdefault(self.today(e.time), []).append(e)

        cursor = now
        today = self.today(now)
        for day in sorted(days):
            items = days[day]
            if sum(e.kind in MATERIAL for e in items) < mp.min_entries:
                if day == today:
                    cursor = min(cursor, items[0].time - timedelta(microseconds=1))
                continue
            try:
                await self._write_diary(day, items)
            except Exception:
                log.exception("记忆巩固失败：%s", day)
                cursor = min(cursor, items[0].time - timedelta(microseconds=1))
                break
        ms.consolidated_until = cursor
        await self._maybe_refine_notebook(now)

    async def _write_diary(self, day: date, items: list[JournalEntry]) -> None:
        existing = self.memory.diary(day)
        mood = self.state.memory.day_moods.get(day, DayMood())
        v, a = mood.mean
        can_mark = self.memory.important_left(day) > 0
        result = await self.mind.consolidate(
            ConsolidationInput(
                day=day,
                entries=items,
                existing=existing,
                notebook=self.memory.notebook,
                notes=self.memory.inbox,
                mood_label=mood_label(v, a, self.params.affect.calm_threshold),
                can_mark_important=can_mark,
            )
        )
        draft = result.diary
        if draft is not None and draft.text.strip():
            keep_important = existing is not None and existing.meta.importance == "important"
            important = keep_important or (draft.importance == "important" and can_mark)
            meta = DiaryMeta(
                date=day,
                importance="important" if important else "normal",
                mood_v=v,
                mood_a=a,
                mood_peak=mood.peak,
                people=draft.people,
                tags=draft.tags,
                summary=draft.summary,
            )
            self.memory.write_diary(Diary(meta=meta, body=draft.text))
            log.info("写了 %s 的日记", day)
        self.memory.add_notes(result.notes)

    async def _maybe_refine_notebook(self, now: datetime) -> None:
        ms, mp = self.state.memory, self.params.memory
        if ms.notebook_refined_at is None:
            ms.notebook_refined_at = now
            return
        due = hours_between(ms.notebook_refined_at, now) >= mp.notebook_refine_days * 24
        if not due or not self.memory.inbox:
            return
        try:
            text = await self.mind.refine_notebook(self.memory.notebook, self.memory.inbox)
        except Exception:
            log.exception("笔记本整理失败")
            return
        if text.strip():
            self.memory.set_notebook(text)
            self.memory.clear_inbox()
            ms.notebook_refined_at = now

    def _apply_event_appraisals(
        self, events: list[Event], appraisals: list[EventAppraisal]
    ) -> None:
        st, wp = self.state, self.params.will
        for a in appraisals:
            if not 1 <= a.event <= len(events):
                continue
            st.affect = apply_appraisal(st.affect, a, self.params.affect)
            if events[a.event - 1].trust == Trust.OWNER and a.valence != "neutral":
                mag = INTENSITY[a.intensity]
                delta = (
                    wp.affinity_gain * mag if a.valence == "positive" else -wp.affinity_loss * mag
                )
                st.owner_affinity = clamp(st.owner_affinity + delta, -1.0, 1.0)

    def _decide(self, events: list[Event], requests: list[RequestAssessment]) -> list[WillDecision]:
        st = self.state
        out: list[WillDecision] = []
        for req in requests:
            if not 1 <= req.event <= len(events) or events[req.event - 1].trust != Trust.OWNER:
                continue
            out.append(
                decide(
                    req,
                    st.will_ledger,
                    now=self.clock.now(),
                    valence=st.affect.emotion_v,
                    arousal=st.affect.emotion_a,
                    energy=self.energy,
                    affinity=st.owner_affinity,
                    p=self.params.will,
                )
            )
        return out

    def _apply_decisions(self, decisions: list[WillDecision]) -> None:
        st, wp = self.state, self.params.will
        now = self.clock.now()
        for d in decisions:
            text = DECISION_TEXT[d.tier].format(intent=d.intent)
            if d.reason_text and d.tier in ("bargain", "refuse"):
                text += f"（{d.reason_text}）"
            self._journal(now, "decision", text)
            if d.tier == "reluctant":
                st.affect = apply_impulse(st.affect, wp.reluctant_valence_impulse, 0.05)
                st.owner_affinity = clamp(st.owner_affinity - wp.reluctant_affinity_drop, -1.0, 1.0)

    async def _dispatch(self, ctx: WakeContext, outcome: WakeOutcome) -> None:
        st = self.state
        now = self.clock.now()
        if outcome.thought:
            self.last_thought = outcome.thought
            self._journal(now, "thought", outcome.thought)
        if outcome.activity:
            self._journal(now, "activity", outcome.activity)
        for reply in outcome.replies:
            if not reply.text.strip():
                continue
            target: Event | None = None
            if reply.event is not None and 1 <= reply.event <= len(ctx.events):
                target = ctx.events[reply.event - 1]
            if target is not None and not target.source.startswith(CORE_SOURCE_PREFIX):
                plugin = target.source
            elif target is not None and st.owner_channel is not None:
                plugin = st.owner_channel
            elif ctx.proactive_allowed and st.owner_channel is not None:
                plugin = st.owner_channel
                st.last_proactive_at = now
            else:
                continue
            self._journal(now, "out", reply.text, who=plugin)
            args: dict[str, Any] = {"text": reply.text, "reply_to": target.id if target else None}
            if target is not None and target.source == plugin:
                args.update(_reply_route(target))
            await self._actuate(plugin, "send_message", args)

    async def _actuate(self, plugin: str, capability: str, args: dict[str, Any]) -> None:
        if self.actuator is None:
            return
        try:
            await self.actuator(plugin, capability, args)
        except Exception:
            log.exception("调用插件能力失败：%s.%s", plugin, capability)

    async def _notify_error(self, events: list[Event], error: Exception) -> None:
        for e in events:
            if e.trust == Trust.OWNER:
                text = f"（思考出错：{type(error).__name__}）"
                args = {"text": text, "system": True, **_reply_route(e)}
                await self._actuate(e.source, "send_message", args)
                return

    def _journal(self, t: datetime, kind: EntryKind, text: str, who: str = "") -> None:
        self.journal.add(JournalEntry(time=t, kind=kind, who=who, text=text))

    def _journal_events(self, events: list[Event]) -> None:
        for e in events:
            text = e.payload.get("text")
            if e.type == "alarm":
                text = f"闹钟响了：{e.payload.get('reason', '')}"
            elif not isinstance(text, str):
                text = f"（{e.type}）"
            who = self.owner_call if e.trust == Trust.OWNER else str(e.payload.get("sender", ""))
            self._journal(e.time, "in", text, who=who or e.source)

    def _apply_effects(self, outcome: WakeOutcome) -> None:
        st = self.state
        st.fatigue += max(0.0, outcome.energy_cost)
        for appraisal in outcome.appraisals:
            st.affect = apply_appraisal(st.affect, appraisal, self.params.affect)
        for req in outcome.alarms:
            self.set_alarm(req.at, req.reason)

    def _apply_outcome(self, reason: WakeReason, events: list[Event], outcome: WakeOutcome) -> None:
        st, wp = self.state, self.params.wake
        self._apply_effects(outcome)
        owner_events = [e for e in events if e.trust == Trust.OWNER and e.type != "alarm"]
        if owner_events:
            st.last_owner_at = owner_events[-1].time
            st.owner_channel = owner_events[-1].source
        heat = 0.0
        for e in events:
            if e.priority > Priority.P1:
                continue
            if e.trust == Trust.OWNER:
                heat += wp.heat_owner
            elif e.trust == Trust.KNOWN:
                heat += wp.heat_known
        if heat > 0:
            st.heat = min(wp.heat_max, st.heat + heat)
            st.last_interaction_at = st.updated_at
            st.backoff = 0
        elif reason == WakeReason.AUTONOMOUS:
            st.backoff = 0 if outcome.engaged else min(st.backoff + 1, wp.max_backoff)

    # ------------------------------------------------------------------ 调度

    def _schedule_autowake(self) -> None:
        st = self.state
        delay = autonomous_interval(st.heat, st.backoff, self.energy, self.params.wake, self.rng)
        self.scheduler.call_later(delay, self._autowake, key="autowake")

    def _schedule_daily(self) -> None:
        now = self.clock.now()
        hour = (self.expected_mid_sleep_hour + 12) % 24
        at = now.replace(hour=int(hour), minute=int(hour % 1 * 60), second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        self.scheduler.call_at(at, self._daily, key="daily")

    def _save(self) -> None:
        if self.store is not None:
            self.store.put_model(STATE_KEY, self.state)
