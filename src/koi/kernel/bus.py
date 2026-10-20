"""事件总线 / 中断控制器。

优先级不高于屏蔽级别的事件立即作为中断交给处理者，其余事件排队，
等处理者下次主动 `drain`。
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class Priority(IntEnum):
    P0 = 0  # 主人呼叫、闹钟
    P1 = 1  # 他人私聊、任务回调
    P2 = 2  # 回复、群消息
    P3 = 3  # 环境信息


class Trust(StrEnum):
    OWNER = "owner"
    KNOWN = "known_person"
    EXTERNAL = "external"
    SYSTEM = "system"


class Event(BaseModel):
    type: str
    priority: Priority
    source: str
    trust: Trust
    time: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    id: str = Field(default_factory=lambda: uuid4().hex)


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self, *, max_pending_ambient: int = 20) -> None:
        self._mask = Priority.P3
        self._pending: list[Event] = []
        self._max_ambient = max_pending_ambient
        self._on_interrupt: Handler | None = None
        self._on_queued: Handler | None = None

    @property
    def mask(self) -> Priority:
        return self._mask

    def set_mask(self, level: Priority) -> None:
        """优先级数值 <= level 的事件会中断处理者。"""
        self._mask = level

    def bind(self, on_interrupt: Handler, on_queued: Handler | None = None) -> None:
        self._on_interrupt = on_interrupt
        self._on_queued = on_queued

    @property
    def pending(self) -> tuple[Event, ...]:
        return tuple(self._pending)

    async def publish(self, event: Event) -> None:
        if self._on_interrupt is not None and event.priority <= self._mask:
            await self._on_interrupt(event)
            return
        self._enqueue(event)
        if self._on_queued is not None:
            await self._on_queued(event)

    def drain(self) -> list[Event]:
        events = sorted(self._pending, key=lambda e: (e.priority, e.time))
        self._pending.clear()
        return events

    def _enqueue(self, event: Event) -> None:
        self._pending.append(event)
        ambient = [e for e in self._pending if e.priority == Priority.P3]
        if len(ambient) > self._max_ambient:
            oldest = min(ambient, key=lambda e: e.time)
            self._pending.remove(oldest)
