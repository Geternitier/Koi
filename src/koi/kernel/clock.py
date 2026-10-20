"""可注入时钟。核心中所有"现在"都从这里取，以便模拟与测试。"""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now().astimezone()


class SimClock:
    """离散事件模拟用的时钟，只能由调度器向前推进。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock 需要带时区的起始时间")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance_to(self, t: datetime) -> None:
        if t < self._now:
            raise ValueError(f"SimClock 不能回退：{t} < {self._now}")
        self._now = t


def hours_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600
