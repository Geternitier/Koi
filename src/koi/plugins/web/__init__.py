"""Web 插件：网页对话与状态查看。只监听本机，访问者即主人。"""

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

import qrcode
import qrcode.image.svg
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from koi.protocol import (
    Approval,
    Capability,
    EventIn,
    ImageData,
    Interaction,
    Manifest,
    PluginContext,
    sniff_image,
)

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("index.html")
STATIC_DIR = Path(__file__).with_name("static")
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
KEEPALIVE_S = 15.0
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_IMAGES = 4
UPLOAD_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
UPLOAD_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
# 没被对话引用的上传图最多留这么多天、这么多份
UPLOAD_KEEP_DAYS = 30
UPLOAD_KEEP_FILES = 300
CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
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


def _image_response(img: ImageData, *, max_age: int = 86400) -> Response:
    # 页面用 ?v=<内容哈希> 取图，换了图地址就变，所以可以放心缓存
    return Response(
        img.data,
        media_type=img.info.media_type,
        headers={"Cache-Control": f"private, max-age={max_age}", "ETag": f'"{img.info.sha256}"'},
    )


def _upload_name(raw: str | None) -> str:
    """上传时带过来的文件名，只留可打印字符，去掉路径分隔符和控制字符。"""
    name = unquote(raw or "").strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '\\/:*?"<>|')
    return name[:120].strip() or "图片"


def _history_images_ok(images: Any) -> bool:
    """历史里的图片条目：只认本机接口的 URL，避免把外部地址带进页面。"""
    if not isinstance(images, list) or len(images) > MAX_UPLOAD_IMAGES:
        return False
    return all(
        isinstance(image, dict)
        and isinstance(image.get("url"), str)
        and image["url"].startswith("/api/")
        for image in images
    )


class MessageIn(BaseModel):
    text: str = Field(default="", max_length=4000)
    images: list[str] = Field(default_factory=list, max_length=MAX_UPLOAD_IMAGES)
    # 页面用来把乐观消息和服务端回的这条对上，不参与展示
    client_id: str | None = Field(default=None, max_length=64)


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
        image_dir: Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.manifest = Manifest(
            id="web",
            version="0.1.0",
            description="网页对话与状态",
            capabilities=[
                Capability(name="send_message", description="向网页发送消息", tool=False),
                Capability(
                    name="send_image",
                    description="把一张图片发到网页聊天里给主人看",
                    parameters={
                        "image": "图片 ID",
                        "caption": "随图说的一句话，可空",
                    },
                    energy_cost=0.5,
                    timeout_s=10.0,
                ),
            ],
            max_trust="owner",
            permissions=["approve", "owner_ui", "appearance"],
        )
        self.history: deque[dict[str, Any]] = deque(maxlen=history)
        self._history_path = Path(history_path) if history_path is not None else None
        if image_dir is None and history_path is not None:
            image_dir = Path(history_path).parent / "images"
        self._image_dir = Path(image_dir) if image_dir is not None else None
        self._persisted_lines = 0
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._ctx: PluginContext | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._state_task: asyncio.Task[None] | None = None
        self._last_state_sig = ""
        self._load_history()
        self._prune_images()

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
        if capability == "send_message":
            self._publish(
                {
                    "role": "system" if args.get("system") else "koi",
                    "text": str(args.get("text", "")),
                    "time": datetime.now().astimezone().isoformat(),
                }
            )
            return {"ok": True}
        if capability == "send_image":
            return await self._send_image(args)
        raise ValueError(f"未知能力：{capability}")

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
            if not isinstance(item, dict) or item.get("role") not in {"owner", "koi", "system"}:
                dirty = True
                continue
            images = item.get("images")
            if images is not None and not _history_images_ok(images):
                dirty = True
                continue
            text = item.get("text")
            if not isinstance(text, str) or (not text and not images):
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

    def _publish(self, message: dict[str, Any]) -> dict[str, Any]:
        item = dict(message)
        item.setdefault("id", uuid4().hex)
        self.history.append(item)
        self._append_history(item)
        self._broadcast({"kind": "message", "data": item})
        return item

    async def _send_image(self, args: dict[str, Any]) -> dict[str, Any]:
        """她把一张登记的图片发到网页上。image 是核心给的 ID，这里不假设来源。"""
        image_id = str(args.get("image") or "").strip()
        caption = str(args.get("caption") or "").strip()[:300]
        if not image_id:
            return {"ok": False, "error": "没有指定图片"}
        try:
            img = await self.ctx.image(image_id)
        except Exception:
            return {"ok": False, "error": f"没有这张图：{image_id}"}
        item = self._publish(
            {
                "role": "koi",
                "text": caption,
                "time": datetime.now().astimezone().isoformat(),
                "images": [
                    {
                        "id": image_id,
                        "url": f"/api/images/{quote(image_id)}?v={img.info.sha256[:16]}",
                        "media_type": img.info.media_type,
                        "sha256": img.info.sha256,
                        "alt": img.info.caption or image_id,
                    }
                ],
            }
        )
        return {"ok": True, "image": image_id, "caption": caption, "message_id": item["id"]}

    # ---------------------------------------------------------------- 上传图片

    def _load_upload(self, image_id: str) -> tuple[dict[str, Any], bytes] | None:
        """按 ID 读回上传的图片。ID 不是 32 位十六进制时直接拒绝，防止路径穿越。"""
        directory = self._image_dir
        if directory is None or not UPLOAD_ID.fullmatch(image_id):
            return None
        try:
            meta = json.loads((directory / f"{image_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict):
            return None
        file = meta.get("file")
        if not isinstance(file, str) or Path(file).name != file:
            return None
        media_type = meta.get("media_type")
        if media_type not in UPLOAD_TYPES:
            return None
        path = directory / file
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        # 磁盘上的内容也被改过时不相信 sidecar
        if sniff_image(data) != media_type:
            return None
        return meta, data

    def _save_upload(self, data: bytes, media_type: str, name: str) -> dict[str, Any]:
        directory = self._image_dir
        if directory is None:
            raise HTTPException(503, "图片目录没有配置")
        directory.mkdir(parents=True, exist_ok=True)
        image_id = uuid4().hex
        file = f"{image_id}{UPLOAD_SUFFIX[media_type]}"
        path = directory / file
        tmp = directory / f".{image_id}.tmp"
        meta = {
            "id": image_id,
            "file": file,
            "name": name,
            "media_type": media_type,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "at": datetime.now().astimezone().isoformat(),
        }
        try:
            tmp.write_bytes(data)
            tmp.replace(path)
            (directory / f"{image_id}.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as e:
            with contextlib.suppress(OSError):
                tmp.unlink()
            with contextlib.suppress(OSError):
                path.unlink()
            raise HTTPException(500, "保存图片失败") from e
        return meta

    def _drop_upload(self, sidecar: Path) -> None:
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        directory = self._image_dir
        if directory is not None and isinstance(meta, dict):
            file = meta.get("file")
            if isinstance(file, str) and Path(file).name == file:
                with contextlib.suppress(OSError):
                    (directory / file).unlink()
        with contextlib.suppress(OSError):
            sidecar.unlink()

    def _prune_images(self) -> None:
        """清掉没人引用的上传图，避免聊天越来越多把磁盘占满。"""
        directory = self._image_dir
        if directory is None or not directory.is_dir():
            return
        for path in directory.glob("*.tmp"):
            with contextlib.suppress(OSError):
                path.unlink()
        referenced: set[str] = set()
        for message in self.history:
            for image in message.get("images") or []:
                url = image.get("url") if isinstance(image, dict) else None
                if isinstance(url, str) and url.startswith("/api/uploads/"):
                    referenced.add(url.rsplit("/", 1)[-1].split("?", 1)[0])
        sidecars = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
        excess = max(0, len(sidecars) - UPLOAD_KEEP_FILES)
        cutoff = datetime.now().timestamp() - UPLOAD_KEEP_DAYS * 86400
        for sidecar in sidecars:
            if not sidecar.exists() or sidecar.stem in referenced:
                continue
            try:
                stale = sidecar.stat().st_mtime < cutoff
            except OSError:
                continue
            if excess > 0:
                excess -= 1
            elif not stale:
                continue
            self._drop_upload(sidecar)

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
            return _image_response(img)

        @app.get("/api/images/{image_id}")
        async def shared_image(image_id: str) -> Response:
            """她已经登记的图片：目前是形象图，以后工具产出的图也用同一入口。"""
            try:
                img = await self.ctx.image(image_id)
            except Exception:
                raise HTTPException(404, "没有这张图") from None
            return _image_response(img)

        @app.post("/api/uploads")
        async def upload(request: Request) -> dict[str, Any]:
            if self._image_dir is None:
                raise HTTPException(503, "图片目录没有配置")
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "图片太大（上限 5 MB）")
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "图片太大（上限 5 MB）")
            data = bytes(body)
            if not data:
                raise HTTPException(400, "图片是空的")
            media_type = sniff_image(data)
            if media_type not in UPLOAD_TYPES:
                raise HTTPException(415, "只支持 PNG、JPEG、WebP 图片")
            name = _upload_name(request.headers.get("x-koi-filename"))
            meta = self._save_upload(data, media_type, name)
            return {
                "id": meta["id"],
                "url": f"/api/uploads/{meta['id']}",
                "media_type": meta["media_type"],
                "sha256": meta["sha256"],
                "size": meta["size"],
                "name": meta["name"],
            }

        @app.get("/api/uploads/{image_id}")
        async def uploaded(image_id: str) -> Response:
            found = self._load_upload(image_id)
            if found is None:
                raise HTTPException(404, "没有这张图片")
            meta, data = found
            return Response(
                data,
                media_type=str(meta["media_type"]),
                headers={
                    "Cache-Control": "private, max-age=31536000, immutable",
                    "ETag": f'"{meta["sha256"]}"',
                    "Content-Disposition": (f"inline; filename*=UTF-8''{quote(str(meta['name']))}"),
                },
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
        async def post_message(msg: MessageIn) -> dict[str, Any]:
            text = msg.text.strip()
            image_ids = list(dict.fromkeys(msg.images))
            if not text and not image_ids:
                raise HTTPException(422, "消息不能为空")
            if len(image_ids) > MAX_UPLOAD_IMAGES:
                raise HTTPException(422, f"一条消息最多 {MAX_UPLOAD_IMAGES} 张图片")
            refs: list[dict[str, str]] = []
            shown: list[dict[str, str]] = []
            for image_id in image_ids:
                found = self._load_upload(image_id)
                if found is None:
                    raise HTTPException(422, "有图片已经不在了，请重新添加")
                meta, _ = found
                refs.append(
                    {
                        "path": f"images/{meta['file']}",
                        "caption": f"主人发来的图片（{meta['name']}）",
                    }
                )
                shown.append(
                    {
                        "id": image_id,
                        "url": f"/api/uploads/{image_id}",
                        "media_type": str(meta["media_type"]),
                        "sha256": str(meta["sha256"]),
                        "alt": str(meta["name"]),
                    }
                )
            now = datetime.now().astimezone().isoformat()
            # 带图的消息不参与验证码截获：主人是在分享图片，不是在填表
            target = _capture(await self.ctx.interactions(), text) if not image_ids else None
            if target is not None:
                field = target.fields[0]
                shown_text = "•" * len(text) if field.sensitive else text
                self._publish({"role": "owner", "text": shown_text, "time": now})
                try:
                    await self.ctx.submit_interaction(target.id, {field.name: text})
                except Exception as e:
                    self._system(f"提交失败：{e}")
                else:
                    self._system(f"已作为「{field.label}」提交给「{target.title}」，不会发给她")
                return {"ok": True, "captured": True}
            item = self._publish(
                {
                    "role": "owner",
                    "text": text,
                    "time": now,
                    "images": shown,
                    "client_id": msg.client_id,
                }
            )
            payload: dict[str, Any] = {"text": text or f"（发来 {len(refs)} 张图片）"}
            if refs:
                payload["images"] = refs
            # 普通消息是 P2：睡着时留给她醒来再看；带急事字样的由核心升级
            await self.ctx.emit(
                EventIn(type="owner_message", priority=2, trust="owner", payload=payload)
            )
            return {"ok": True, "id": item["id"]}

        @app.post("/api/wake", status_code=202)
        async def wake_up() -> dict[str, bool]:
            """主人点「叫她一声」：主人的控制事件，睡着也会叫醒她。"""
            self._system("你叫了她一声")
            await self.ctx.emit(
                EventIn(
                    type="wake_up",
                    priority=0,
                    trust="owner",
                    payload={"text": "（主人叫了你一声，想让你醒醒）"},
                )
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
