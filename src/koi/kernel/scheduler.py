"""定时调度器：按时间排序的回调优先队列。

实时模式下由 `run_forever` 驱动；模拟模式下由 `run_until` 直接跳到下一个事件。
"""

import asyncio
import contextlib
import heapq
import inspect
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from koi.kernel.clock import Clock, SimClock

Callback = Callable[[], Awaitable[object] | object]


@dataclass(order=True)
class _Entry:
    when: datetime
    seq: int
    callback: Callback = field(compare=False)
    key: str | None = field(default=None, compare=False)
    cancelled: bool = field(default=False, compare=False)


class Scheduler:
    # 宿主机休眠时单调时钟可能停走，实时等待需定期醒来重新读取墙钟
    MAX_REAL_WAIT = timedelta(seconds=30)

    def __init__(
        self, clock: Clock, *, on_error: Callable[[BaseException], None] | None = None
    ) -> None:
        """`on_error` 为空时回调异常直接抛出（模拟与测试）；否则交给它处理并继续运行。"""
        self.clock = clock
        self.on_error = on_error
        self._heap: list[_Entry] = []
        self._keys: dict[str, _Entry] = {}
        self._seq = itertools.count()
        self._changed: asyncio.Event | None = None

    def call_at(self, when: datetime, callback: Callback, *, key: str | None = None) -> None:
        """在 `when` 时执行回调。同一 `key` 只保留最新的一次。"""
        if key is not None:
            self.cancel(key)
        entry = _Entry(when, next(self._seq), callback, key)
        heapq.heappush(self._heap, entry)
        if key is not None:
            self._keys[key] = entry
        if self._changed is not None:
            self._changed.set()

    def call_later(self, delay: timedelta, callback: Callback, *, key: str | None = None) -> None:
        self.call_at(self.clock.now() + delay, callback, key=key)

    def cancel(self, key: str) -> bool:
        entry = self._keys.pop(key, None)
        if entry is None:
            return False
        entry.cancelled = True
        return True

    def scheduled_at(self, key: str) -> datetime | None:
        entry = self._keys.get(key)
        return entry.when if entry is not None else None

    def next_time(self) -> datetime | None:
        entry = self._peek()
        return entry.when if entry is not None else None

    def _peek(self) -> _Entry | None:
        while self._heap and self._heap[0].cancelled:
            heapq.heappop(self._heap)
        return self._heap[0] if self._heap else None

    async def _run(self, entry: _Entry) -> None:
        if entry.key is not None and self._keys.get(entry.key) is entry:
            del self._keys[entry.key]
        try:
            result = entry.callback()
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            if self.on_error is None:
                raise
            self.on_error(e)

    async def run_due(self) -> int:
        count = 0
        while (entry := self._peek()) is not None and entry.when <= self.clock.now():
            heapq.heappop(self._heap)
            await self._run(entry)
            count += 1
        return count

    async def run_until(self, end: datetime) -> None:
        """模拟模式：依次跳到每个到期事件，直到 `end`。"""
        if not isinstance(self.clock, SimClock):
            raise TypeError("run_until 只能用于 SimClock")
        while (entry := self._peek()) is not None and entry.when <= end:
            heapq.heappop(self._heap)
            if entry.when > self.clock.now():
                self.clock.advance_to(entry.when)
            await self._run(entry)
        if end > self.clock.now():
            self.clock.advance_to(end)

    async def run_forever(self) -> None:
        self._changed = asyncio.Event()
        while True:
            await self.run_due()
            timeout = self.MAX_REAL_WAIT.total_seconds()
            nxt = self.next_time()
            if nxt is not None:
                timeout = min(timeout, max(0.0, (nxt - self.clock.now()).total_seconds()))
            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout)
