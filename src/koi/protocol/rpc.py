"""JSON-RPC 2.0 对等端：双方都可以发请求和通知，消息按行分隔（与 MCP 的 stdio 传输一致）。

方法一览（核心 → 插件）：
  initialize {protocol_version, plugin_id, settings} -> {manifest}
  ping -> {}
  tools/list -> {tools}
  tools/call {name, arguments} -> 结果对象
  koi/approval/resolved {Approval}（通知）
  koi/shutdown -> {}
（插件 → 核心）：
  koi/event {EventIn}（通知）
  koi/state -> 状态对象
  koi/approval/request {action, detail} -> {id}
  koi/approvals -> [Approval]
  koi/approval/resolve {id, approved} -> {}
  koi/audit {limit} -> [AuditRecord]
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

Handler = Callable[[str, dict[str, Any]], Awaitable[Any]]
Sender = Callable[[bytes], Awaitable[None]]


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class RpcPeer:
    def __init__(self, send: Sender, handler: Handler, *, name: str = "peer") -> None:
        self._send = send
        self._handler = handler
        self.name = name
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self.closed = False

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        if self.closed:
            raise ConnectionError(f"{self.name}：连接已断开")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            await self._write(msg)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self.closed:
            raise ConnectionError(f"{self.name}：连接已断开")
        await self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def feed(self, line: bytes) -> None:
        """处理收到的一行消息。请求在独立任务中并发处理，不阻塞读取。"""
        if not line.strip():
            return
        try:
            msg = json.loads(line)
        except ValueError:
            log.warning("%s：无法解析的消息：%r", self.name, line[:200])
            return
        if not isinstance(msg, dict):
            return
        if "method" in msg:
            self._spawn(self._serve(msg))
        elif "id" in msg:
            fut = self._pending.get(msg["id"])
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"] or {}
                code = err.get("code", INTERNAL_ERROR)
                fut.set_exception(RpcError(code, err.get("message", ""), err.get("data")))
            else:
                fut.set_result(msg.get("result"))

    async def wait_idle(self, timeout: float = 5.0) -> None:
        """等待正在处理的请求写完响应（不含调用者自身所在的任务）。"""
        current = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not current]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(f"{self.name}：连接已断开"))
        for t in self._tasks:
            t.cancel()

    # ------------------------------------------------------------------ 内部

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(self, msg: dict[str, Any]) -> None:
        rid = msg.get("id")
        params = msg.get("params") or {}
        try:
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, "params 必须是对象")
            result = await self._handler(str(msg["method"]), params)
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "result": result}
        except RpcError as e:
            reply = {"jsonrpc": "2.0", "id": rid, "error": _error(e.code, e.message, e.data)}
        except Exception as e:
            if rid is None:
                log.exception("%s：处理通知 %s 失败", self.name, msg.get("method"))
                return
            reply = {
                "jsonrpc": "2.0",
                "id": rid,
                "error": _error(INTERNAL_ERROR, f"{type(e).__name__}: {e}"),
            }
        if rid is None or self.closed:
            return
        with contextlib.suppress(ConnectionError, OSError):
            await self._write(reply)

    async def _write(self, msg: dict[str, Any]) -> None:
        data = json.dumps(msg, ensure_ascii=False, default=str) + "\n"
        await self._send(data.encode("utf-8"))


def _error(code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return err
