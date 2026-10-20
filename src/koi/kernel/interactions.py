"""主人交互请求：插件请主人扫码、填手机号或验证码，由主人使用的界面展示和提交。

- 只展示给主人：只有带 owner_ui 权限的界面插件能看到和提交。
- 主人填写的值只转给发起的插件，核心不保存；敏感值交给 `on_sensitive`（加入脱敏）。
- 进度以 `interaction_update` 事件告诉人格体，事件文字里永远没有填写的值。
  主人的操作（提交、取消）算主人在跟进她交办的事，插件的进度更新不算。
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import uuid4

from koi.kernel.bus import Event, EventBus, Priority, Trust
from koi.kernel.clock import Clock
from koi.protocol import Interaction, InteractionRequest, InteractionStatus

log = logging.getLogger(__name__)

INTERACTION_SOURCE = "core.interaction"
MAX_PENDING_PER_PLUGIN = 3
MIN_EXPIRES_S = 30.0
MAX_EXPIRES_S = 1800.0
MAX_VALUE_CHARS = 200
OPEN: frozenset[InteractionStatus] = frozenset({"pending", "submitted"})

Notifier = Callable[[Interaction, dict[str, str] | None], Awaitable[None]]


class InteractionError(RuntimeError):
    pass


class InteractionService:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        *,
        on_sensitive: Callable[[str, str], None] | None = None,
    ) -> None:
        self.bus = bus
        self.clock = clock
        self._on_sensitive = on_sensitive
        self._items: dict[str, Interaction] = {}
        self._notify: Notifier | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def bind(self, notify: Notifier) -> None:
        self._notify = notify

    # ------------------------------------------------------------------ 插件侧

    def request(self, plugin_id: str, req: InteractionRequest) -> Interaction:
        self._expire()
        if req.kind == "form":
            if not req.fields:
                raise InteractionError("表单至少要有一个字段")
            if len({f.name for f in req.fields}) != len(req.fields):
                raise InteractionError("字段名重复")
            for f in req.fields:
                if f.pattern is not None:
                    try:
                        re.compile(f.pattern)
                    except re.error as e:
                        raise InteractionError(f"字段 {f.name} 的格式无效：{e}") from None
        elif not (req.qr or req.url or req.message):
            raise InteractionError("展示请求至少要有二维码、链接或说明之一")
        if req.url is not None and not re.match(r"https?://", req.url, re.IGNORECASE):
            raise InteractionError("链接只能是 http 或 https")
        # 同一件事还在等主人处理时，重复的请求合并成一个
        for a in self._items.values():
            if (a.plugin, a.kind, a.title, a.status) == (plugin_id, req.kind, req.title, "pending"):
                return a
        open_count = sum(
            1 for a in self._items.values() if a.plugin == plugin_id and a.status in OPEN
        )
        if open_count >= MAX_PENDING_PER_PLUGIN:
            raise InteractionError(f"未处理的请求太多了（最多 {MAX_PENDING_PER_PLUGIN} 个）")
        now = self.clock.now()
        expires = min(max(req.expires_in_s, MIN_EXPIRES_S), MAX_EXPIRES_S)
        a = Interaction(
            id=uuid4().hex[:8],
            plugin=plugin_id,
            kind=req.kind,
            title=req.title,
            message=req.message,
            qr=req.qr,
            url=req.url,
            fields=req.fields,
            created_at=now,
            expires_at=now + timedelta(seconds=expires),
            updated_at=now,
        )
        self._items[a.id] = a
        log.info("交互请求 %s：%s 请主人处理「%s」", a.id, plugin_id, a.title)
        return a

    async def update(self, plugin_id: str, interaction_id: str, message: str) -> Interaction:
        a = self._own(plugin_id, interaction_id)
        a.message = message
        a.updated_at = self.clock.now()
        self._publish(a, Priority.P1, f"（{a.plugin}）{a.title}：{message}", owner_action=False)
        return a

    async def close(
        self, plugin_id: str, interaction_id: str, ok: bool, message: str = ""
    ) -> Interaction:
        a = self._own(plugin_id, interaction_id)
        a.status = "done" if ok else "failed"
        a.message = message
        a.updated_at = self.clock.now()
        outcome = "成功了" if ok else "失败了"
        text = f"（{a.plugin}）{a.title}{outcome}" + (f"：{message}" if message else "")
        self._publish(a, Priority.P1, text, owner_action=False)
        return a

    # ------------------------------------------------------------------ 主人侧

    def pending(self) -> list[Interaction]:
        self._expire()
        return sorted(
            (a for a in self._items.values() if a.status == "pending"), key=lambda a: a.created_at
        )

    def get(self, interaction_id: str) -> Interaction | None:
        return self._items.get(interaction_id)

    async def submit(self, interaction_id: str, values: dict[str, str]) -> Interaction:
        self._expire()
        a = self._pending_or_raise(interaction_id)
        if a.kind != "form":
            raise InteractionError("这个请求不需要填写")
        clean: dict[str, str] = {}
        for f in a.fields:
            value = str(values.get(f.name, "")).strip()
            if not value:
                raise InteractionError(f"「{f.label}」没有填")
            if len(value) > MAX_VALUE_CHARS:
                raise InteractionError(f"「{f.label}」太长了")
            if f.pattern is not None and not re.fullmatch(f.pattern, value):
                raise InteractionError(f"「{f.label}」格式不对")
            clean[f.name] = value
        for f in a.fields:
            if f.sensitive and self._on_sensitive is not None:
                self._on_sensitive(f"{a.plugin}.{f.name}", clean[f.name])
        a.status = "submitted"
        a.updated_at = self.clock.now()
        log.info("交互请求 %s：主人已提交", a.id)
        # 先发主人的动作，再交给插件：插件在回调里就可能报告结果
        labels = "、".join(f.label for f in a.fields)
        text = f"主人填好了「{a.title}」（{labels}），插件会接着处理，等它的结果就好"
        self._publish(a, Priority.P0, text, owner_action=True)
        await self._deliver(a, clean)
        return a

    async def cancel(self, interaction_id: str) -> Interaction:
        a = self._items.get(interaction_id)
        if a is None or a.status not in OPEN:
            raise InteractionError("这个请求已经结束了")
        a.status = "cancelled"
        a.updated_at = self.clock.now()
        self._publish(a, Priority.P0, f"主人取消了「{a.title}」", owner_action=True)
        await self._deliver(a, None)
        return a

    # ------------------------------------------------------------------ 内部

    def _own(self, plugin_id: str, interaction_id: str) -> Interaction:
        a = self._items.get(interaction_id)
        if a is None or a.plugin != plugin_id:
            raise InteractionError(f"没有这个交互请求：{interaction_id}")
        if a.status not in OPEN:
            raise InteractionError(f"交互请求已经结束了：{a.status}")
        return a

    def _pending_or_raise(self, interaction_id: str) -> Interaction:
        a = self._items.get(interaction_id)
        if a is None:
            raise InteractionError(f"没有这个请求：{interaction_id}")
        if a.status != "pending":
            raise InteractionError(
                "这个请求已经处理过了" if a.status != "expired" else "已经过期了"
            )
        return a

    def _expire(self) -> None:
        now = self.clock.now()
        for a in list(self._items.values()):
            if a.status in OPEN and now >= a.expires_at:
                a.status = "expired"
                a.updated_at = now
                self._spawn(self._deliver(a, None))
                self._publish(a, Priority.P2, f"「{a.title}」过期了", owner_action=False)
            elif a.status not in OPEN and now - a.updated_at > timedelta(hours=1):
                del self._items[a.id]

    async def _deliver(self, a: Interaction, values: dict[str, str] | None) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(a.model_copy(), values)
        except Exception:
            log.exception("通知插件交互结果失败：%s", a.plugin)

    def _publish(
        self, a: Interaction, priority: Priority, text: str, *, owner_action: bool
    ) -> None:
        event = Event(
            type="interaction_update",
            priority=priority,
            source=INTERACTION_SOURCE,
            trust=Trust.SYSTEM,
            time=a.updated_at,
            payload={
                "text": text,
                "interaction_id": a.id,
                "plugin": a.plugin,
                "status": a.status,
                "owner_action": owner_action,
            },
        )
        # 不等人格体想完，避免界面上的提交卡住
        self._spawn(self.bus.publish(event))

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
