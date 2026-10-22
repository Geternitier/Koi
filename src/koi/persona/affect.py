"""心情：Russell 情绪环形模型 + 两层（情绪/心境）均值回归动力学。

每个维度（效价 v、唤醒度 a）独立满足：
    de = θe (m - e) dt + σ dW + 冲击
    dm = θm (μ - m) dt + λ (e - m) dt
确定性部分用 2x2 矩阵指数求闭式解。
"""

import math
import random
from typing import Literal

from pydantic import BaseModel

from koi.persona.params import AffectParams

SECTORS = ("开心", "兴奋", "紧张", "烦躁", "低落", "消沉", "疲倦", "满足")
CALM = "平静"

INTENSITY = {"slight": 0.1, "medium": 0.25, "strong": 0.5}


class AffectState(BaseModel):
    emotion_v: float = 0.0
    emotion_a: float = 0.0
    mood_v: float = 0.0
    mood_a: float = 0.0

    @property
    def intensity(self) -> float:
        return math.hypot(self.emotion_v, self.emotion_a)

    @property
    def direction_deg(self) -> float:
        return math.degrees(math.atan2(self.emotion_a, self.emotion_v)) % 360


class Appraisal(BaseModel):
    """LLM 对事件的离散评估，由核心按人格参数换算成冲击。"""

    valence: Literal["positive", "neutral", "negative"]
    arousal: Literal["exciting", "neutral", "calming"] = "neutral"
    intensity: Literal["slight", "medium", "strong"] = "slight"
    type: str = ""


def mood_label(v: float, a: float, calm_threshold: float) -> str:
    if math.hypot(v, a) < calm_threshold:
        return CALM
    angle = math.degrees(math.atan2(a, v)) % 360
    return SECTORS[int(((angle + 22.5) % 360) // 45)]


def _expm2(
    a11: float, a12: float, a21: float, a22: float, t: float
) -> tuple[float, float, float, float]:
    """2x2 矩阵指数 exp(A t)，按特征值合并指数项以避免溢出。"""
    s = (a11 + a22) / 2
    disc = s * s - (a11 * a22 - a12 * a21)
    if disc >= 0:
        q = math.sqrt(disc)
        hi, lo = math.exp((s + q) * t), math.exp((s - q) * t)
        ch = (hi + lo) / 2
        sh = (hi - lo) / (2 * q) if q > 1e-12 else t * math.exp(s * t)
    else:
        q = math.sqrt(-disc)
        es = math.exp(s * t)
        ch, sh = es * math.cos(q * t), es * math.sin(q * t) / q
    return ch + sh * (a11 - s), sh * a12, sh * a21, ch + sh * (a22 - s)


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def advance_affect(
    state: AffectState,
    hours: float,
    baseline_v: float,
    baseline_a: float,
    p: AffectParams,
    rng: random.Random | None = None,
) -> AffectState:
    if hours <= 0:
        return state
    m11, m12, m21, m22 = _expm2(
        -p.theta_emotion_per_h,
        p.theta_emotion_per_h,
        p.mood_coupling_per_h,
        -(p.theta_mood_per_h + p.mood_coupling_per_h),
        hours,
    )

    def step(e: float, m: float, mu: float) -> tuple[float, float]:
        y1, y2 = e - mu, m - mu
        return mu + m11 * y1 + m12 * y2, mu + m21 * y1 + m22 * y2

    ev, mv = step(state.emotion_v, state.mood_v, baseline_v)
    ea, ma = step(state.emotion_a, state.mood_a, baseline_a)
    if rng is not None and p.noise > 0:
        th = p.theta_emotion_per_h
        sd = p.noise * math.sqrt((1 - math.exp(-2 * th * hours)) / (2 * th))
        ev += rng.gauss(0, sd)
        ea += rng.gauss(0, sd)
    return AffectState(emotion_v=_clip(ev), emotion_a=_clip(ea), mood_v=_clip(mv), mood_a=_clip(ma))


def _push(x: float, d: float) -> float:
    """向边界方向的冲击会随接近边界而饱和，保证结果仍在 [-1, 1]。"""
    d = _clip(d)
    return x + d * (1 - x) if d >= 0 else x + d * (1 + x)


def apply_impulse(state: AffectState, dv: float, da: float) -> AffectState:
    return state.model_copy(
        update={"emotion_v": _push(state.emotion_v, dv), "emotion_a": _push(state.emotion_a, da)}
    )


def apply_appraisal(
    state: AffectState, appraisal: Appraisal, p: AffectParams, scale: float = 1.0
) -> AffectState:
    mag = INTENSITY[appraisal.intensity] * scale
    if appraisal.valence == "positive":
        dv = mag * p.sensitivity_positive
    elif appraisal.valence == "negative":
        dv = -mag * p.sensitivity_negative
    else:
        dv = 0.0
    da = {"exciting": 1.0, "neutral": 0.0, "calming": -1.0}[appraisal.arousal]
    return apply_impulse(state, dv, da * mag * p.sensitivity_arousal)
