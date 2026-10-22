"""连续表达：把一段信息拆成多条消息，并在不该说的时候停住。"""

from datetime import timedelta
from functools import partial

from test_llm_mind import T0, build, owner, user_text

from koi.persona.llm_mind import ActOutput, PerceiveOutput


def _texts(sent: list) -> list[str]:
    return [args["text"] for _, capability, args in sent if capability == "send_message"]


def _act_prompts(llm) -> list[str]:
    return [user_text(m) for schema, m in llm.calls if schema is ActOutput]


async def _say(persona, sched, bus, text: str, minutes: int = 1) -> None:
    t = T0 + timedelta(minutes=minutes)
    sched.call_at(t, partial(bus.publish, owner(t, text)))
    await sched.run_until(t + timedelta(seconds=1))
    assert persona.state.last_proactive_at is None


async def test_several_beats_carry_the_rest_of_the_information():
    beats = iter(
        [
            {"replies": [{"event": 1, "text": "好呀"}], "more": True},
            {"replies": [{"event": 1, "text": "昨天那只猫还在"}], "more": True},
            {"replies": [{"event": 1, "text": "它趴在窗台上"}], "more": False},
        ]
    )

    def respond(schema, messages):
        if schema is PerceiveOutput:
            return {"requests": [{"event": 1, "intent": "聊聊天", "aversion": "like"}]}
        return next(beats)

    persona, sched, bus, llm, sent = build(respond)
    await persona.start()
    await _say(persona, sched, bus, "想和你聊聊天")

    assert _texts(sent) == ["好呀", "昨天那只猫还在", "它趴在窗台上"]
    prompts = _act_prompts(llm)
    assert len(prompts) == 3
    assert "# 这次你已经说过的话\n- 好呀" in prompts[1]
    assert "- 昨天那只猫还在" in prompts[2]
    assert [d.tier for d in persona.last_decisions] == ["eager"]


async def test_speech_stops_after_the_round_cap():
    n = 0

    def respond(schema, _messages):
        nonlocal n
        if schema is PerceiveOutput:
            return {}
        n += 1
        return {"replies": [{"event": 1, "text": f"第{n}句"}], "more": True}

    persona, sched, bus, llm, sent = build(respond)
    await persona.start()
    persona.params = persona.params.model_copy(
        update={"mind": persona.params.mind.model_copy(update={"max_speech_rounds": 2})}
    )
    await _say(persona, sched, bus, "跟我说说")

    assert _texts(sent) == ["第1句", "第2句", "第3句"]
    assert len(_act_prompts(llm)) == 3
    assert any("还有话没说完" in e.text for e in persona.journal.recent(20))


async def test_new_owner_message_drops_the_unsent_beat():
    holder: dict = {}

    def respond(schema, messages):
        if schema is PerceiveOutput:
            return {}
        text = user_text(messages)
        if "已经说过" in text:
            return {"replies": [{"event": 1, "text": "不该说"}], "more": False}
        if "等等" in text:
            return {"replies": [{"event": 1, "text": "你说"}], "more": False}
        holder["persona"]._inbox.append(owner(holder["persona"].clock.now(), "等等"))
        return {"replies": [{"event": 1, "text": "好呀"}], "more": True}

    persona, sched, bus, llm, sent = build(respond)
    holder["persona"] = persona
    await persona.start()
    await _say(persona, sched, bus, "想聊聊天")

    assert _texts(sent) == ["好呀", "你说"]
    prompts = _act_prompts(llm)
    assert len(prompts) == 2
    assert all("这次你已经说过的话" not in p for p in prompts)
    assert "等等" in prompts[1]


async def test_blocked_tiers_and_low_mood_do_not_continue():
    def hate(schema, _messages):
        if schema is PerceiveOutput:
            return {"requests": [{"event": 1, "intent": "整理目录", "aversion": "hate"}]}
        return {"replies": [{"event": 1, "text": "不想"}], "more": True}

    persona, sched, bus, llm, sent = build(hate)
    await persona.start()
    await _say(persona, sched, bus, "帮我整理下载目录")
    assert [d.tier for d in persona.last_decisions] == ["bargain"]
    assert _texts(sent) == ["不想"]
    assert len(_act_prompts(llm)) == 1

    def low(schema, _messages):
        if schema is PerceiveOutput:
            return {"requests": [{"event": 1, "intent": "聊聊天", "aversion": "like"}]}
        return {"replies": [{"event": 1, "text": "嗯"}], "more": True}

    persona, sched, bus, llm, sent = build(low)
    await persona.start()
    persona.state.affect.emotion_v = -0.6
    await _say(persona, sched, bus, "想聊聊天", minutes=2)
    assert [d.tier for d in persona.last_decisions] == ["eager"]
    assert _texts(sent) == ["嗯"]
    assert len(_act_prompts(llm)) == 1

    persona.state.affect.emotion_v = persona.params.affect.baseline_valence
    persona.state.fatigue = 10_000
    await _say(persona, sched, bus, "再聊会儿", minutes=3)
    assert persona.energy < persona.params.mind.continue_min_energy
    assert [d.tier for d in persona.last_decisions] == ["eager"]
    assert _texts(sent) == ["嗯", "嗯"]
    assert len(_act_prompts(llm)) == 2


async def test_autonomous_wake_does_not_continue():
    def respond(schema, _messages):
        assert schema is ActOutput
        return {"more": True, "activity": "发呆", "replies": [{"text": "嘿"}]}

    persona, sched, _, llm, sent = build(respond)
    await persona.start()
    await sched.run_until(T0 + timedelta(hours=3))
    prompts = _act_prompts(llm)
    assert prompts
    assert all("这次你已经说过的话" not in p for p in prompts)
    assert sent == []
