"""形象：人设里登记的头像和参考图，经插件协议交给 Web 显示、留给生图模型。"""

import hashlib
import struct
import sys
import zlib
from pathlib import Path

import httpx2 as httpx
import pytest

from koi.kernel import EventBus, RealClock
from koi.kernel.plugins import PluginError, PluginHost, _Context
from koi.kernel.process import PluginSpec, ProcessPlugin
from koi.persona import load_appearance, load_profile
from koi.persona.appearance import MAX_BYTES, Appearance, Image
from koi.persona.profile import parse_profile
from koi.persona.templates import ensure, install
from koi.plugins.web import WebPlugin
from koi.protocol import AppearanceView, Capability, ImageData, ImageInfo, Manifest

FIXTURE = Path(__file__).with_name("plugin_fixture.py")


def png(color: tuple[int, int, int] = (200, 120, 90)) -> bytes:
    """1×1 的真 PNG。"""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"\x00" + bytes(color)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


PROFILE = """---
name: 小鲤
avatar: avatar.png
appearance: 短发，圆眼睛
images:
  - file: images/full.png
    caption: 全身
  - file: images/fake.png
    caption: 其实是 SVG
  - file: ../outside.png
  - file: images/missing.png
---
开朗。
"""


def make_persona(directory: Path) -> Path:
    (directory / "images").mkdir(parents=True)
    (directory / "profile.md").write_text(PROFILE, encoding="utf-8")
    (directory / "avatar.png").write_bytes(png())
    (directory / "images" / "full.png").write_bytes(png((10, 20, 30)))
    (directory / "images" / "fake.png").write_text("<svg onload='alert(1)'/>", encoding="utf-8")
    (directory.parent / "outside.png").write_bytes(png())
    return directory


def test_load_appearance_keeps_only_safe_images(tmp_path: Path, caplog):
    persona = make_persona(tmp_path / "persona")
    profile = parse_profile(PROFILE)
    assert profile.image_files[0] == "avatar.png"

    look = load_appearance(persona, profile)
    assert look.description == "短发，圆眼睛"
    assert look.avatar is not None and look.avatar.media_type == "image/png"
    assert look.avatar.sha256 == hashlib.sha256(png()).hexdigest()
    # 伪装成 PNG 的 SVG、跳出人设目录的、不存在的都被跳过
    assert [(i.id, i.caption) for i in look.images] == [("ref-1", "全身")]
    assert look.get("avatar") is look.avatar and look.get("ref-1") is look.images[0]
    assert look.get("ref-2") is None
    for hint in ("不是 PNG", "不在人设目录", "不存在"):
        assert hint in caplog.text


def test_oversized_and_absent_avatar(tmp_path: Path):
    (tmp_path / "big.png").write_bytes(png() + b"\x00" * MAX_BYTES)
    look = load_appearance(tmp_path, parse_profile("---\navatar: big.png\n---\n"))
    assert look.avatar is None
    assert load_appearance(tmp_path, parse_profile("---\nname: 小鲤\n---\n")).avatar is None


def test_init_from_own_profile_copies_images(tmp_path: Path):
    source = make_persona(tmp_path / "mine")
    target = tmp_path / "instance" / "persona"
    install(target, source=source)
    assert (target / "avatar.png").read_bytes() == png()
    assert (target / "images" / "full.png").is_file()
    assert not (target.parent / "outside.png").exists()


def test_default_template_ships_its_look(tmp_path: Path):
    for persona in (tmp_path / "default", tmp_path / "init"):
        if persona.name == "default":
            ensure(persona)
        else:
            install(persona)
        look = load_appearance(persona, load_profile(persona / "profile.md"))
        assert look.avatar is not None and look.description
        assert [i.id for i in look.images] == ["ref-1"]


def _host(look: Appearance) -> PluginHost:
    def info(img: Image) -> ImageInfo:
        return ImageInfo(
            id=img.id,
            kind=img.kind,
            caption=img.caption,
            media_type=img.media_type,
            sha256=img.sha256,
            size=len(img.data),
        )

    def image(image_id: str) -> ImageData | None:
        img = look.get(image_id)
        return ImageData(info=info(img), data=img.data) if img else None

    host = PluginHost(EventBus(), RealClock())
    host.set_appearance_provider(
        lambda: AppearanceView(
            name=look.name,
            description=look.description,
            avatar=info(look.avatar) if look.avatar else None,
            images=[info(i) for i in look.images],
        ),
        image,
    )
    return host


class _Plain:
    def __init__(self, permissions: list) -> None:
        self.manifest = Manifest(id="plain", version="1", permissions=permissions)


async def test_appearance_needs_permission(tmp_path: Path):
    host = _host(load_appearance(make_persona(tmp_path / "p"), parse_profile(PROFILE)))
    with pytest.raises(PermissionError, match="appearance"):
        await _Context(host, _Plain([])).image("avatar")  # type: ignore[arg-type]
    ctx = _Context(host, _Plain(["appearance"]))  # type: ignore[arg-type]
    assert (await ctx.appearance()).avatar is not None
    assert (await ctx.image("avatar")).data == png()
    with pytest.raises(PluginError, match="没有这张图"):
        await ctx.image("ref-9")


def test_image_data_is_base64_in_json():
    info = ImageInfo(id="avatar", kind="avatar", media_type="image/png", sha256="x", size=3)
    data = ImageData(info=info, data=b"\x00\xffA")
    dumped = data.model_dump(mode="json")
    assert dumped["data"] == "AP9B"
    assert ImageData.model_validate(dumped).data == b"\x00\xffA"


async def test_process_plugin_receives_images_intact(tmp_path: Path):
    host = _host(load_appearance(make_persona(tmp_path / "p"), parse_profile(PROFILE)))
    spec = PluginSpec(
        id="fixture", entry=[sys.executable, str(FIXTURE)], permissions=["appearance"]
    )
    plugin = ProcessPlugin(spec, env=None)
    host.add(plugin)
    await host.start()
    try:
        got = await host.call("fixture", "image", {"id": "ref-1"})
    finally:
        await host.stop()
    assert got == {
        "description": "短发，圆眼睛",
        "ids": ["ref-1"],
        "media_type": "image/png",
        "intact": True,
    }


class _AvatarCtx:
    plugin_id = "web"

    def __init__(self, data: ImageData | None) -> None:
        self.data = data

    async def image(self, image_id: str) -> ImageData:
        if self.data is None or image_id != "avatar":
            raise PluginError("没有这张图")
        return self.data


async def test_web_serves_avatar():
    info = ImageInfo(id="avatar", kind="avatar", media_type="image/png", sha256="abc", size=1)
    for data, status in ((ImageData(info=info, data=png()), 200), (None, 404)):
        plugin = WebPlugin()
        plugin._ctx = _AvatarCtx(data)  # type: ignore[assignment]
        transport = httpx.ASGITransport(app=plugin.create_app(), client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            r = await c.get("/api/avatar?v=abc")
        assert r.status_code == status
        if status == 200:
            assert r.content == png() and r.headers["content-type"] == "image/png"
            assert "max-age" in r.headers["cache-control"]
            assert r.headers["x-content-type-options"] == "nosniff"
    assert "appearance" in WebPlugin().manifest.permissions
    assert Capability(name="x").tool  # 协议包仍然可以单独导入
