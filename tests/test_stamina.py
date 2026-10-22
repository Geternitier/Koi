"""动态精力：心情倍率、正面互动回充、负面互动额外消耗、工具成败带来的情绪。"""

import random
from datetime import timedelta

import pytest
from test_persona import T0, make

from koi.persona.params import WakeParams
from koi.persona.wake import autonomous_interval


def test_mood_factor_follows_valence_and_is_clamped():
    persona, *_ = make()
    sp = persona.params.stamina
    persona.state.affect.emotion_v = 0.0
    assert persona._mood_factor() == pytest.approx(1.0)
    persona.state.affect.emotion_v = 0.3
    assert persona._mood_factor() == pytest.approx(1 - sp.mood_relief * 0.3)
    persona.state.affect.emotion_v = 1.0
    assert persona._mood_factor() == sp.factor_min
    persona.state.affect.emotion_v = -1.0
    assert persona._mood_factor() == sp.factor_max


def test_declared_costs_are_scaled_and_mood_changes_the_price():
    persona, *_ = make()
    sp = persona.params.stamina
    persona.state.fatigue = 0
    persona.state.affect.emotion_v = 0.0
    persona._spend(1.0, declared=True)
    assert persona.state.fatigue == pytest.approx(sp.tool_scale)

    persona.state.fatigue = 0
    persona.state.affect.emotion_v = 0.5
    persona._spend(1.0)
    happy = persona.state.fatigue
    persona.state.fatigue = 0
    persona.state.affect.emotion_v = -0.5
    persona._spend(1.0)
    assert happy < 1.0 < persona.state.fatigue
    persona._spend(-5.0)
    assert persona.state.fatigue > 1.0


def test_recharge_is_capped_by_credit_and_floor():
    persona, *_ = make()
    sp = persona.params.stamina
    st = persona.state
    st.fatigue = 0
    st.recharge_credit = 2.0
    persona._recharge(5.0)
    assert st.fatigue == pytest.approx(-2.0)
    assert st.recharge_credit == 0
    persona._recharge(5.0)
    assert st.fatigue == pytest.approx(-2.0)

    st.fatigue = sp.fatigue_floor + 1
    st.recharge_credit = sp.recharge_max
    persona._recharge(sp.recharge_max)
    assert st.fatigue == sp.fatigue_floor


async def test_recharge_credit_refills_over_time():
    persona, _, sched, _, _ = make()
    sp = persona.params.stamina
    await persona.start()
    assert persona.state.recharge_credit == sp.recharge_max
    persona.state.recharge_credit = 0
    await sched.run_until(T0 + timedelta(minutes=30))
    assert persona.state.recharge_credit == pytest.approx(sp.recharge_per_h * 0.5, rel=0.05)
    await sched.run_until(T0 + timedelta(hours=5))
    assert persona.state.recharge_credit == sp.recharge_max


def test_tool_failures_hurt_more_in_a_row_and_success_resets():
    persona, *_ = make()
    sp = persona.params.stamina
    st = persona.state
    st.affect.emotion_v = 0.0
    drops = []
    for _ in range(sp.fail_streak_max + 1):
        before = st.affect.emotion_v
        persona._feel_tool_result(False)
        drops.append(before - st.affect.emotion_v)
    assert drops[0] < drops[1] < drops[2]
    assert drops[-1] <= drops[-2]  # 连败放大有上限
    assert st.tool_fail_streak == sp.fail_streak_max + 1

    before = st.affect.emotion_v
    persona._feel_tool_result(True)
    assert st.affect.emotion_v > before
    assert st.tool_fail_streak == 0


def test_good_mood_wakes_her_sooner():
    p = WakeParams(jitter_low=1.0, jitter_high=1.0)
    calm = autonomous_interval(0, 0, 50, 0.0, p, random.Random(0))
    happy = autonomous_interval(0, 0, 50, 0.8, p, random.Random(0))
    sad = autonomous_interval(0, 0, 50, -0.8, p, random.Random(0))
    assert happy < calm == sad
