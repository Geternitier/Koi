"""Web 插件：网页对话与状态查看。只监听本机，访问者即主人。"""

import asyncio
import contextlib
import json
import logging
import re
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import qrcode
import qrcode.image.svg
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from koi.protocol import Approval, Capability, EventIn, Interaction, Manifest, PluginContext

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("index.html")
STATIC_DIR = Path(__file__).with_name("static")
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
KEEPALIVE_S = 15.0
CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def _secure(response: Response) -> Response:
    """不依赖浏览器默认值：这个页面可以批准本机工具。"""
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    return response


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
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        history: int = 200,
        history_path: Path | None = None,
    ) -> None:
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
            permissions=["approve", "owner_ui", "appearance"],
        )
        self.history: deque[dict[str, Any]] = deque(maxlen=history)
        self._history_path = Path(history_path) if history_path is not None else None
        self._persisted_lines = 0
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._ctx: PluginContext | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._state_task: asyncio.Task[None] | None = None
        self._last_state_sig = ""
        self._load_history()

    # ---------------------------------------------------------------- 插件接口

    async def start(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        config = uvicorn.Config(
            self.create_app(), host=self.host, port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        self._state_task = asyncio.create_task(self._state_loop())

    async def stop(self) -> None:
        if self._state_task is not None:
            self._state_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._state_task
            self._state_task = None
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

    def _broadcast(self, event: dict[str, Any]) -> None:
        """给所有 SSE 订阅者投递事件。事件是 {kind, data}。"""
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    def _load_history(self) -> None:
        path = self._history_path
        if path is None or not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            log.warning("读取聊天记录失败：%s", path, exc_info=True)
            return
        items: list[dict[str, Any]] = []
        dirty = False
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                dirty = True
                continue
            if (
                not isinstance(item, dict)
                or item.get("role") not in {"owner", "koi", "system"}
                or not isinstance(item.get("text"), str)
                or not item["text"]
            ):
                dirty = True
                continue
            if not item.get("id"):
                item["id"] = uuid4().hex
                dirty = True
            items.append(item)
        limit = self.history.maxlen
        keep = items if limit is None else (items[-limit:] if limit > 0 else [])
        if len(keep) != len(items):
            dirty = True
        self.history.extend(keep)
        self._persisted_lines = len(items)
        if dirty:
            self._rewrite_history()

    def _rewrite_history(self) -> None:
        path = self._history_path
        if path is None:
            return
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as file:
                for item in self.history:
                    file.write(json.dumps(item, ensure_ascii=False) + "\n")
            tmp.replace(path)
            self._persisted_lines = len(self.history)
        except OSError:
            log.warning("重写聊天记录失败：%s", path, exc_info=True)
            with contextlib.suppress(OSError):
                tmp.unlink()

    def _append_history(self, item: dict[str, Any]) -> None:
        path = self._history_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(item, ensure_ascii=False) + "\n")
            self._persisted_lines += 1
            limit = self.history.maxlen or 200
            if self._persisted_lines > max(400, limit * 2):
                self._rewrite_history()
        except OSError:
            log.warning("写入聊天记录失败：%s", path, exc_info=True)

    def _publish(self, message: dict[str, Any]) -> None:
        item = dict(message)
        item.setdefault("id", uuid4().hex)
        self.history.append(item)
        self._append_history(item)
        self._broadcast({"kind": "message", "data": item})

    async def _state_loop(self) -> None:
        """把状态变化推给页面，省掉高频轮询。断线时客户端仍会自行补拉。"""
        while True:
            if self._subscribers:
                try:
                    snapshot = await self.ctx.state()
                    signature = json.dumps(
                        snapshot, ensure_ascii=False, sort_keys=True, default=str
                    )
                    if signature != self._last_state_sig:
                        self._last_state_sig = signature
                        self._broadcast({"kind": "state", "data": snapshot})
                except asyncio.CancelledError:
                    raise
                except Exception:  # 状态提供器暂时失败时不要让任务退出
                    log.debug("读取状态失败", exc_info=True)
            await asyncio.sleep(2.0)

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
                return _secure(HTMLResponse("仅限本机访问", status_code=403))
            response = await call_next(request)
            if request.url.path.startswith("/api/"):
                response.headers.setdefault("Cache-Control", "no-store")
            elif request.url.path.startswith("/static/"):
                response.headers.setdefault("Cache-Control", "no-cache")
            return _secure(response)

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", response_class=FileResponse)
        async def index() -> FileResponse:
            return FileResponse(
                PAGE,
                media_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/api/history")
        async def history(limit: int = 60, before: str | None = None) -> dict[str, Any]:
            size = max(1, min(200, limit))
            records = list(self.history)
            if before is None:
                end = len(records)
            else:
                index = next(
                    (i for i, message in enumerate(records) if message.get("id") == before), None
                )
                if index is None:
                    raise HTTPException(404, "没有这条消息")
                end = index
            start = max(0, end - size)
            page = records[start:end]
            return {
                "messages": page,
                "has_more": start > 0,
                "next_before": page[0].get("id") if start > 0 and page else None,
            }

        @app.get("/api/state")
        async def state() -> dict[str, Any]:
            return await self.ctx.state()

        @app.get("/api/memory")
        async def memory() -> dict[str, Any]:
            try:
                snapshot = await self.ctx.memory_view()
            except Exception as e:
                raise HTTPException(503, str(e)) from None
            return snapshot.model_dump(mode="json")

        @app.get("/api/avatar")
        async def avatar() -> Response:
            try:
                img = await self.ctx.image("avatar")
            except Exception:
                raise HTTPException(404, "没有头像") from None
            # 页面用 ?v=<内容哈希> 取图，换了图地址就变，所以可以放心缓存
            return Response(
                img.data,
                media_type=img.info.media_type,
                headers={"Cache-Control": "private, max-age=86400", "ETag": f'"{img.info.sha256}"'},
            )

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
                            item = await asyncio.wait_for(queue.get(), KEEPALIVE_S)
                        except TimeoutError:
                            yield ": keepalive\n\n"
                            continue
                        if item.get("kind") == "state":
                            payload = json.dumps(item["data"], ensure_ascii=False)
                            yield f"event: state\ndata: {payload}\n\n"
                        else:
                            message = item.get("data", {})
                            event_id = message.get("id")
                            prefix = f"id: {event_id}\n" if event_id else ""
                            yield prefix + f"data: {json.dumps(message, ensure_ascii=False)}\n\n"
                finally:
                    self._subscribers.discard(queue)

            return StreamingResponse(events(), media_type="text/event-stream")

        return app
