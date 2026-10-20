"""Web 插件：网页对话与状态查看。只监听本机，访问者即主人。"""

import asyncio
import contextlib
import json
import re
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import qrcode
import qrcode.image.svg
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from koi.protocol import Approval, Capability, EventIn, Interaction, Manifest, PluginContext

PAGE = Path(__file__).with_name("index.html")
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
KEEPALIVE_S = 15.0


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class Decision(BaseModel):
    approved: bool


class Submission(BaseModel):
    values: dict[str, str]


def _public(a: Interaction) -> dict[str, Any]:
    """给页面的视图：二维码内容不直接下发，由 qr.svg 渲染。"""
    data = a.model_dump(mode="json", exclude={"qr"})
    data["has_qr"] = a.qr is not None
    return data


def _capture(pending: list[Interaction], text: str) -> Interaction | None:
    """主人在聊天框里回答的内容，是否正好是某个待填表单要的（例如 6 位验证码）。"""
    for a in pending:
        if a.kind != "form" or len(a.fields) != 1:
            continue
        pattern = a.fields[0].pattern
        if pattern is not None and re.fullmatch(pattern, text):
            return a
    return None


class WebPlugin:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, history: int = 200) -> None:
        self.host = host
        self.port = port
        self.manifest = Manifest(
            id="web",
            version="0.1.0",
            description="网页对话与状态",
            capabilities=[
                Capability(name="send_message", description="向网页发送消息", tool=False)
            ],
            max_trust="owner",
            permissions=["approve", "owner_ui"],
        )
        self.history: deque[dict[str, Any]] = deque(maxlen=history)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._ctx: PluginContext | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    # ---------------------------------------------------------------- 插件接口

    async def start(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        config = uvicorn.Config(
            self.create_app(), host=self.host, port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def call(self, capability: str, args: dict[str, Any]) -> dict[str, Any]:
        if capability != "send_message":
            raise ValueError(f"未知能力：{capability}")
        self._publish(
            {
                "role": "system" if args.get("system") else "koi",
                "text": str(args.get("text", "")),
                "time": datetime.now().astimezone().isoformat(),
            }
        )
        return {"ok": True}

    async def on_approval(self, approval: Approval) -> None:
        pass

    # ---------------------------------------------------------------- 内部

    def _system(self, text: str) -> None:
        self._publish(
            {"role": "system", "text": text, "time": datetime.now().astimezone().isoformat()}
        )

    def _publish(self, message: dict[str, Any]) -> None:
        self.history.append(message)
        for q in self._subscribers:
            q.put_nowait(message)

    @property
    def ctx(self) -> PluginContext:
        if self._ctx is None:
            raise RuntimeError("插件尚未启动")
        return self._ctx

    def create_app(self) -> FastAPI:
        app = FastAPI(title="Koi Web", docs_url=None, redoc_url=None)

        @app.middleware("http")
        async def local_only(request: Request, call_next):  # type: ignore[no-untyped-def]
            client = request.client.host if request.client else ""
            # 校验 Host 头防 DNS 重绑定：恶意网页把自己的域名解析到 127.0.0.1 时 Host 不匹配
            host = urlsplit(f"//{request.headers.get('host', '')}").hostname or ""
            if client not in LOCAL_HOSTS or host not in LOCAL_HOSTS:
                return HTMLResponse("仅限本机访问", status_code=403)
            return await call_next(request)

        @app.get("/", response_class=HTMLResponse)
        async def index() -> str:
            return PAGE.read_text(encoding="utf-8")

        @app.get("/api/history")
        async def history() -> list[dict[str, Any]]:
            return list(self.history)

        @app.get("/api/state")
        async def state() -> dict[str, Any]:
            return await self.ctx.state()

        @app.get("/api/approvals")
        async def approvals() -> list[dict[str, Any]]:
            return [a.model_dump(mode="json") for a in await self.ctx.approvals()]

        @app.post("/api/approvals/{approval_id}")
        async def resolve(approval_id: str, decision: Decision) -> dict[str, bool]:
            try:
                await self.ctx.resolve_approval(approval_id, decision.approved)
            except Exception as e:
                raise HTTPException(409, str(e)) from None
            verb = "批准" if decision.approved else "拒绝"
            self._publish(
                {
                    "role": "system",
                    "text": f"你{verb}了一个请求",
                    "time": datetime.now().astimezone().isoformat(),
                }
            )
            return {"ok": True}

        @app.get("/api/audit")
        async def audit(limit: int = 30) -> list[dict[str, Any]]:
            return [r.model_dump(mode="json") for r in await self.ctx.audit(limit)]

        @app.get("/api/interactions")
        async def interactions() -> list[dict[str, Any]]:
            return [_public(a) for a in await self.ctx.interactions()]

        @app.get("/api/interactions/{interaction_id}/qr.svg")
        async def qr(interaction_id: str) -> Response:
            a = next((a for a in await self.ctx.interactions() if a.id == interaction_id), None)
            if a is None or a.qr is None:
                raise HTTPException(404, "没有这个二维码")
            img = qrcode.make(a.qr, image_factory=qrcode.image.svg.SvgPathImage, border=2)
            return Response(
                img.to_string(encoding="unicode"),
                media_type="image/svg+xml",
                headers={"Cache-Control": "no-store"},
            )

        @app.post("/api/interactions/{interaction_id}")
        async def submit(interaction_id: str, body: Submission) -> dict[str, bool]:
            try:
                await self.ctx.submit_interaction(interaction_id, body.values)
            except Exception as e:
                raise HTTPException(409, str(e)) from None
            self._system("你提交了填写的内容")
            return {"ok": True}

        @app.post("/api/interactions/{interaction_id}/cancel")
        async def cancel(interaction_id: str) -> dict[str, bool]:
            try:
                await self.ctx.cancel_interaction(interaction_id)
            except Exception as e:
                raise HTTPException(409, str(e)) from None
            self._system("你取消了一个请求")
            return {"ok": True}

        @app.post("/api/messages", status_code=202)
        async def post_message(msg: MessageIn) -> dict[str, bool]:
            text = msg.text.strip()
            if not text:
                raise HTTPException(422, "消息不能为空")
            now = datetime.now().astimezone().isoformat()
            target = _capture(await self.ctx.interactions(), text)
            if target is not None:
                field = target.fields[0]
                shown = "•" * len(text) if field.sensitive else text
                self._publish({"role": "owner", "text": shown, "time": now})
                try:
                    await self.ctx.submit_interaction(target.id, {field.name: text})
                except Exception as e:
                    self._system(f"提交失败：{e}")
                else:
                    self._system(f"已作为「{field.label}」提交给「{target.title}」，不会发给她")
                return {"ok": True, "captured": True}
            self._publish({"role": "owner", "text": text, "time": now})
            await self.ctx.emit(
                EventIn(type="owner_message", priority=0, trust="owner", payload={"text": text})
            )
            return {"ok": True}

        @app.get("/api/stream")
        async def stream(request: Request) -> StreamingResponse:
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            self._subscribers.add(queue)

            async def events() -> AsyncIterator[str]:
                try:
                    while not await request.is_disconnected():
                        try:
                            msg = await asyncio.wait_for(queue.get(), KEEPALIVE_S)
                        except TimeoutError:
                            yield ": keepalive\n\n"
                            continue
                        yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                finally:
                    self._subscribers.discard(queue)

            return StreamingResponse(events(), media_type="text/event-stream")

        return app
