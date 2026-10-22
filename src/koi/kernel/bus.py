"""事件总线 / 中断控制器。

优先级数值不大于屏蔽线的事件立即作为中断交给处理者，其余事件排队，
等处理者下次主动 `take` 取一批。P4 永远只排队。

屏蔽线放宽时（比如巩固结束、醒来），队里已经够格的事件由 `set_mask` 交回给调用方。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class Priority(IntEnum):
    P0 = 0  # 系统控制、主人的控制（批准、叫醒）
    P1 = 1  # 重要事件：紧急消息、插件进度、P1 闹钟
    P2 = 2  # 主人的普通消息、P2 闹钟
    P3 = 3  # 和别人的交互、插件通知、画面、P3 闹钟
    P4 = 4  # 环境信息，不会中断


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
# 路由之前看一眼事件，可以改它的优先级（比如紧急字样升级）
Classifier = Callable[[Event], Event]

# 同一优先级里按类型排先后，数值小的先处理；没列出的排在通知之后
TYPE_ORDER: dict[str, int] = {
    "wake_up": 0,
    "approval_resolved": 0,
    "interaction_update": 0,
    "alarm": 1,
    "owner_message": 2,
    "message": 3,
    "mention": 4,
    "comment_reply": 4,
    "plugin_notice": 5,
    "media": 7,
    "feed_update": 8,
    "live_danmaku": 8,
}
UNKNOWN_TYPE_ORDER = 6
# 群聊排在私聊后面
GROUP_PENALTY = 1


def type_order(event: Event) -> int:
    order = TYPE_ORDER.get(event.type, UNKNOWN_TYPE_ORDER)
    if event.payload.get("conversation_type") == "group":
        order += GROUP_PENALTY
    return order


def difficulty(event: Event) -> float:
    """处理起来大概多费劲：正文每 200 字算 1，带画面加 3，带链接加 1。"""
    text = event.payload.get("text")
    score = len(text) / 200 if isinstance(text, str) else 0.0
    if event.payload.get("frames"):
        score += 3
    if isinstance(text, str) and ("http://" in text or "https://" in text):
        score += 1
    return score


def text_size(event: Event) -> int:
    text = event.payload.get("text")
    return len(text) if isinstance(text, str) else 0


@dataclass(frozen=True)
class Batch:
    """每次处理多少：条数、正文字数，以及排序用的等待折算和跨级升级时限。"""

    max_events: int = 8
    max_chars: int = 3000
    aging_min: float = 10.0
    promote_after_min: float = 30.0


def effective_priority(event: Event, now: datetime, batch: Batch) -> int:
    """等太久的升一级，最多升到 P2：主人的消息不会被别人的事件超过。"""
    p = int(event.priority)
    waited = (now - event.time).total_seconds() / 60
    if p >= Priority.P3 and waited >= batch.promote_after_min:
        return p - 1
    return p


def sort_key(event: Event, now: datetime, batch: Batch) -> tuple[int, int, float]:
    waited = max(0.0, (now - event.time).total_seconds() / 60)
    return (
        effective_priority(event, now, batch),
        type_order(event),
        difficulty(event) - waited / batch.aging_min,
    )


class EventBus:
    def __init__(self, *, max_pending_ambient: int = 20) -> None:
        self._mask = Priority.P3
        self._pending: list[Event] = []
        self._max_ambient = max_pending_ambient
        self._on_interrupt: Handler | None = None
        self._on_queued: Handler | None = None
        self._classify: Classifier | None = None

    @property
    def mask(self) -> Priority:
        return self._mask

    def set_mask(self, level: Priority) -> list[Event]:
        """优先级数值 <= level 的事件会中断处理者（P4 除外）。

        返回队里原本被挡住、现在够格的事件，并把它们移出队列，交给调用方处理。
        """
        self._mask = level
        released = [e for e in self._pending if self._interrupts(e)]
        if released:
            self._pending = [e for e in self._pending if not self._interrupts(e)]
        return sorted(released, key=lambda e: (e.priority, type_order(e), e.time))

    def bind(
        self,
        on_interrupt: Handler,
        on_queued: Handler | None = None,
        classify: Classifier | None = None,
    ) -> None:
        self._on_interrupt = on_interrupt
        self._on_queued = on_queued
        self._classify = classify

    @property
    def pending(self) -> tuple[Event, ...]:
        return tuple(self._pending)

    def _interrupts(self, event: Event) -> bool:
        return event.priority < Priority.P4 and event.priority <= self._mask

    async def publish(self, event: Event) -> None:
        if self._classify is not None:
            event = self._classify(event)
        if self._on_interrupt is not None and self._interrupts(event):
            await self._on_interrupt(event)
            return
        self._enqueue(event)
        if self._on_queued is not None:
            await self._on_queued(event)

    def drain(self) -> list[Event]:
        """取走全部排队事件。"""
        events = sorted(self._pending, key=lambda e: (e.priority, type_order(e), e.time))
        self._pending.clear()
        return events

    def take(self, now: datetime, batch: Batch, *, first: list[Event] | None = None) -> list[Event]:
        """取一批：先按（优先级、类型、难度减等待）排序，再按条数和字数截断。

        first 是已经确定要处理的事件（比如刚到的中断），占用名额但一定保留。
        至少会取一条，保证再长的事件也能被处理。
        """
        chosen = list(first or [])
        count = len(chosen)
        chars = sum(text_size(e) for e in chosen)
        rest: list[Event] = []
        for e in sorted(self._pending, key=lambda e: sort_key(e, now, batch)):
            size = text_size(e)
            fits = count < batch.max_events and (count == 0 or chars + size <= batch.max_chars)
            if fits:
                chosen.append(e)
                count += 1
                chars += size
            else:
                rest.append(e)
        self._pending = rest
        return chosen

    def requeue(self, events: list[Event]) -> None:
        """把取出来还没处理的事件放回队里（比如攒着的 P3 遇上她睡着了）。"""
        for e in events:
            self._enqueue(e)

    def ready(self) -> bool:
        """队里有没有现在就够格打断她的事件。"""
        return any(self._interrupts(e) for e in self._pending)

    def _enqueue(self, event: Event) -> None:
        self._pending.append(event)
        ambient = [e for e in self._pending if e.priority == Priority.P4]
        if len(ambient) > self._max_ambient:
            oldest = min(ambient, key=lambda e: e.time)
            self._pending.remove(oldest)
