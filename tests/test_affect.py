import random

import pytest

from koi.persona.affect import (
    AffectState,
    Appraisal,
    advance_affect,
    apply_appraisal,
    apply_impulse,
    mood_label,
)
from koi.persona.params import AffectParams

P = AffectParams(noise=0.0)


def euler(state: AffectState, hours: float, mu_v: float, mu_a: float, dt: float = 1e-4):
    ev, ea, mv, ma = state.emotion_v, state.emotion_a, state.mood_v, state.mood_a
    for _ in range(int(hours / dt)):
        dev = P.theta_emotion_per_h * (mv - ev)
        dea = P.theta_emotion_per_h * (ma - ea)
        dmv = P.theta_mood_per_h * (mu_v - mv) + P.mood_coupling_per_h * (ev - mv)
        dma = P.theta_mood_per_h * (mu_a - ma) + P.mood_coupling_per_h * (ea - ma)
        ev, ea, mv, ma = ev + dev * dt, ea + dea * dt, mv + dmv * dt, ma + dma * dt
    return ev, ea, mv, ma


def test_closed_form_matches_numerical_integration():
    s = AffectState(emotion_v=0.8, emotion_a=-0.5, mood_v=-0.2, mood_a=0.1)
    got = advance_affect(s, 3.0, 0.1, 0.0, P)
    want = euler(s, 3.0, 0.1, 0.0)
    assert (got.emotion_v, got.emotion_a, got.mood_v, got.mood_a) == pytest.approx(want, abs=1e-3)


def test_long_horizon_converges_to_baseline_without_overflow():
    s = AffectState(emotion_v=1.0, emotion_a=1.0, mood_v=-1.0, mood_a=-1.0)
    got = advance_affect(s, 1e5, 0.15, -0.1, P)
    assert got.emotion_v == pytest.approx(0.15)
    assert got.mood_a == pytest.approx(-0.1)


def test_splitting_steps_is_equivalent():
    s = AffectState(emotion_v=0.6, mood_v=0.3)
    once = advance_affect(s, 2.0, 0.0, 0.0, P)
    twice = advance_affect(advance_affect(s, 1.0, 0.0, 0.0, P), 1.0, 0.0, 0.0, P)
    assert once.emotion_v == pytest.approx(twice.emotion_v)
    assert once.mood_v == pytest.approx(twice.mood_v)


def test_noise_keeps_values_bounded():
    rng = random.Random(0)
    s = AffectState()
    noisy = AffectParams(noise=2.0)
    for _ in range(500):
        s = advance_affect(s, 0.5, 0.0, 0.0, noisy, rng)
        assert -1 <= s.emotion_v <= 1 and -1 <= s.emotion_a <= 1


def test_impulse_saturates_near_boundary():
    s = AffectState(emotion_v=0.9)
    for _ in range(50):
        s = apply_impulse(s, 1.0, 0.0)
    assert s.emotion_v <= 1.0
    s = apply_impulse(AffectState(emotion_v=-0.9), -1.0, 0.0)
    assert s.emotion_v >= -1.0


def test_appraisal_direction_and_sensitivity():
    neg = Appraisal(valence="negative", arousal="exciting", intensity="strong")
    calm = apply_appraisal(AffectState(), neg, AffectParams())
    touchy = apply_appraisal(AffectState(), neg, AffectParams(sensitivity_negative=2.0))
    assert calm.emotion_v < 0 < calm.emotion_a
    assert touchy.emotion_v < calm.emotion_v


@pytest.mark.parametrize(
    ("v", "a", "label"),
    [
        (0.0, 0.0, "平静"),
        (0.5, 0.0, "开心"),
        (0.4, 0.4, "兴奋"),
        (0.0, 0.5, "紧张"),
        (-0.4, 0.4, "烦躁"),
        (-0.5, 0.0, "低落"),
        (-0.4, -0.4, "消沉"),
        (0.0, -0.5, "疲倦"),
        (0.4, -0.4, "满足"),
    ],
)
def test_mood_label_sectors(v, a, label):
    assert mood_label(v, a, 0.12) == label
