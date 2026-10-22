"""画面从插件工作目录进入下一次思考，字节不进事件。"""

import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from koi.kernel import Event, EventBus, Priority, RealClock, Scheduler, SimClock, Trust
from koi.kernel.llm import Usage
from koi.kernel.media import (
    MAX_IMAGE_BYTES,
    MediaError,
    frame_caption,
    resolve_frame,
    resolve_image,
)
from koi.kernel.plugins import PluginError, PluginHost
from koi.kernel.store import Store
from koi.persona import Journal, Persona, PersonaParams, PersonaProfile
from koi.persona.llm_mind import ActOutput, LLMMind, PerceiveOutput
from koi.persona.mind import SeenImage
from koi.persona.params import AffectParams, SleepParams
from koi.protocol import Manifest, MediaFrame, sniff_image

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 20, 10, tzinfo=TZ)
USAGE = Usage(prompt_tokens=10, completion_tokens=10, cache_hit_tokens=0, cache_miss_tokens=10)
PARAMS = PersonaParams(
    sleep=SleepParams(self_wake_rate_per_h=0, near_wake_rate_per_h=0),
    affect=AffectParams(noise=0),
)
PROFILE = PersonaProfile(name="小鲤", owner_call="主人", body="开朗。")


class _LLM:
    def __init__(self, responder) -> None:
        self.responder = responder
        self.calls: list[tuple[type, list[dict[str, Any]]]] = []

    async def chat_json(self, messages, schema, **kwargs):
        self.calls.append((schema, list(messages)))
        return schema.model_validate(self.responder(schema, list(messages))), USAGE


def _owner(t: datetime, text: str) -> Event:
    return Event(
        type="owner_message",
        priority=Priority.P0,
        source="web",
        trust=Trust.OWNER,
        time=t,
        payload={"text": text},
    )


class _Plugin:
    def __init__(self, plugin_id: str = "bilibili") -> None:
        self.manifest = Manifest(id=plugin_id, version="0.1.0")

    async def start(self, ctx) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def call(self, capability: str, args: dict) -> dict:
        return {"ok": True}

    async def on_approval(self, approval) -> None:
        return None


def test_frame_caption():
    assert frame_caption(45) == "第 45 秒"
    assert frame_caption(65) == "第 1 分 5 秒"
    assert frame_caption(-3) == "第 0 秒"


def test_resolve_frame_stays_inside_work_dir(tmp_path):
    root = tmp_path / "bilibili"
    frames = root / "frames" / "BV1"
    frames.mkdir(parents=True)
    (frames / "0045.jpg").write_bytes(JPEG)
    (tmp_path / "outside.jpg").write_bytes(JPEG)

    found = resolve_frame(root, "frames/BV1/0045.jpg")
    assert found == (frames / "0045.jpg").resolve()

    for rel in ("../outside.jpg", "/etc/passwd", "C:/outside.jpg", "frames/BV1/note.txt", ""):
        try:
            resolve_frame(root, rel)
        except MediaError:
            continue
        raise AssertionError(rel)


async def test_offer_media_keeps_bytes_out_of_the_event(tmp_path):
    root = tmp_path / "bilibili"
    (root / "frames").mkdir(parents=True)
    (root / "frames" / "0045.jpg").write_bytes(JPEG)
    bus = EventBus()
    store = Store()
    host = PluginHost(bus, RealClock(), store=store)
    host.add(_Plugin())
    host.set_work_dir("bilibili", root)

    host.offer_media(
        "bilibili",
        "字幕：猫探出头",
        [MediaFrame(path="frames/0045.jpg", t=45)],
        energy_cost=4.5,
    )
    await asyncio.gather(*host._tasks)

    event = bus.pending[0]
    assert event.type == "media"
    assert event.priority == 3
    assert event.payload["text"] == "字幕：猫探出头"
    assert event.payload["frames"] == [{"path": "frames/0045.jpg", "t": 45.0}]
    assert event.payload["energy_cost"] == 4.5
    raw = str(event.payload)
    assert "ffd8" not in raw.lower()
    store.append_event(event)
    window = timedelta(seconds=1)
    saved = store.events_between(event.time - window, event.time + window)
    assert "ffd8" not in str(saved[0].payload).lower()

    loaded = host.load_frames("bilibili", event.payload["frames"])
    assert loaded == [(45.0, JPEG)]


async def test_offer_media_rejects_escape_and_missing_dir(tmp_path):
    root = tmp_path / "bilibili"
    root.mkdir()
    (tmp_path / "outside.jpg").write_bytes(JPEG)
    host = PluginHost(EventBus(), RealClock())
    host.add(_Plugin())

    try:
        host.offer_media("bilibili", "看完了", [MediaFrame(path="a.jpg", t=1)])
    except PluginError as e:
        assert "工作目录" in str(e)
    else:
        raise AssertionError("没有工作目录时应拒绝")

    host.set_work_dir("bilibili", root)
    try:
        host.offer_media("bilibili", "看完了", [MediaFrame(path="../outside.jpg", t=1)])
    except PluginError as e:
        assert "相对路径" in str(e)
    else:
        raise AssertionError("越出工作目录时应拒绝")


async def test_act_sees_frames_and_spends_energy():
    def responder(schema, messages):
        if schema is PerceiveOutput:
            return {"appraisals": [], "requests": [], "recall": []}
        return {"thought": "猫探头了", "replies": [], "engaged": True}

    clock = SimClock(T0)
    sched = Scheduler(clock)
    bus = EventBus()
    llm = _LLM(responder)
    persona = Persona(
        PARAMS,
        clock=clock,
        scheduler=sched,
        bus=bus,
        mind=LLMMind(llm, PROFILE, PARAMS.mind),
        rng=random.Random(0),
        journal=Journal(),
        media_loader=lambda plugin_id, frames: [SeenImage(caption="第 45 秒", data=JPEG)],
    )

    async def publish_media():
        await bus.publish(
            Event(
                type="media",
                priority=Priority.P2,
                source="bilibili",
                trust=Trust.EXTERNAL,
                time=T0,
                payload={
                    "text": "字幕：猫探出头",
                    "frames": [{"path": "frames/0045.jpg", "t": 45}],
                    "energy_cost": 4.5,
                },
            )
        )

    before = persona.state.fatigue
    await persona.start()
    t = T0 + timedelta(minutes=1)
    sched.call_at(t, publish_media)
    sched.call_at(t, lambda: bus.publish(_owner(t, "在看什么")))
    await sched.run_until(t + timedelta(seconds=1))

    sp = PARAMS.stamina
    assert persona.state.fatigue >= before + 4.5 * sp.tool_scale * sp.factor_min
    act_messages = next(
        messages
        for schema, messages in llm.calls
        if schema is ActOutput and isinstance(messages[1]["content"], list)
    )
    assert isinstance(act_messages[0]["content"], str)
    content = act_messages[1]["content"]
    assert isinstance(content, list)
    assert any(part.get("type") == "image_url" for part in content)
    joined = content[0]["text"]
    assert "你看见的图片" in joined
    assert "第 45 秒" in joined
    blob = str(persona.journal.recent(20))
    assert "字幕：猫探出头" in blob
    assert "ffd8" not in blob.lower()


def test_sniff_image_recognizes_common_types():
    assert sniff_image(PNG) == "image/png"
    assert sniff_image(JPEG) == "image/jpeg"
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBP") == "image/webp"
    assert sniff_image(b"GIF89a") == "image/gif"
    assert sniff_image(b"<svg/>") is None


def test_resolve_image_stays_inside_work_dir(tmp_path):
    root = tmp_path / "web"
    (root / "images").mkdir(parents=True)
    (root / "images" / "a.png").write_bytes(PNG)
    (tmp_path / "outside.png").write_bytes(PNG)
    assert resolve_image(root, "images/a.png") == (root / "images" / "a.png").resolve()
    for rel in ("../outside.png", "/etc/passwd", "C:/x.png", "images/../outside.png", ""):
        with pytest.raises(MediaError):
            resolve_image(root, rel)


async def test_load_images_reads_png_and_skips_bad_ones(tmp_path):
    root = tmp_path / "web"
    (root / "images").mkdir(parents=True)
    (root / "images" / "a.png").write_bytes(PNG)
    (root / "images" / "big.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * MAX_IMAGE_BYTES)
    (root / "images" / "text.txt").write_bytes(b"not an image")
    host = PluginHost(EventBus(), RealClock())
    host.add(_Plugin("web"))
    host.set_work_dir("web", root)
    loaded = host.load_images(
        "web",
        [
            {"path": "images/a.png", "caption": "主人发来的图片"},
            {"path": "../outside.png"},
            {"path": "images/big.png"},
            {"path": "images/text.txt"},
            {"path": "images/missing.png"},
        ],
    )
    assert loaded == [("主人发来的图片", "image/png", PNG)]


async def test_act_sees_owner_images():
    def responder(schema, messages):
        if schema is PerceiveOutput:
            return {"appraisals": [], "requests": [], "recall": []}
        return {"thought": "看到了", "replies": [{"event": 1, "text": "好看"}]}

    clock = SimClock(T0)
    sched = Scheduler(clock)
    bus = EventBus()
    llm = _LLM(responder)
    persona = Persona(
        PARAMS,
        clock=clock,
        scheduler=sched,
        bus=bus,
        mind=LLMMind(llm, PROFILE, PARAMS.mind),
        rng=random.Random(0),
        journal=Journal(),
        media_loader=lambda plugin_id, refs: [
            SeenImage(caption=ref["caption"], media_type="image/png", data=PNG) for ref in refs
        ],
    )
    await persona.start()
    t = T0 + timedelta(minutes=1)
    sched.call_at(
        t,
        lambda: bus.publish(
            Event(
                type="owner_message",
                priority=Priority.P0,
                source="web",
                trust=Trust.OWNER,
                time=t,
                payload={
                    "text": "看看这个",
                    "images": [{"path": "images/a.png", "caption": "主人发来的图片（cat.png）"}],
                },
            )
        ),
    )
    await sched.run_until(t + timedelta(seconds=1))

    act = next(
        messages
        for schema, messages in llm.calls
        if schema is ActOutput and isinstance(messages[1]["content"], list)
    )
    joined = act[1]["content"][0]["text"]
    assert "你看见的图片" in joined
    assert "主人发来的图片（cat.png）" in joined
    assert any(part.get("type") == "image_url" for part in act[1]["content"])
    assert "iVBOR" not in str(persona.journal.recent(20))
