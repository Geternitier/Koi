import random
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

from pydantic import BaseModel

from koi.kernel import Event, EventBus, Priority, Scheduler, SimClock, Trust
from koi.kernel.llm import Usage
from koi.persona import Journal, Persona, PersonaParams, PersonaProfile, Status, WakeReason
from koi.persona.llm_mind import ActOutput, LLMMind, PerceiveOutput
from koi.persona.mind import SeenImage
from koi.persona.params import AffectParams, SleepParams
from koi.persona.prompt import system_prompt

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 20, 10, tzinfo=TZ)
PARAMS = PersonaParams(
    sleep=SleepParams(self_wake_rate_per_h=0, near_wake_rate_per_h=0),
    affect=AffectParams(noise=0),
)
PROFILE = PersonaProfile(name="小鲤", owner_call="主人", body="开朗，嘴硬心软。")
USAGE = Usage(
    prompt_tokens=1000, completion_tokens=100, cache_hit_tokens=800, cache_miss_tokens=200
)

Responder = Callable[[type[BaseModel], list[dict[str, str]]], dict[str, Any]]


class FakeLLM:
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.calls: list[tuple[type[BaseModel], list[dict[str, str]]]] = []

    async def chat_json(self, messages, schema, **kwargs):
        self.calls.append((schema, list(messages)))
        return schema.model_validate(self.responder(schema, list(messages))), USAGE


def build(responder: Responder):
    clock = SimClock(T0)
    sched = Scheduler(clock)
    bus = EventBus()
    llm = FakeLLM(responder)
    sent: list[tuple[str, str, dict[str, Any]]] = []

    async def actuator(plugin: str, capability: str, args: dict[str, Any]) -> dict[str, Any]:
        sent.append((plugin, capability, args))
        return {"ok": True}

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


def user_text(messages: list[dict[str, str]]) -> str:
    return messages[-1]["content"]


async def test_refusal_then_reluctant_compliance_after_strong_insistence():
    perceptions = iter(
        [
            {"requests": [{"event": 1, "intent": "整理下载目录", "aversion": "hate"}]},
            {
                "requests": [
                    {
                        "event": 1,
                        "intent": "整理目录",
                        "same_as": "整理下载目录",
                        "aversion": "hate",
                        "insistence": "strong",
                    }
                ]
            },
            {
                "appraisals": [{"event": 1, "valence": "negative", "intensity": "medium"}],
                "requests": [
                    {
                        "event": 1,
                        "intent": "整理",
                        "same_as": "整理下载目录",
                        "aversion": "hate",
                        "insistence": "strong",
                    }
                ],
            },
        ]
    )

    def respond(schema, messages):
        if schema is PerceiveOutput:
            return next(perceptions)
        return {"thought": "唉", "replies": [{"event": 1, "text": "回复"}]}

    persona, sched, bus, llm, sent = build(respond)
    await persona.start()
    persona.state.fatigue = 80  # 很累
    texts = ["帮我整理下载目录", "快去整理！", "我说了，现在就整理"]
    for i, text in enumerate(texts):
        t = T0 + timedelta(minutes=1 + i)
        sched.call_at(t, partial(bus.publish, owner(t, text)))
    affinity_before = persona.state.owner_affinity
    await sched.run_until(T0 + timedelta(minutes=5))

    act_prompts = [user_text(m) for s, m in llm.calls if s is ActOutput]
    tiers = [d.tier for d in persona.last_decisions]
    assert tiers == ["reluctant"]
    assert "拒绝，并结合你现在的心情" in act_prompts[0]
    assert "主要原因：不喜欢做这类事" in act_prompts[0]
    assert "尚未答应的请求" in act_prompts[1]
    assert "不情愿地照做" in act_prompts[2]
    assert persona.state.will_ledger == {}
    assert persona.state.owner_affinity < affinity_before
    assert [(p, c) for p, c, _ in sent] == [("web", "send_message")] * 3
    assert persona.state.owner_channel == "web"
    kinds = [e.kind for e in persona.journal.recent(50)]
    assert kinds.count("decision") == 3 and kinds.count("out") == 3


async def test_perceive_and_act_share_prompt_prefix():
    def respond(schema, messages):
        return {} if schema is PerceiveOutput else {"replies": [{"event": 1, "text": "嗯"}]}

    persona, sched, bus, llm, _ = build(respond)
    await persona.start()
    t = T0 + timedelta(minutes=1)
    sched.call_at(t, partial(bus.publish, owner(t, "在吗")))
    await sched.run_until(t + timedelta(seconds=1))

    (_, perceive), (_, act) = llm.calls[-2:]
    assert perceive[0] == act[0]  # system prompt 完全相同
    p, a = user_text(perceive), user_text(act)
    prefix = p[: p.index("# 此刻")]
    assert a.startswith(prefix) and "在吗" in prefix


async def test_autonomous_wake_skips_perceive_and_respects_proactive_gate():
    def respond(schema, messages):
        assert schema is ActOutput
        return {"engaged": True, "activity": "构思了一个小故事", "replies": [{"text": "嘿"}]}

    persona, sched, _, llm, sent = build(respond)
    await persona.start()
    await sched.run_until(T0 + timedelta(hours=3))
    assert llm.calls and all(s is ActOutput for s, _ in llm.calls)
    # 主人从未出现过，不知道往哪个渠道主动发
    assert sent == []
    assert persona.state.backoff == 0
    assert any(e.kind == "activity" for e in persona.journal.recent(50))


async def test_llm_failure_is_reported_and_life_goes_on():
    def respond(schema, messages):
        raise RuntimeError("network down")

    persona, sched, bus, _, sent = build(respond)
    await persona.start()
    t = T0 + timedelta(minutes=1)
    sched.call_at(t, partial(bus.publish, owner(t, "在吗")))
    await sched.run_until(t + timedelta(seconds=1))
    assert sent and sent[0][2]["system"] is True
    assert persona.state.status == Status.DOZING
    assert sched.scheduled_at("autowake") is not None


async def test_replies_carry_plugin_route_back():
    answers = {"在吗": "在的", "大家好": "你好呀"}

    def respond(schema, messages):
        if schema is PerceiveOutput:
            return {}
        text = user_text(messages)
        block = text[text.index("# 新事件") : text.index("# 此刻")]
        replies = [
            {"event": int(line.split(".")[0]), "text": answers[line.rsplit("：", 1)[1]]}
            for line in block.splitlines()[1:]
            if line[:1].isdigit()
        ]
        return {"replies": replies}

    persona, sched, bus, _, sent = build(respond)
    await persona.start()
    t = T0 + timedelta(minutes=1)
    owner_dm = owner(t, "在吗").model_copy(
        update={"source": "qq", "payload": {"text": "在吗", "conversation": "private:1"}}
    )
    group = Event(
        type="message",
        priority=Priority.P1,
        source="qq",
        trust=Trust.EXTERNAL,
        time=t,
        payload={"text": "大家好", "sender": "阿明", "conversation": "group:9", "message_id": "m7"},
    )
    sched.call_at(t, partial(bus.publish, group))
    sched.call_at(t, partial(bus.publish, owner_dm))
    await sched.run_until(t + timedelta(seconds=1))
    routes = {a["text"]: (p, a.get("conversation"), a.get("message_id")) for p, _, a in sent}
    assert routes == {"在的": ("qq", "private:1", None), "你好呀": ("qq", "group:9", "m7")}


async def test_alarm_from_act_output_is_scheduled():
    def respond(schema, messages):
        if schema is PerceiveOutput:
            return {"requests": [{"event": 1, "intent": "提醒开会"}]}
        return {
            "replies": [{"event": 1, "text": "好"}],
            "alarm": {"at": "2026-10-20T15:00", "reason": "提醒主人开会"},
        }

    persona, sched, bus, _, _ = build(respond)
    await persona.start()
    t = T0 + timedelta(minutes=1)
    sched.call_at(t, partial(bus.publish, owner(t, "三点提醒我开会")))
    await sched.run_until(t + timedelta(seconds=1))
    (alarm,) = persona.state.alarms.values()
    assert alarm.at == datetime(2026, 10, 20, 15, tzinfo=TZ)


class _ImageRejected(Exception):
    status_code = 400


class _VisionLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []

    async def chat_json(self, messages, schema, **kwargs):
        self.calls.append((list(messages), kwargs))
        if isinstance(messages[1]["content"], list):
            raise _ImageRejected("image_url is not supported")
        return schema.model_validate({"replies": []}), USAGE


async def test_vision_rejection_falls_back_to_text_once():
    clock = SimClock(T0)
    llm = _VisionLLM()
    mind = LLMMind(llm, PROFILE, PARAMS.mind)
    persona = Persona(
        PARAMS,
        clock=clock,
        scheduler=Scheduler(clock),
        bus=EventBus(),
        mind=mind,
        journal=Journal(),
    )
    ctx = persona._context(WakeReason.OWNER, [], [])
    image = SeenImage(caption="一张图", media_type="image/png", data=b"\x89PNG\r\n\x1a\n")
    ctx = ctx.model_copy(update={"images": [image]})

    await mind.act(ctx, [])
    assert len(llm.calls) == 2
    assert isinstance(llm.calls[0][0][1]["content"], list)
    assert isinstance(llm.calls[1][0][1]["content"], str)
    assert "没能送到你眼前" in llm.calls[1][0][1]["content"]

    # 记住模型不接受图片，下次不再附图
    await mind.act(ctx, [])
    assert len(llm.calls) == 3
    assert isinstance(llm.calls[2][0][1]["content"], str)


class _RecordingLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat_json(self, messages, schema, **kwargs):
        self.calls.append(kwargs)
        return schema.model_validate({"replies": []}), USAGE


async def test_vision_model_is_used_only_with_images():
    params = PARAMS.mind.model_copy(update={"model": "text-model", "vision_model": "vision-model"})
    llm = _RecordingLLM()
    mind = LLMMind(llm, PROFILE, params)
    clock = SimClock(T0)
    persona = Persona(
        PARAMS,
        clock=clock,
        scheduler=Scheduler(clock),
        bus=EventBus(),
        mind=mind,
        journal=Journal(),
    )
    ctx = persona._context(WakeReason.OWNER, [], [])
    image = SeenImage(caption="一张图", media_type="image/png", data=b"\x89PNG\r\n\x1a\n")
    await mind.act(ctx.model_copy(update={"images": [image]}), [])
    await mind.act(ctx, [])
    assert llm.calls[0]["model"] == "vision-model"
    assert llm.calls[1]["model"] == "text-model"


def test_system_prompt_includes_appearance_and_identity_rules():
    profile = PROFILE.model_copy(update={"appearance": "栗棕色长卷发，圆眼睛。"})
    text = system_prompt(profile)
    assert "# 你的样子" in text
    assert "栗棕色长卷发" in text
    assert "以主人说的为准" in text
    assert "# 你的样子" not in system_prompt(PROFILE)


class _MessageLLM:
    def __init__(self) -> None:
        self.messages: list[list[dict[str, Any]]] = []

    async def chat_json(self, messages, schema, **kwargs):
        self.messages.append(list(messages))
        return schema.model_validate({"replies": []}), USAGE


async def test_self_images_are_attached_only_when_looking_at_images():
    llm = _MessageLLM()
    portrait = SeenImage(
        caption="这是你（头像）", media_type="image/png", data=b"\x89PNG\r\n\x1a\n"
    )
    mind = LLMMind(llm, PROFILE, PARAMS.mind, self_images=[portrait])
    clock = SimClock(T0)
    persona = Persona(
        PARAMS,
        clock=clock,
        scheduler=Scheduler(clock),
        bus=EventBus(),
        mind=mind,
        journal=Journal(),
    )
    ctx = persona._context(WakeReason.OWNER, [], [])
    image = SeenImage(caption="主人发来的图片", media_type="image/png", data=b"\x89PNG\r\n\x1a\n")
    await mind.act(ctx.model_copy(update={"images": [image]}), [])
    content = llm.messages[0][1]["content"]
    assert isinstance(content, list)
    texts = [part.get("text") for part in content if part.get("type") == "text"]
    assert any("下面是你自己的照片" in text for text in texts)
    assert sum(part.get("type") == "image_url" for part in content) == 2

    # 没有新图片时不附自己的照片
    await mind.act(ctx, [])
    assert isinstance(llm.messages[1][1]["content"], str)
