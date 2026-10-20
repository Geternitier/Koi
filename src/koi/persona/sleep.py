"""睡眠双过程模型（Borbély）：睡眠压力 S 与昼夜节律 C。

所有更新都是闭式解，可以接受任意大的时间步长。
"""

import math
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from koi.persona.params import SleepParams


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def wrap_hours(h: float) -> float:
    """把小时差归一化到 [-12, 12)。"""
    return (h + 12) % 24 - 12


def hour_of_day(t: datetime) -> float:
    return t.hour + t.minute / 60 + t.second / 3600


def circadian(t: datetime, peak_hour: float, p: SleepParams) -> float:
    return p.circadian_amplitude * math.cos(2 * math.pi * (hour_of_day(t) - peak_hour) / 24)


def thresholds(t: datetime, peak_hour: float, p: SleepParams) -> tuple[float, float]:
    """返回 (入睡阈值, 自然醒阈值)。"""
    c = circadian(t, peak_hour, p)
    return p.upper_base + c, p.lower_base + c


def pressure_after(s: float, hours: float, *, asleep: bool, p: SleepParams) -> float:
    if hours <= 0:
        return s
    if asleep:
        return s * math.exp(-hours / p.tau_sleep_h)
    return 1 - (1 - s) * math.exp(-hours / p.tau_wake_h)


def fatigue_after(f: float, hours: float, *, asleep: bool, p: SleepParams) -> float:
    if hours <= 0:
        return f
    tau = p.fatigue_tau_sleep_h if asleep else p.fatigue_tau_awake_h
    return f * math.exp(-hours / tau)


def base_energy(s: float, t: datetime, peak_hour: float, p: SleepParams) -> float:
    """由 S 与 C 决定的基础精力：刚自然醒时为 100，硬撑到余量耗尽时为 0。"""
    upper, lower = thresholds(t, peak_hour, p)
    top = upper + p.stay_up_margin
    return 100 * clamp((top - s) / (top - lower), 0.0, 1.0)


@lru_cache(maxsize=32)
def reference_mid_sleep_offset(p: SleepParams) -> float:
    """无干扰时，主睡眠中点相对节律峰值的偏移（小时）。

    用于判断实际作息相对"本性"是提前还是推后了。
    """
    step = timedelta(minutes=5)
    h = step.total_seconds() / 3600
    peak = p.circadian_peak_hour
    t = datetime(2000, 1, 1, 8, tzinfo=UTC)
    s = thresholds(t, peak, p)[1] + 0.05
    asleep = False
    onset: datetime | None = None
    mids: list[float] = []
    for _ in range(int(12 * 24 / h)):
        s = pressure_after(s, h, asleep=asleep, p=p)
        t += step
        upper, lower = thresholds(t, peak, p)
        if not asleep and s >= upper:
            asleep, onset = True, t
        elif asleep and s <= lower:
            asleep = False
            assert onset is not None
            mids.append(hour_of_day(onset + (t - onset) / 2))
    if len(mids) < 4:
        raise ValueError("睡眠参数无法形成稳定的昼夜作息")
    return wrap_hours(mids[-1] - peak)
