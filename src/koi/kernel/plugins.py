"""插件宿主：承载插件、把插件事件转换为总线事件、代核心调用插件能力。

进程内插件和进程外插件（`ProcessPlugin`）在这里没有区别。
所有工具调用都经过这里，统一记审计日志，插件无法绕过。
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from koi.kernel.approvals import ApprovalService
from koi.kernel.bus import Event, EventBus, Priority, Trust
from koi.kernel.clock import Clock
from koi.kernel.store import Store
from koi.protocol import (
    Approval,
    ApprovalRequest,
    AuditRecord,
    Capability,
    EventIn,
    Manifest,
    Permission,
    Plugin,
)

log = logging.getLogger(__name__)

_TRUST_RANK = {Trust.EXTERNAL: 0, Trust.KNOWN: 1, Trust.OWNER: 2}
AUDIT_ARGS_CHARS = 2000
AUDIT_SUMMARY_CHARS = 500

StateProvider = Callable[[], dict[str, Any]]


class PluginError(RuntimeError):
    pass


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"……（共 {len(text)} 字）"


class _Context:
    def __init__(self, host: "PluginHost", plugin: Plugin) -> None:
        self._host = host
        self._plugin = plugin

    @property
    def _manifest(self) -> Manifest:
        # 进程外插件初始化后才拿到完整清单，所以每次现取
        return self._plugin.manifest

    @property
    def plugin_id(self) -> str:
        return self._manifest.id

    def _require(self, permission: Permission) -> None:
        if permission not in self._manifest.permissions:
            raise PermissionError(f"插件 {self.plugin_id} 没有 {permission} 权限")

    async def emit(self, event: EventIn) -> None:
        self._host._emit(self._manifest, event)

    async def state(self) -> dict[str, Any]:
        return self._host._state_provider()

    async def request_approval(self, request: ApprovalRequest) -> str:
        return self._host._approvals_or_raise().request(self.plugin_id, request).id

    async def approvals(self) -> list[Approval]:
        self._require("approve")
        return self._host._approvals_or_raise().pending()

    async def resolve_approval(self, approval_id: str, approved: bool) -> None:
        self._require("approve")
        await self._host._approvals_or_raise().resolve(approval_id, approved)

    async def audit(self, limit: int = 50) -> list[AuditRecord]:
        self._require("approve")
        return self._host.store.recent_audit(limit) if self._host.store else []


class PluginHost:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        *,
        approvals: ApprovalService | None = None,
        store: Store | None = None,
        redact: Callable[[str], str] | None = None,
    ) -> None:
        self.bus = bus
        self.clock = clock
        self.approvals = approvals
        self.store = store
        self._redact = redact or (lambda s: s)
        self._plugins: dict[str, Plugin] = {}
        self._state_provider: StateProvider = dict
        self._tasks: set[asyncio.Task[None]] = set()
        if approvals is not None:
            approvals.bind(self._on_approval)

    def add(self, plugin: Plugin) -> None:
        pid = plugin.manifest.id
        if pid in self._plugins:
            raise PluginError(f"插件 ID 重复：{pid}")
        self._plugins[pid] = plugin

    def set_state_provider(self, provider: StateProvider) -> None:
        self._state_provider = provider

    @property
    def plugin_ids(self) -> list[str]:
        return list(self._plugins)

    def get(self, plugin_id: str) -> Plugin | None:
        return self._plugins.get(plugin_id)

    def tools(self) -> list[tuple[str, Capability]]:
        """交给认知循环的工具。"""
        return [
            (pid, cap)
            for pid, plugin in self._plugins.items()
            for cap in plugin.manifest.capabilities
            if cap.tool
        ]

    async def start(self) -> None:
        for plugin in self._plugins.values():
            try:
                await plugin.start(_Context(self, plugin))
                log.info("插件已启动：%s", plugin.manifest.id)
            except Exception:
                log.exception("插件启动失败：%s", plugin.manifest.id)

    async def stop(self) -> None:
        for plugin in reversed(list(self._plugins.values())):
            try:
                await plugin.stop()
            except Exception:
                log.exception("插件停止失败：%s", plugin.manifest.id)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def call(self, plugin_id: str, capability: str, args: dict[str, Any]) -> dict[str, Any]:
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            raise PluginError(f"未知插件：{plugin_id}")
        cap = next((c for c in plugin.manifest.capabilities if c.name == capability), None)
        if cap is None:
            raise PluginError(f"插件 {plugin_id} 没有声明能力 {capability}")
        if not cap.tool:
            return await plugin.call(capability, args)
        started = self.clock.now()
        try:
            result = await asyncio.wait_for(plugin.call(capability, args), cap.timeout_s)
        except TimeoutError:
            self._audit(started, plugin_id, capability, args, False, f"超时（{cap.timeout_s} 秒）")
            raise PluginError(f"{plugin_id}.{capability} 超时") from None
        except Exception as e:
            self._audit(started, plugin_id, capability, args, False, f"{type(e).__name__}: {e}")
            raise
        ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
        self._audit(started, plugin_id, capability, args, ok, _dumps(result))
        return result

    # ------------------------------------------------------------------ 内部

    def _approvals_or_raise(self) -> ApprovalService:
        if self.approvals is None:
            raise PluginError("审批服务未启用")
        return self.approvals

    def _audit(
        self,
        time: datetime,
        plugin_id: str,
        capability: str,
        args: dict[str, Any],
        ok: bool,
        summary: str,
    ) -> None:
        record = AuditRecord(
            time=time,
            plugin=plugin_id,
            capability=capability,
            args=_clip(self._redact(_dumps(args)), AUDIT_ARGS_CHARS),
            ok=ok,
            summary=_clip(self._redact(summary), AUDIT_SUMMARY_CHARS),
        )
        log.info("工具调用 %s.%s ok=%s args=%s", plugin_id, capability, ok, record.args[:200])
        if self.store is not None:
            self.store.append_audit(record)

    async def _on_approval(self, approval: Approval) -> None:
        plugin = self._plugins.get(approval.plugin)
        handler = getattr(plugin, "on_approval", None)
        if handler is not None:
            await handler(approval)

    def _emit(self, manifest: Manifest, incoming: EventIn) -> None:
        trust = Trust(incoming.trust)
        ceiling = Trust(manifest.max_trust)
        if _TRUST_RANK[trust] > _TRUST_RANK[ceiling]:
            log.warning(
                "插件 %s 声明的信任级别 %s 超出上限，降级为 %s", manifest.id, trust, ceiling
            )
            trust = ceiling
        event = Event(
            type=incoming.type,
            priority=Priority(incoming.priority),
            source=manifest.id,
            trust=trust,
            time=incoming.time or self.clock.now(),
            payload=incoming.payload,
        )
        task = asyncio.create_task(self.bus.publish(event))
        self._tasks.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("处理插件事件失败", exc_info=exc)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
