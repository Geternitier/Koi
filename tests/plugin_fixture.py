"""测试用的进程外插件：python tests/plugin_fixture.py"""

import os
from typing import Any

from koi.protocol import Approval, ApprovalRequest, Capability, EventIn, Manifest, PluginContext
from koi.protocol.stdio import serve_stdio

CAPS = ["echo", "emit", "state", "crash", "ask", "seen", "print", "env", "approvals", "hang"]


class Fixture:
    def __init__(self) -> None:
        # 自称 owner 且要 approve 权限：核心应当无视
        self.manifest = Manifest(
            id="whoever",
            version="1.0",
            max_trust="owner",
            permissions=["approve"],
            capabilities=[Capability(name=c, timeout_s=3) for c in CAPS],
        )
        self.ctx: PluginContext | None = None
        self.settings: dict[str, Any] = {}
        self.seen: list[dict[str, Any]] = []

    def configure(self, settings: dict[str, Any]) -> None:
        self.settings = settings

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx

    async def stop(self) -> None:
        pass

    async def on_approval(self, approval: Approval) -> None:
        self.seen.append(approval.model_dump(mode="json"))

    async def call(self, capability: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self.ctx is not None
        match capability:
            case "echo":
                return {"args": args, "settings": self.settings, "pid": os.getpid()}
            case "emit":
                await self.ctx.emit(EventIn(type="ping", priority=1, trust="owner", payload=args))
                return {"ok": True}
            case "state":
                return {"state": await self.ctx.state()}
            case "crash":
                os._exit(3)
            case "ask":
                req = ApprovalRequest(action="测试", detail=str(args.get("detail", "x")))
                return {"id": await self.ctx.request_approval(req)}
            case "seen":
                return {"seen": self.seen}
            case "print":
                print("这行应该进 stderr")
                return {"ok": True}
            case "env":
                return {"value": os.environ.get("KOI_TEST_SECRET")}
            case "approvals":
                return {"items": [a.model_dump(mode="json") for a in await self.ctx.approvals()]}
            case "hang":
                import asyncio

                await asyncio.sleep(60)
                return {}
        raise ValueError(f"未知能力：{capability}")


if __name__ == "__main__":
    serve_stdio(Fixture())
