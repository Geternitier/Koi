"""审批：插件请求主人批准某个操作，主人在界面上处理。

审批是非阻塞的：插件发起请求后立即返回审批 ID，工具调用以"需要批准"失败；
主人处理后，核心通知发起的插件，并向总线发一个 P0 事件叫醒人格体，由她重试。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import uuid4

from koi.kernel.bus import Event, EventBus, Priority, Trust
from koi.kernel.clock import Clock
from koi.protocol import Approval, ApprovalRequest

log = logging.getLogger(__name__)

APPROVAL_SOURCE = "core.approval"

Notifier = Callable[[Approval], Awaitable[None]]


class ApprovalError(RuntimeError):
    pass


class ApprovalService:
    def __init__(self, bus: EventBus, clock: Clock, *, ttl: timedelta = timedelta(hours=24)):
        self.bus = bus
        self.clock = clock
        self.ttl = ttl
        self._items: dict[str, Approval] = {}
        self._notify: Notifier | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def bind(self, notify: Notifier) -> None:
        self._notify = notify

    def _expire(self) -> None:
        now = self.clock.now()
        for a in list(self._items.values()):
            if a.status == "pending" and now - a.created_at > self.ttl:
                a.status = "expired"
                a.resolved_at = now
            if a.status != "pending" and now - a.created_at > self.ttl * 2:
                del self._items[a.id]

    def request(self, plugin_id: str, req: ApprovalRequest) -> Approval:
        self._expire()
        for a in self._items.values():
            if (a.plugin, a.action, a.detail, a.status) == (
                plugin_id,
                req.action,
                req.detail,
                "pending",
            ):
                return a
        a = Approval(
            id=uuid4().hex[:8],
            plugin=plugin_id,
            action=req.action,
            detail=req.detail,
            created_at=self.clock.now(),
        )
        self._items[a.id] = a
        log.info("审批请求 %s：%s 想要%s（%s）", a.id, plugin_id, a.action, a.detail)
        return a

    def pending(self) -> list[Approval]:
        self._expire()
        return sorted(
            (a for a in self._items.values() if a.status == "pending"), key=lambda a: a.created_at
        )

    def get(self, approval_id: str) -> Approval | None:
        return self._items.get(approval_id)

    async def resolve(self, approval_id: str, approved: bool) -> Approval:
        self._expire()
        a = self._items.get(approval_id)
        if a is None:
            raise ApprovalError(f"没有这个审批：{approval_id}")
        if a.status != "pending":
            raise ApprovalError(f"审批已经处理过了：{a.status}")
        a.status = "approved" if approved else "denied"
        a.resolved_at = self.clock.now()
        log.info("审批 %s：%s", a.id, a.status)
        if self._notify is not None:
            try:
                await self._notify(a)
            except Exception:
                log.exception("通知插件审批结果失败：%s", a.plugin)
        verb = "批准" if approved else "拒绝"
        event = Event(
            type="approval_resolved",
            priority=Priority.P0,
            source=APPROVAL_SOURCE,
            trust=Trust.SYSTEM,
            time=a.resolved_at,
            payload={
                "text": f"主人{verb}了：{a.action}（{a.detail}）",
                "approval_id": a.id,
                "approved": approved,
            },
        )
        # 不等人格体想完，避免界面上的点击卡住
        task = asyncio.create_task(self.bus.publish(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return a
