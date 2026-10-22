"""插件宿主：承载插件、把插件事件转换为总线事件、代核心调用插件能力。

进程内插件和进程外插件（`ProcessPlugin`）在这里没有区别。
所有工具调用都经过这里，统一记审计日志，插件无法绕过。
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from koi.kernel.approvals import ApprovalService
from koi.kernel.bus import Event, EventBus, Priority, Trust
from koi.kernel.clock import Clock
from koi.kernel.interactions import InteractionService
from koi.kernel.media import (
    MAX_FRAMES,
    MAX_MEDIA_ENERGY,
    MEDIA_TEXT_CHARS,
    MediaError,
    read_frame,
    relative_posix,
    resolve_frame,
)
from koi.kernel.secrets import Redactor, SecretStore
from koi.kernel.store import Store
from koi.protocol import (
    AppearanceView,
    Approval,
    ApprovalRequest,
    AuditRecord,
    Capability,
    EventIn,
    ImageData,
    Interaction,
    InteractionRequest,
    Manifest,
    MediaFrame,
    MemorySnapshot,
    Permission,
    Plugin,
)

log = logging.getLogger(__name__)

_TRUST_RANK = {Trust.EXTERNAL: 0, Trust.KNOWN: 1, Trust.OWNER: 2}
AUDIT_ARGS_CHARS = 2000
AUDIT_SUMMARY_CHARS = 500

StateProvider = Callable[[], dict[str, Any]]
MemoryProvider = Callable[[], dict[str, Any]]
AppearanceProvider = Callable[[], AppearanceView]
# 图片 ID -> 图片，没有这张时为 None
ImageProvider = Callable[[str], ImageData | None]


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

    async def memory_view(self) -> MemorySnapshot:
        self._require("owner_ui")
        if self._host._memory_provider is None:
            raise PluginError("记忆服务未启用")
        return MemorySnapshot.model_validate(self._host._memory_provider())

    async def appearance(self) -> AppearanceView:
        self._require("appearance")
        if self._host._appearance_provider is None:
            return AppearanceView(name="")
        return self._host._appearance_provider()

    async def image(self, image_id: str) -> ImageData:
        self._require("appearance")
        provider = self._host._image_provider
        image = provider(image_id) if provider is not None else None
        if image is None:
            raise PluginError(f"没有这张图：{image_id}")
        return image

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

    async def request_interaction(self, request: InteractionRequest) -> str:
        self._require("interact")
        return self._host._interactions_or_raise().request(self.plugin_id, request).id

    async def update_interaction(self, interaction_id: str, message: str) -> None:
        self._require("interact")
        await self._host._interactions_or_raise().update(self.plugin_id, interaction_id, message)

    async def close_interaction(self, interaction_id: str, ok: bool, message: str = "") -> None:
        self._require("interact")
        svc = self._host._interactions_or_raise()
        await svc.close(self.plugin_id, interaction_id, ok, message)

    async def interactions(self) -> list[Interaction]:
        self._require("owner_ui")
        return self._host._interactions_or_raise().pending()

    async def submit_interaction(self, interaction_id: str, values: dict[str, str]) -> None:
        self._require("owner_ui")
        await self._host._interactions_or_raise().submit(interaction_id, values)

    async def cancel_interaction(self, interaction_id: str) -> None:
        self._require("owner_ui")
        await self._host._interactions_or_raise().cancel(interaction_id)

    def _secret_key(self, name: str) -> str:
        if name not in self._manifest.secrets:
            raise PermissionError(f"插件 {self.plugin_id} 没有登记凭据 {name}")
        if self._host.secrets is None:
            raise PluginError("凭据服务未启用")
        return plugin_secret_name(self.plugin_id, name)

    async def get_secret(self, name: str) -> str | None:
        key = self._secret_key(name)
        assert self._host.secrets is not None
        value = self._host.secrets.get(key)
        return value.get_secret_value() if value is not None else None

    async def set_secret(self, name: str, value: str) -> None:
        key = self._secret_key(name)
        assert self._host.secrets is not None
        self._host.secrets.set(key, value)
        if self._host.redactor is not None:
            self._host.redactor.add(key, value)
        log.info("插件 %s 保存了凭据 %s", self.plugin_id, name)

    async def delete_secret(self, name: str) -> None:
        key = self._secret_key(name)
        assert self._host.secrets is not None
        self._host.secrets.delete(key)

    async def offer_media(
        self, text: str, frames: list[MediaFrame], *, energy_cost: float = 0.0
    ) -> None:
        self._host.offer_media(self.plugin_id, text, frames, energy_cost=energy_cost)


def plugin_secret_name(plugin_id: str, name: str) -> str:
    return f"plugin.{plugin_id}.{name}"


class PluginHost:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        *,
        approvals: ApprovalService | None = None,
        interactions: InteractionService | None = None,
        store: Store | None = None,
        secrets: SecretStore | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.bus = bus
        self.clock = clock
        self.approvals = approvals
        self.interactions = interactions
        self.store = store
        self.secrets = secrets
        self.redactor = redactor
        self._redact: Callable[[str], str] = redactor or (lambda s: s)
        self._plugins: dict[str, Plugin] = {}
        self._state_provider: StateProvider = dict
        self._memory_provider: MemoryProvider | None = None
        self._appearance_provider: AppearanceProvider | None = None
        self._image_provider: ImageProvider | None = None
        self._work_dirs: dict[str, Path] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        if approvals is not None:
            approvals.bind(self._on_approval)
        if interactions is not None:
            interactions.bind(self._on_interaction)

    def add(self, plugin: Plugin) -> None:
        pid = plugin.manifest.id
        if pid in self._plugins:
            raise PluginError(f"插件 ID 重复：{pid}")
        self._plugins[pid] = plugin
        # 插件凭据从一开始就参与脱敏，不等插件读取
        for name in plugin.manifest.secrets:
            key = plugin_secret_name(pid, name)
            if self.secrets is not None:
                self.secrets.register(key)
                value = self.secrets.get(key)
                if value is not None and self.redactor is not None:
                    self.redactor.add(key, value.get_secret_value())
        cwd = getattr(getattr(plugin, "spec", None), "cwd", None)
        if cwd:
            self.set_work_dir(pid, Path(cwd))

    def set_state_provider(self, provider: StateProvider) -> None:
        self._state_provider = provider

    def set_memory_provider(self, provider: MemoryProvider) -> None:
        self._memory_provider = provider

    def set_appearance_provider(self, appearance: AppearanceProvider, image: ImageProvider) -> None:
        self._appearance_provider = appearance
        self._image_provider = image

    def set_work_dir(self, plugin_id: str, path: Path) -> None:
        """插件进程的工作目录。画面只能从这里读。"""
        self._work_dirs[plugin_id] = path

    def offer_media(
        self,
        plugin_id: str,
        text: str,
        frames: list[MediaFrame],
        *,
        energy_cost: float = 0.0,
    ) -> None:
        """校验画面路径后发一条 media 事件。payload 只有说明和相对路径。"""
        if plugin_id not in self._plugins:
            raise PluginError(f"未知插件：{plugin_id}")
        root = self._work_dirs.get(plugin_id)
        if root is None:
            raise PluginError(f"插件 {plugin_id} 没有工作目录，不能提交画面")
        if len(frames) > MAX_FRAMES:
            raise PluginError(f"一次最多 {MAX_FRAMES} 张画面")
        stored: list[dict[str, Any]] = []
        for frame in frames:
            try:
                path = resolve_frame(root, frame.path)
                read_frame(path)
                stored.append({"path": relative_posix(root, path), "t": frame.t})
            except MediaError as e:
                raise PluginError(str(e)) from e
        summary = _clip(text.strip(), MEDIA_TEXT_CHARS)
        if not summary and not stored:
            raise PluginError("没有可看的内容")
        try:
            cost = float(energy_cost)
        except (TypeError, ValueError) as e:
            raise PluginError("精力消耗无效") from e
        event = Event(
            type="media",
            priority=Priority.P2,
            source=plugin_id,
            trust=Trust.EXTERNAL,
            time=self.clock.now(),
            payload={
                "text": summary,
                "frames": stored,
                "energy_cost": max(0.0, min(cost, MAX_MEDIA_ENERGY)),
            },
        )
        task = asyncio.create_task(self.bus.publish(event))
        self._tasks.add(task)
        task.add_done_callback(self._on_done)

    def load_frames(
        self, plugin_id: str, frames: list[dict[str, Any]]
    ) -> list[tuple[float, bytes]]:
        """按事件里的相对路径读 JPEG。文件不在或路径不合法时跳过，不抛错。"""
        root = self._work_dirs.get(plugin_id)
        if root is None:
            return []
        loaded: list[tuple[float, bytes]] = []
        for frame in frames[:MAX_FRAMES]:
            rel = frame.get("path")
            if not isinstance(rel, str):
                continue
            try:
                data = read_frame(resolve_frame(root, rel))
                loaded.append((float(frame.get("t") or 0), data))
            except (MediaError, TypeError, ValueError, OSError):
                continue
        return loaded

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

    def _interactions_or_raise(self) -> InteractionService:
        if self.interactions is None:
            raise PluginError("交互服务未启用")
        return self.interactions

    async def _on_interaction(
        self, interaction: Interaction, values: dict[str, str] | None
    ) -> None:
        plugin = self._plugins.get(interaction.plugin)
        handler = getattr(plugin, "on_interaction", None)
        if handler is not None:
            await handler(interaction, values)

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
