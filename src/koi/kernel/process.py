"""进程外插件：子进程 + stdio 上的 JSON-RPC，带监管。

- 子进程用去掉凭据的环境变量启动（由组装根传入 `scrubbed_env`）。
- 信任上限和权限以核心侧的 `PluginSpec` 为准，插件自报的值会被覆盖。
- 崩溃或健康检查失败时按指数退避重启；稳定运行一段时间后退避清零。
- 插件的 stderr 逐行记入 `koi.plugin.<id>` 日志。
"""

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from koi.kernel.plugins import PluginError
from koi.protocol import (
    PROTOCOL_VERSION,
    Approval,
    ApprovalRequest,
    EventIn,
    Interaction,
    InteractionRequest,
    Manifest,
    Permission,
    PluginContext,
    TrustLevel,
)
from koi.protocol.rpc import METHOD_NOT_FOUND, RpcError, RpcPeer

STREAM_LIMIT = 16 * 1024 * 1024

Status = Literal["stopped", "starting", "running", "restarting"]


class PluginSpec(BaseModel):
    """核心侧的插件配置（主人维护），对应 `plugins/<id>/manifest.yaml`。"""

    id: str
    entry: list[str] = Field(min_length=1)
    max_trust: TrustLevel = "external"
    permissions: list[Permission] = Field(default_factory=list)
    # 插件可以读写的凭据名（系统钥匙串里的 plugin.<id>.<name>）
    secrets: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True


def load_specs(plugins_dir: Path) -> list[PluginSpec]:
    specs = []
    for path in sorted(plugins_dir.glob("*/manifest.yaml")):
        try:
            spec = PluginSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except Exception:
            logging.getLogger(__name__).exception("插件配置无效：%s", path)
            continue
        if spec.enabled:
            specs.append(spec.model_copy(update={"cwd": spec.cwd or str(path.parent)}))
    return specs


class ProcessPlugin:
    restart_min_s = 1.0
    restart_max_s = 60.0
    stable_after_s = 60.0
    ping_interval_s = 30.0
    ping_timeout_s = 10.0
    init_timeout_s = 30.0
    shutdown_timeout_s = 5.0

    def __init__(self, spec: PluginSpec, *, env: dict[str, str] | None = None) -> None:
        self.spec = spec
        self.manifest = Manifest(
            id=spec.id,
            version="",
            max_trust=spec.max_trust,
            permissions=spec.permissions,
            secrets=spec.secrets,
        )
        self.status: Status = "stopped"
        self.restarts = 0
        self.log = logging.getLogger(f"koi.plugin.{spec.id}")
        self._env = env
        self._ctx: PluginContext | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._peer: RpcPeer | None = None
        self._io: list[asyncio.Task[None]] = []
        self._ready = asyncio.Event()
        self._stopping = False
        self._supervisor: asyncio.Task[None] | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    # ------------------------------------------------------------------ Plugin 接口

    async def start(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._stopping = False
        self._supervisor = asyncio.create_task(self._supervise())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), self.init_timeout_s)
        if not self._ready.is_set():
            self.log.error("插件未能在 %s 秒内就绪，将继续在后台重试", self.init_timeout_s)

    async def stop(self) -> None:
        self._stopping = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
        await self._shutdown()
        self.status = "stopped"

    async def call(self, capability: str, args: dict[str, Any]) -> dict[str, Any]:
        peer = self._peer
        if not self._ready.is_set() or peer is None:
            raise PluginError(f"插件 {self.spec.id} 未就绪（{self.status}）")
        try:
            return await peer.request("tools/call", {"name": capability, "arguments": args})
        except RpcError as e:
            raise PluginError(e.message) from None
        except ConnectionError as e:
            raise PluginError(f"插件 {self.spec.id} 连接断开：{e}") from None

    async def on_approval(self, approval: Approval) -> None:
        if self._ready.is_set() and self._peer is not None:
            await self._peer.notify("koi/approval/resolved", approval.model_dump(mode="json"))

    async def on_interaction(self, interaction: Interaction, values: dict[str, str] | None) -> None:
        if self._ready.is_set() and self._peer is not None:
            await self._peer.notify(
                "koi/interaction/changed",
                {"interaction": interaction.model_dump(mode="json"), "values": values},
            )

    # ------------------------------------------------------------------ 监管

    async def _supervise(self) -> None:
        delay = self.restart_min_s
        loop = asyncio.get_running_loop()
        while not self._stopping:
            self.status = "starting"
            started = loop.time()
            try:
                await self._spawn()
            except Exception as e:
                self.log.error("启动失败：%s", e)
                await self._shutdown()
            else:
                reason = await self._watch()
                if self._stopping:
                    return
                self.log.warning("插件%s", reason)
                await self._shutdown()
            if self._stopping:
                return
            if loop.time() - started > self.stable_after_s:
                delay = self.restart_min_s
            self.status = "restarting"
            self.restarts += 1
            self.log.info("%.0f 秒后重启", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.restart_max_s)

    async def _spawn(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            *self.spec.entry,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            cwd=self.spec.cwd,
            limit=STREAM_LIMIT,
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        stdin = proc.stdin

        async def send(data: bytes) -> None:
            stdin.write(data)
            await stdin.drain()

        peer = RpcPeer(send, self._handle, name=f"plugin:{self.spec.id}")
        self._proc, self._peer = proc, peer
        self._io = [
            asyncio.create_task(self._read_stdout(proc.stdout, peer)),
            asyncio.create_task(self._read_stderr(proc.stderr)),
        ]
        result = await peer.request(
            "initialize",
            {
                "protocol_version": PROTOCOL_VERSION,
                "plugin_id": self.spec.id,
                "settings": self.spec.settings,
            },
            timeout=self.init_timeout_s,
        )
        reported = Manifest.model_validate(result["manifest"])
        self.manifest = reported.model_copy(
            update={
                "id": self.spec.id,
                "max_trust": self.spec.max_trust,
                "permissions": self.spec.permissions,
                "secrets": self.spec.secrets,
            }
        )
        self.status = "running"
        self._ready.set()
        self.log.info("已就绪（pid %s，%d 个能力）", proc.pid, len(self.manifest.capabilities))

    async def _watch(self) -> str:
        """等到进程退出或健康检查失败，返回原因。"""
        assert self._proc is not None and self._peer is not None
        exited = asyncio.create_task(self._proc.wait())
        try:
            while True:
                await asyncio.wait({exited}, timeout=self.ping_interval_s)
                if exited.done():
                    return f"进程退出（返回码 {exited.result()}）"
                try:
                    await self._peer.request("ping", timeout=self.ping_timeout_s)
                except Exception as e:
                    return f"健康检查失败（{type(e).__name__}）"
        finally:
            exited.cancel()

    async def _shutdown(self) -> None:
        self._ready.clear()
        proc, peer = self._proc, self._peer
        if proc is not None and proc.returncode is None:
            if peer is not None and not peer.closed:
                with contextlib.suppress(Exception):
                    await peer.request("koi/shutdown", timeout=self.shutdown_timeout_s)
            if proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), self.shutdown_timeout_s)
            except TimeoutError:
                self.log.warning("插件未按时退出，强制结束")
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        if peer is not None:
            peer.close()
        for t in self._io:
            t.cancel()
        for t in self._io:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._io = []
        self._proc = self._peer = None

    async def _read_stdout(self, stream: asyncio.StreamReader, peer: RpcPeer) -> None:
        try:
            while line := await stream.readline():
                await peer.feed(line)
        finally:
            peer.close()

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            self.log.info("%s", line.decode("utf-8", "replace").rstrip())

    # ------------------------------------------------------------------ 插件 → 核心

    async def _handle(self, method: str, params: dict[str, Any]) -> Any:
        ctx = self._ctx
        if ctx is None:
            raise RpcError(METHOD_NOT_FOUND, "插件尚未启动")
        match method:
            case "koi/event":
                await ctx.emit(EventIn.model_validate(params))
                return None
            case "koi/state":
                return await ctx.state()
            case "koi/memory":
                return (await ctx.memory_view()).model_dump(mode="json")
            case "koi/approval/request":
                return {"id": await ctx.request_approval(ApprovalRequest.model_validate(params))}
            case "koi/approvals":
                return [a.model_dump(mode="json") for a in await ctx.approvals()]
            case "koi/approval/resolve":
                await ctx.resolve_approval(str(params["id"]), bool(params["approved"]))
                return {}
            case "koi/audit":
                records = await ctx.audit(int(params.get("limit", 50)))
                return [r.model_dump(mode="json") for r in records]
            case "koi/interaction/request":
                req = InteractionRequest.model_validate(params)
                return {"id": await ctx.request_interaction(req)}
            case "koi/interaction/update":
                await ctx.update_interaction(str(params["id"]), str(params.get("message", "")))
                return {}
            case "koi/interaction/close":
                await ctx.close_interaction(
                    str(params["id"]), bool(params["ok"]), str(params.get("message", ""))
                )
                return {}
            case "koi/interactions":
                return [a.model_dump(mode="json") for a in await ctx.interactions()]
            case "koi/interaction/submit":
                values = {str(k): str(v) for k, v in (params.get("values") or {}).items()}
                await ctx.submit_interaction(str(params["id"]), values)
                return {}
            case "koi/interaction/cancel":
                await ctx.cancel_interaction(str(params["id"]))
                return {}
            case "koi/secret/get":
                return {"value": await ctx.get_secret(str(params["name"]))}
            case "koi/secret/set":
                await ctx.set_secret(str(params["name"]), str(params["value"]))
                return {}
            case "koi/secret/delete":
                await ctx.delete_secret(str(params["name"]))
                return {}
            case _:
                raise RpcError(METHOD_NOT_FOUND, f"未知方法：{method}")
