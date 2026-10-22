"""网页上传图片、带图消息，以及她把登记的图片发回网页。"""

import base64
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx2 as httpx

from koi.kernel.plugins import PluginError
from koi.plugins.web import WebPlugin
from koi.protocol import (
    ImageData,
    ImageInfo,
    Interaction,
    InteractionField,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


class FakeCtx:
    plugin_id = "web"

    def __init__(self) -> None:
        self.emitted = []
        self.pending = []
        self.submitted = []
        self.images: dict[str, ImageData] = {}

    async def emit(self, event) -> None:
        self.emitted.append(event)

    async def interactions(self):
        return self.pending

    async def submit_interaction(self, interaction_id: str, values: dict[str, str]) -> None:
        self.submitted.append((interaction_id, values))

    async def image(self, image_id: str) -> ImageData:
        if image_id not in self.images:
            raise PluginError(f"没有这张图：{image_id}")
        return self.images[image_id]


def client(plugin: WebPlugin, host: str = "127.0.0.1") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=plugin.create_app(), client=(host, 1234))
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765")


def make_plugin(tmp_path: Path, ctx: FakeCtx | None = None) -> WebPlugin:
    plugin = WebPlugin(history_path=tmp_path / "web" / "history.jsonl")
    plugin._ctx = ctx or FakeCtx()
    return plugin


async def upload(
    c: httpx.AsyncClient, data: bytes = PNG, name: str = "cat.png", media_type: str = "image/png"
) -> dict:
    r = await c.post(
        "/api/uploads",
        content=data,
        headers={"Content-Type": media_type, "X-Koi-Filename": quote(name)},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_upload_and_serve_roundtrip(tmp_path):
    plugin = make_plugin(tmp_path)
    async with client(plugin) as c:
        data = await upload(c, name="猫.png")
        assert data["media_type"] == "image/png"
        assert data["name"] == "猫.png"
        assert data["url"] == "/api/uploads/" + data["id"]
        assert len(data["sha256"]) == 64
        got = await c.get(data["url"])
        assert got.status_code == 200 and got.content == PNG
        assert got.headers["content-type"] == "image/png"
        assert got.headers["etag"] == f'"{data["sha256"]}"'
        assert "immutable" in got.headers["cache-control"]
        assert got.headers["x-content-type-options"] == "nosniff"


async def test_upload_rejects_bad_input(tmp_path):
    plugin = make_plugin(tmp_path)
    async with client(plugin) as c:
        assert (await c.post("/api/uploads", content=b"")).status_code == 400
        bad = await c.post("/api/uploads", content=SVG, headers={"Content-Type": "image/svg+xml"})
        assert bad.status_code == 415
        text = await c.post(
            "/api/uploads", content=b"hello", headers={"Content-Type": "text/plain"}
        )
        assert text.status_code == 415
        huge = b"\x89PNG\r\n\x1a\n" + b"0" * (5 * 1024 * 1024 + 1)
        too_big = await c.post("/api/uploads", content=huge, headers={"Content-Type": "image/png"})
        assert too_big.status_code == 413
        assert (await c.get("/api/uploads/" + "0" * 32)).status_code == 404
        assert (await c.get("/api/uploads/not-an-id")).status_code == 404


async def test_message_with_images_publishes_refs_and_history(tmp_path):
    plugin = make_plugin(tmp_path)
    async with client(plugin) as c:
        first = await upload(c, name="猫.png")
        second = await upload(c, JPEG, "2.jpg", "image/jpeg")
        r = await c.post(
            "/api/messages",
            json={"text": "看看这个", "images": [first["id"], second["id"]], "client_id": "c1"},
        )
        assert r.status_code == 202 and r.json()["id"]
        event = plugin._ctx.emitted[-1]
        assert event.type == "owner_message" and event.trust == "owner" and event.priority == 2
        assert event.payload["text"] == "看看这个"
        assert [item["path"].split("/")[0] for item in event.payload["images"]] == [
            "images",
            "images",
        ]
        assert event.payload["images"][0]["caption"].startswith("主人发来的图片（")

        history = (await c.get("/api/history")).json()["messages"]
        message = history[-1]
        assert message["role"] == "owner" and message["client_id"] == "c1"
        assert message["text"] == "看看这个"
        assert [image["url"] for image in message["images"]] == [
            f"/api/uploads/{first['id']}",
            f"/api/uploads/{second['id']}",
        ]
        assert message["images"][0]["alt"] == "猫.png"
        raw = (tmp_path / "web" / "history.jsonl").read_text(encoding="utf-8")
        assert base64.b64encode(PNG).decode("ascii")[:20] not in raw


async def test_image_only_message_and_limits(tmp_path):
    plugin = make_plugin(tmp_path)
    async with client(plugin) as c:
        assert (await c.post("/api/messages", json={"text": ""})).status_code == 422
        data = await upload(c)
        r = await c.post("/api/messages", json={"text": "", "images": [data["id"]]})
        assert r.status_code == 202
        event = plugin._ctx.emitted[-1]
        assert event.payload["text"] == "（发来 1 张图片）"
        history = (await c.get("/api/history")).json()["messages"]
        assert history[-1]["text"] == "" and len(history[-1]["images"]) == 1

        more = [await upload(c) for _ in range(4)]
        too_many = await c.post(
            "/api/messages",
            json={"text": "多", "images": [data["id"], *[item["id"] for item in more]]},
        )
        assert too_many.status_code == 422
        unknown = await c.post("/api/messages", json={"text": "x", "images": ["0" * 32]})
        assert unknown.status_code == 422
        assert len(plugin._ctx.emitted) == 1


async def test_history_with_images_survives_restart(tmp_path):
    path = tmp_path / "web" / "history.jsonl"
    first = WebPlugin(history_path=path)
    first._ctx = FakeCtx()
    async with client(first) as c:
        data = await upload(c)
        await c.post("/api/messages", json={"text": "留着", "images": [data["id"]]})

    again = WebPlugin(history_path=path)
    again._ctx = FakeCtx()
    async with client(again) as c:
        history = (await c.get("/api/history")).json()["messages"]
        assert history[-1]["text"] == "留着"
        assert history[-1]["images"][0]["url"] == f"/api/uploads/{data['id']}"
        assert (await c.get(f"/api/uploads/{data['id']}")).content == PNG


async def test_capture_is_skipped_when_image_is_attached(tmp_path):
    ctx = FakeCtx()
    now = datetime.now().astimezone()
    ctx.pending = [
        Interaction(
            id="i1",
            plugin="bili",
            kind="form",
            title="登录",
            fields=[InteractionField(name="code", label="验证码", pattern=r"\d{6}")],
            created_at=now,
            expires_at=now + timedelta(minutes=5),
            updated_at=now,
        )
    ]
    plugin = make_plugin(tmp_path, ctx)
    async with client(plugin) as c:
        data = await upload(c)
        r = await c.post("/api/messages", json={"text": "123456", "images": [data["id"]]})
        assert r.status_code == 202
        assert ctx.submitted == []
        assert ctx.emitted[-1].type == "owner_message"
        # 没有图片时仍然按验证码截获
        r = await c.post("/api/messages", json={"text": "654321"})
        assert r.status_code == 202 and r.json().get("captured") is True
        assert ctx.submitted == [("i1", {"code": "654321"})]


async def test_send_image_accepts_any_registered_id(tmp_path):
    ctx = FakeCtx()
    for image_id, caption in (("ref-1", "镜子前的自拍"), ("out-abc", "刚画好的")):
        ctx.images[image_id] = ImageData(
            info=ImageInfo(
                id=image_id,
                kind="reference" if image_id.startswith("ref-") else "avatar",
                caption=caption,
                media_type="image/png",
                sha256="a" * 64,
                size=len(PNG),
            ),
            data=PNG,
        )
    plugin = make_plugin(tmp_path, ctx)
    async with client(plugin) as c:
        result = await plugin.call("send_image", {"image": "ref-1", "caption": "给你看"})
        assert result["ok"] is True and result["image"] == "ref-1"
        history = (await c.get("/api/history")).json()["messages"]
        message = history[-1]
        assert message["role"] == "koi" and message["text"] == "给你看"
        assert message["images"][0]["url"].startswith("/api/images/ref-1?v=")
        assert message["images"][0]["alt"] == "镜子前的自拍"

        shared = await c.get("/api/images/out-abc")
        assert shared.status_code == 200 and shared.content == PNG
        assert (await c.get("/api/images/nope")).status_code == 404

        missing = await plugin.call("send_image", {"image": "ref-9"})
        assert missing["ok"] is False and "没有这张图" in missing["error"]


def test_manifest_declares_send_image():
    capabilities = {capability.name: capability for capability in WebPlugin().manifest.capabilities}
    assert capabilities["send_image"].tool is True
    assert capabilities["send_image"].parameters["image"]
    assert "appearance" in WebPlugin().manifest.permissions
