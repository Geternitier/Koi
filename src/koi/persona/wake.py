"""生活时间的自动唤醒：交互热度 × 退避 × 精力 × 随机抖动。"""

import random
from datetime import timedelta

from koi.persona.params import WakeParams


def decay_heat(heat: float, hours: float, p: WakeParams) -> float:
    if hours <= 0:
        return heat
    return heat * 0.5 ** (hours * 60 / p.heat_half_life_min)


def autonomous_interval(
    heat: float, backoff: int, energy: float, p: WakeParams, rng: random.Random
) -> timedelta:
    g = 0.5 + max(0.0, min(100.0, energy)) / 100
    minutes = (
        p.base_interval_min
        * 2**backoff
        / (1 + p.heat_alpha * heat)
        / g
        * rng.uniform(p.jitter_low, p.jitter_high)
    )
    return timedelta(minutes=min(max(minutes, p.min_interval_min), p.max_interval_min))
