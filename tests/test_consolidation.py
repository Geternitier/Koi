"""记忆巩固与日记激活在人格体中的端到端行为（LLMMind + 假 LLM）。"""

import inspect
import random
from datetime import date, datetime, timedelta, timezone
from functools import partial
from typing import Any

from koi.kernel import Event, EventBus, Priority, Scheduler, SimClock, Trust
from koi.kernel.llm import Usage
from koi.persona import (
    Consolidation,
    Diary,
    DiaryMeta,
    Journal,
    Persona,
    PersonaParams,
    PersonaProfile,
    WakeReason,
)
from koi.persona.llm_mind import ActOutput, LLMMind, NotebookOutput, PerceiveOutput
from koi.persona.params import AffectParams, MemoryParams, SleepParams

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 20, 10, tzinfo=TZ)
DAY = date(2026, 10, 20)
PARAMS = PersonaParams(
    sleep=SleepParams(self_wake_rate_per_h=0, near_wake_rate_per_h=0),
    affect=AffectParams(noise=0),
    memory=MemoryParams(journal_min_entries=2, sample_k=0),
)
PROFILE = PersonaProfile(name="小鲤", owner_call="主人", body="开朗，嘴硬心软。")
USAGE = Usage(prompt_tokens=100, completion_tokens=10)

DIARY = {
    "diary": {
        "text": "今天主人让我帮他冲咖啡，我又不会冲……",
        "summary": "被叫去冲咖啡",
        "importance": "important",
        "people": ["主人"],
        "tags": ["咖啡"],
    },
    "notes": ["主人早上离不开咖啡"],
}


class FakeLLM:
    def __init__(self, responder) -> None:
        self.responder = responder
        self.calls: list[tuple[type, list[dict[str, str]]]] = []

    async def chat_json(self, messages, schema, **kwargs):
        self.calls.append((schema, list(messages)))
        data = self.responder(schema, list(messages))
        if inspect.isawaitable(data):
            data = await data
        return schema.model_validate(data), USAGE

    def prompts(self, schema) -> list[str]:
        return [m[-1]["content"] for s, m in self.calls if s is schema]


def default_respond(schema, messages) -> dict[str, Any]:
    if schema is PerceiveOutput:
        return {}
    if schema is ActOutput:
        if "（没有新事件）" in messages[-1]["content"]:
            return {}
        return {"thought": "嗯", "replies": [{"event": 1, "text": "好呀"}]}
    if schema is Consolidation:
        return DIARY
    if schema is NotebookOutput:
        return {"notebook": "## 主人\n- 早上离不开咖啡"}
    raise AssertionError(schema)


def build(responder=default_respond):
    clock = SimClock(T0)
    sched = Scheduler(clock)
    bus = EventBus()
    llm = FakeLLM(responder)
    sent: list[dict[str, Any]] = []

    async def actuator(plugin: str, capability: str, args: dict[str, Any]) -> dict[str, Any]:
        sent.append(args)
        return {}

    persona = Persona(
        PARAMS,
        clock=clock,
        scheduler=sched,
        bus=bus,
        mind=LLMMind(llm, PROFILE, PARAMS.mind),
        rng=random.Random(0),
        journal=Journal(),
        actuator=actuator,
    )
    return persona, sched, bus, llm, sent


def owner(t: datetime, text: str) -> Event:
    return Event(
        type="owner_message",
        priority=Priority.P0,
        source="web",
        trust=Trust.OWNER,
        time=t,
        payload={"text": text},
    )


def chat(sched: Scheduler, bus: EventBus, start: datetime, texts: list[str]) -> None:
    for i, text in enumerate(texts):
        t = start + timedelta(minutes=2 * i)
        sched.call_at(t, partial(bus.publish, owner(t, text)))


def section(prompt: str, title: str) -> str:
    start = prompt.index(f"# {title}")
    end = prompt.find("\n# ", start + 1)
    return prompt[start : end if end != -1 else None]


async def test_diary_written_at_bedtime_and_used_the_next_day():
    persona, sched, bus, llm, _ = build()
    await persona.start()
    chat(sched, bus, T0 + timedelta(minutes=1), ["早", "帮我冲杯咖啡", "谢啦"])
    await sched.run_until(T0 + timedelta(hours=22))  # 次日 8 点
    assert persona.state.episodes[-1].wake is not None

    (prompt,) = llm.prompts(Consolidation)
    assert "# 2026-10-20 周二 这一天的经历" in prompt
    assert "帮我冲杯咖啡" in prompt

    d = persona.memory.diary(DAY)
    assert d is not None and d.body.startswith("今天主人让我")
    assert d.meta.importance == "important"
    assert d.meta.tags == ["咖啡"] and d.meta.mood_peak > 0
    assert persona.memory.inbox == ["主人早上离不开咖啡"]
    cursor = persona.state.memory.consolidated_until
    assert cursor is not None and cursor >= persona.state.episodes[-1].onset

    morning = T0 + timedelta(hours=22, minutes=1)
    chat(sched, bus, morning, ["早上好"])
    await sched.run_until(morning + timedelta(seconds=1))
    act = llm.prompts(ActOutput)[-1]
    assert "今天主人让我帮他冲咖啡" in section(act, "日记")
    assert "早上离不开咖啡" in section(act, "笔记本")
    # 写进日记的经历不再原样塞进 prompt，只留最后几条衔接
    recent = section(act, "最近的经历")
    assert "帮我冲杯咖啡" not in recent and "睡醒了" in recent


async def test_too_little_material_is_kept_for_later():
    def respond(schema, messages):
        if schema is ActOutput:
            return {}
        return default_respond(schema, messages)

    persona, sched, bus, llm, _ = build(respond)
    await persona.start()
    first = T0 + timedelta(minutes=1)
    chat(sched, bus, first, ["在吗"])
    await sched.run_until(T0 + timedelta(hours=22))
    assert llm.prompts(Consolidation) == []
    assert persona.memory.diary(DAY) is None
    cursor = persona.state.memory.consolidated_until
    assert cursor is not None and cursor < first


async def test_failed_consolidation_is_retried_next_time():
    attempts: list[str] = []

    def respond(schema, messages):
        if schema is Consolidation:
            attempts.append(messages[-1]["content"])
            if len(attempts) == 1:
                raise RuntimeError("network down")
        return default_respond(schema, messages)

    persona, sched, bus, _, _ = build(respond)
    await persona.start()
    chat(sched, bus, T0 + timedelta(minutes=1), ["早", "帮我冲杯咖啡", "谢啦"])
    await sched.run_until(T0 + timedelta(hours=22))
    assert len(attempts) == 1 and persona.memory.diary(DAY) is None
    assert persona.state.episodes[-1].wake is not None  # 照常睡觉、醒来

    await sched.run_until(T0 + timedelta(days=1, hours=22))
    assert len(attempts) == 2 and "帮我冲杯咖啡" in attempts[1]
    assert persona.memory.diary(DAY) is not None


async def test_message_during_consolidation_wakes_her_afterwards():
    persona, sched, bus, llm, sent = build()

    async def respond(schema, messages):
        if schema is Consolidation and not any(
            "睡了吗" in e.text for e in persona.journal.recent(50)
        ):
            await bus.publish(owner(persona.clock.now(), "睡了吗"))
        return default_respond(schema, messages)

    llm.responder = respond
    await persona.start()
    chat(sched, bus, T0 + timedelta(minutes=1), ["早", "帮我冲杯咖啡", "谢啦"])
    await sched.run_until(T0 + timedelta(hours=22))

    woken = [ep for ep in persona.state.episodes if ep.wake_reason == WakeReason.OWNER]
    assert woken, "巩固期间到达的消息应在巩固结束后处理"
    assert any("睡了吗" in p for p in llm.prompts(ActOutput))
    assert len(sent) == 4


async def test_conversation_recalls_old_diary():
    old = DAY - timedelta(days=100)

    def respond(schema, messages):
        if schema is PerceiveOutput:
            return {"recall": ["西湖"]}
        return default_respond(schema, messages)

    persona, sched, bus, llm, _ = build(respond)
    persona.memory.write_diary(
        Diary(
            meta=DiaryMeta(date=old, tags=["西湖"], summary="去西湖"), body="和主人去西湖看荷花。"
        )
    )
    await persona.start()
    chat(sched, bus, T0 + timedelta(minutes=1), ["还记得去西湖那次吗"])
    await sched.run_until(T0 + timedelta(minutes=2))

    (perceive,) = llm.prompts(PerceiveOutput)
    act = llm.prompts(ActOutput)[-1]
    assert "看荷花" not in perceive
    assert "## 刚才想起的" in act and "看荷花" in act
    ms = persona.state.memory
    assert ms.recalls == {old: 1} and ms.recalled_today == [old]
    assert any(e.text.startswith("想起了") for e in persona.journal.recent(20))

    # 当天一直保留，不重复计数
    chat(sched, bus, T0 + timedelta(minutes=5), ["荷花好看吗"])
    await sched.run_until(T0 + timedelta(minutes=6))
    assert "看荷花" in llm.prompts(PerceiveOutput)[-1]
    assert ms.recalls == {old: 1}


async def test_notebook_refined_weekly():
    persona, sched, bus, llm, _ = build()
    persona.state.memory.notebook_refined_at = T0 - timedelta(days=8)
    persona.memory.add_notes(["主人不吃香菜"])
    await persona.start()
    chat(sched, bus, T0 + timedelta(minutes=1), ["早", "帮我冲杯咖啡", "谢啦"])
    await sched.run_until(T0 + timedelta(hours=22))

    (prompt,) = llm.prompts(NotebookOutput)
    assert "主人不吃香菜" in prompt and "主人早上离不开咖啡" in prompt
    assert persona.memory.notebook == "## 主人\n- 早上离不开咖啡"
    assert persona.memory.inbox == []
    refined = persona.state.memory.notebook_refined_at
    assert refined is not None and refined > T0
