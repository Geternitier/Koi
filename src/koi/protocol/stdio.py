"""插件侧运行器：把一个 Plugin 对象挂到 stdio 上，作为独立进程运行。

    if __name__ == "__main__":
        serve_stdio(MyPlugin())

stdout 专用于协议：启动后 fd 1 会被重定向到 stderr，插件里的 print 和
子进程继承的输出都进 stderr，由核心记入日志。
插件如果实现了 `configure(settings: dict)`，会在 start 之前收到核心侧的设置。
"""

import asyncio
import os
import sys
import threading
from typing import Any, BinaryIO

from koi.protocol import (
    PROTOCOL_VERSION,
    Approval,
    ApprovalRequest,
    AuditRecord,
    EventIn,
    Interaction,
    InteractionRequest,
    MemorySnapshot,
    Plugin,
)
from koi.protocol.rpc import METHOD_NOT_FOUND, RpcError, RpcPeer


class RemoteContext:
    """PluginContext 的远端实现：每个方法都是一次对核心的 JSON-RPC 调用。"""

    def __init__(self, peer: RpcPeer, plugin_id: str) -> None:
        self._peer = peer
        self._plugin_id = plugin_id

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    async def emit(self, event: EventIn) -> None:
        await self._peer.notify("koi/event", event.model_dump(mode="json"))

    async def state(self) -> dict[str, Any]:
        return await self._peer.request("koi/state")

    async def memory_view(self) -> MemorySnapshot:
        return MemorySnapshot.model_validate(await self._peer.request("koi/memory"))

    async def request_approval(self, request: ApprovalRequest) -> str:
        result = await self._peer.request("koi/approval/request", request.model_dump())
        return str(result["id"])

    async def approvals(self) -> list[Approval]:
        return [Approval.model_validate(a) for a in await self._peer.request("koi/approvals")]

    async def resolve_approval(self, approval_id: str, approved: bool) -> None:
        await self._peer.request("koi/approval/resolve", {"id": approval_id, "approved": approved})

    async def audit(self, limit: int = 50) -> list[AuditRecord]:
        rows = await self._peer.request("koi/audit", {"limit": limit})
        return [AuditRecord.model_validate(r) for r in rows]

    async def request_interaction(self, request: InteractionRequest) -> str:
        result = await self._peer.request("koi/interaction/request", request.model_dump())
        return str(result["id"])

    async def update_interaction(self, interaction_id: str, message: str) -> None:
        await self._peer.request(
            "koi/interaction/update", {"id": interaction_id, "message": message}
        )

    async def close_interaction(self, interaction_id: str, ok: bool, message: str = "") -> None:
        await self._peer.request(
            "koi/interaction/close", {"id": interaction_id, "ok": ok, "message": message}
        )

    async def interactions(self) -> list[Interaction]:
        rows = await self._peer.request("koi/interactions")
        return [Interaction.model_validate(r) for r in rows]

    async def submit_interaction(self, interaction_id: str, values: dict[str, str]) -> None:
        await self._peer.request("koi/interaction/submit", {"id": interaction_id, "values": values})

    async def cancel_interaction(self, interaction_id: str) -> None:
        await self._peer.request("koi/interaction/cancel", {"id": interaction_id})

    async def get_secret(self, name: str) -> str | None:
        result = await self._peer.request("koi/secret/get", {"name": name})
        return result.get("value")

    async def set_secret(self, name: str, value: str) -> None:
        await self._peer.request("koi/secret/set", {"name": name, "value": value})

    async def delete_secret(self, name: str) -> None:
        await self._peer.request("koi/secret/delete", {"name": name})


def serve_stdio(plugin: Plugin) -> None:
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    asyncio.run(_serve(plugin, sys.stdin.buffer, out))


async def _serve(plugin: Plugin, stdin: BinaryIO, out: BinaryIO) -> None:
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[bytes] = asyncio.Queue()
    write_lock = threading.Lock()
    done = asyncio.Event()
    started = False

    def reader() -> None:
        # 守护线程阻塞读取 stdin，避免 Windows 上异步管道的兼容问题
        for line in iter(stdin.readline, b""):
            loop.call_soon_threadsafe(lines.put_nowait, line)
        loop.call_soon_threadsafe(lines.put_nowait, b"")

    async def send(data: bytes) -> None:
        with write_lock:
            out.write(data)
            out.flush()

    async def handle(method: str, params: dict[str, Any]) -> Any:
        nonlocal started
        match method:
            case "initialize":
                configure = getattr(plugin, "configure", None)
                if configure is not None:
                    configure(params.get("settings") or {})
                await plugin.start(RemoteContext(peer, str(params.get("plugin_id", ""))))
                started = True
                return {
                    "protocol_version": PROTOCOL_VERSION,
                    "manifest": plugin.manifest.model_dump(mode="json"),
                }
            case "ping":
                return {}
            case "tools/list":
                return {"tools": [c.model_dump() for c in plugin.manifest.capabilities]}
            case "tools/call":
                return await plugin.call(str(params["name"]), params.get("arguments") or {})
            case "koi/approval/resolved":
                await plugin.on_approval(Approval.model_validate(params))
                return None
            case "koi/interaction/changed":
                handler = getattr(plugin, "on_interaction", None)
                if handler is not None:
                    interaction = Interaction.model_validate(params["interaction"])
                    await handler(interaction, params.get("values"))
                return None
            case "koi/shutdown":
                done.set()
                return {}
            case _:
                raise RpcError(METHOD_NOT_FOUND, f"未知方法：{method}")

    peer = RpcPeer(send, handle, name="core")
    threading.Thread(target=reader, daemon=True).start()

    async def pump() -> None:
        while line := await lines.get():
            await peer.feed(line)
        done.set()  # stdin 关闭：核心已经不在了

    pump_task = asyncio.create_task(pump())
    await done.wait()
    await peer.wait_idle()
    if started:
        await plugin.stop()
    pump_task.cancel()
    peer.close()
