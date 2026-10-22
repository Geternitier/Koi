"""主人交互请求：扫码登录、手机号加验证码，以及敏感值不外泄。"""

import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_plugins import FIXTURE, Recorder, client, wait_until

from koi.kernel import EventBus, RealClock, SimClock
from koi.kernel.bus import Priority, Trust
from koi.kernel.interactions import (
    INTERACTION_SOURCE,
    MAX_PENDING_PER_PLUGIN,
    InteractionError,
    InteractionService,
)
from koi.kernel.plugins import PluginHost, _Context
from koi.kernel.process import PluginSpec, ProcessPlugin
from koi.kernel.secrets import MemoryBackend, Redactor, SecretStore
from koi.kernel.store import Store
from koi.plugins.web import WebPlugin
from koi.protocol import (
    Capability,
    Interaction,
    InteractionField,
    InteractionRequest,
    Manifest,
)

PHONE = "13812345678"
CODE = "246810"
COOKIE = "SESSDATA=abcdef0123456789"
QR = "https://passport.example.com/qr?token=t0ken"


class FakeBili:
    """假的 B站 插件：扫码登录和手机号验证码登录。"""

    def __init__(self, permissions: list[str] | None = None) -> None:
        self.manifest = Manifest(
            id="bili",
            version="0",
            permissions=permissions if permissions is not None else ["interact"],  # type: ignore[arg-type]
            secrets=["cookie"],
            capabilities=[Capability(name="login", parameters={"method": "qr 或 sms"})],
        )
        self.ctx: Any = None
        self.received: list[tuple[str, str, dict[str, str] | None]] = []
        self.phone: str | None = None
        self.qr_id: str | None = None

    async def start(self, ctx) -> None:
        self.ctx = ctx

    async def stop(self) -> None:
        pass

    async def on_approval(self, approval) -> None:
        pass

    async def call(self, capability: str, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("method") == "qr":
            self.qr_id = await self.ctx.request_interaction(
                InteractionRequest(kind="display", title="B站扫码登录", qr=QR, expires_in_s=180)
            )
            return {"ok": True, "message": "二维码已经发给主人了"}
        phone = InteractionField(name="phone", label="手机号", sensitive=True, pattern=r"1\d{10}")
        await self.ctx.request_interaction(
            InteractionRequest(kind="form", title="B站手机号登录", fields=[phone])
        )
        return {"ok": True, "message": "请主人填手机号"}

    async def on_interaction(self, a: Interaction, values: dict[str, str] | None) -> None:
        self.received.append((a.title, a.status, values))
        if values is None:
            return
        if "phone" in values:
            self.phone = values["phone"]
            code = InteractionField(name="code", label="验证码", sensitive=True, pattern=r"\d{6}")
            await self.ctx.request_interaction(
                InteractionRequest(
                    kind="form", title="B站验证码", message="验证码已发到手机", fields=[code]
                )
            )
        elif values.get("code") == CODE:
            await self.ctx.set_secret("cookie", COOKIE)
            await self.ctx.close_interaction(a.id, True, "已登录")
        else:
            await self.ctx.close_interaction(a.id, False, "验证码不对")


class World:
    def __init__(self, bili_permissions: list[str] | None = None) -> None:
        self.bus = EventBus()
        self.rec = Recorder(self.bus)
        self.store = SecretStore(MemoryBackend(), env_map={}, environ={}, user_env=lambda _: None)
        self.redactor = Redactor({})
        self.interactions = InteractionService(
            self.bus,
            RealClock(),
            on_sensitive=lambda name, value: self.redactor.add(name, value, min_len=4),
        )
        self.host = PluginHost(
            self.bus,
            RealClock(),
            interactions=self.interactions,
            store=Store(),
            secrets=self.store,
            redactor=self.redactor,
        )
        self.bili = FakeBili(bili_permissions)
        self.web = WebPlugin()
        self.host.add(self.bili)
        self.host.add(self.web)
        self.bili.ctx = _Context(self.host, self.bili)
        self.web._ctx = _Context(self.host, self.web)

    def updates(self) -> list[dict[str, Any]]:
        return [e.payload for e in self.rec.events if e.type == "interaction_update"]


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_qr_login_flow():
    w = World()
    result = await w.host.call("bili", "login", {"method": "qr"})
    assert result["ok"] and "主人" in result["message"]
    async with client(w.web) as c:
        (card,) = (await c.get("/api/interactions")).json()
        assert card["title"] == "B站扫码登录" and card["has_qr"] and "qr" not in card
        assert card["plugin"] == "bili" and card["fields"] == []
        svg = await c.get(f"/api/interactions/{card['id']}/qr.svg")
        assert svg.status_code == 200 and svg.text.startswith("<svg")
        assert svg.headers["content-type"].startswith("image/svg+xml")
        assert (await c.get("/api/interactions/nope/qr.svg")).status_code == 404
        # 展示类请求不能"提交"
        r = await c.post(f"/api/interactions/{card['id']}", json={"values": {}})
        assert r.status_code == 409

        assert w.bili.qr_id is not None
        await w.bili.ctx.update_interaction(w.bili.qr_id, "已扫码，请在手机上确认")
        assert (await c.get("/api/interactions")).json()[0]["message"].startswith("已扫码")
        await w.bili.ctx.close_interaction(w.bili.qr_id, True, "已登录")
        assert (await c.get("/api/interactions")).json() == []

    await settle()
    texts = [(u["text"], u["owner_action"]) for u in w.updates()]
    assert texts == [
        ("（bili）B站扫码登录：已扫码，请在手机上确认", False),
        ("（bili）B站扫码登录成功了：已登录", False),
    ]
    events = [e for e in w.rec.events if e.type == "interaction_update"]
    assert all(e.source == INTERACTION_SOURCE and e.trust == Trust.SYSTEM for e in events)
    assert all(e.priority == Priority.P1 for e in events)


async def test_sms_login_flow_keeps_values_out_of_everything(caplog):
    caplog.set_level(logging.DEBUG)
    w = World()
    await w.host.call("bili", "login", {"method": "sms"})
    async with client(w.web) as c:
        (form,) = (await c.get("/api/interactions")).json()
        assert form["fields"][0]["sensitive"] and form["fields"][0]["label"] == "手机号"

        r = await c.post(f"/api/interactions/{form['id']}", json={"values": {"phone": "12345"}})
        assert r.status_code == 409 and "格式不对" in r.json()["detail"]
        assert "12345" not in r.text
        r = await c.post(f"/api/interactions/{form['id']}", json={"values": {"phone": PHONE}})
        assert r.is_success
        assert w.bili.phone == PHONE
        # 同一个表单不能提交两次
        r = await c.post(f"/api/interactions/{form['id']}", json={"values": {"phone": PHONE}})
        assert r.status_code == 409

        (code_form,) = (await c.get("/api/interactions")).json()
        assert code_form["title"] == "B站验证码"
        # 主人直接在聊天框里回验证码：被截获，不会作为消息发给她
        r = await c.post("/api/messages", json={"text": CODE})
        assert r.json() == {"ok": True, "captured": True}
        history = (await c.get("/api/history")).json()["messages"]
        assert history[-2]["role"] == "owner" and history[-2]["text"] == "••••••"
        assert set(history[-2]) == {"id", "role", "text", "time"}
        assert "不会发给她" in history[-1]["text"]
        # 普通消息照常发给她
        await c.post("/api/messages", json={"text": "登好了吗"})

    await settle()
    assert (await w.bili.ctx.get_secret("cookie")) == COOKIE
    assert w.store.get("plugin.bili.cookie").get_secret_value() == COOKIE  # type: ignore[union-attr]

    texts = [(u["text"], u["owner_action"]) for u in w.updates()]
    wait = "，插件会接着处理，等它的结果就好"
    assert texts == [
        (f"主人填好了「B站手机号登录」（手机号）{wait}", True),
        (f"主人填好了「B站验证码」（验证码）{wait}", True),
        ("（bili）B站验证码成功了：已登录", False),
    ]
    owner_texts = [e.payload["text"] for e in w.rec.events if e.type == "owner_message"]
    assert owner_texts == ["登好了吗"]

    # 敏感值没有出现在任何事件、日志、审计里，脱敏器认识它们
    leak_targets = [str(e.model_dump()) for e in w.rec.events]
    leak_targets.append(caplog.text)
    leak_targets += [r.args + r.summary for r in w.host.store.recent_audit()]  # type: ignore[union-attr]
    for text in leak_targets:
        for secret in (PHONE, CODE, COOKIE):
            assert secret not in text
    redacted = w.redactor(f"电话 {PHONE} 验证码 {CODE} cookie {COOKIE}")
    assert PHONE not in redacted and CODE not in redacted and COOKIE not in redacted


async def test_wrong_code_and_cancel():
    w = World()
    await w.host.call("bili", "login", {"method": "sms"})
    (form,) = w.interactions.pending()
    await w.interactions.submit(form.id, {"phone": PHONE})
    (code_form,) = w.interactions.pending()
    await w.interactions.submit(code_form.id, {"code": "000000"})
    assert w.bili.received[-1][1] == "submitted"
    assert w.interactions.get(code_form.id).status == "failed"  # type: ignore[union-attr]

    await w.host.call("bili", "login", {"method": "qr"})
    (qr,) = w.interactions.pending()
    async with client(w.web) as c:
        assert (await c.post(f"/api/interactions/{qr.id}/cancel")).is_success
        assert (await c.post(f"/api/interactions/{qr.id}/cancel")).status_code == 409
    assert w.bili.received[-1] == ("B站扫码登录", "cancelled", None)
    await settle()
    assert w.updates()[-1] == {
        "text": "主人取消了「B站扫码登录」",
        "interaction_id": qr.id,
        "plugin": "bili",
        "status": "cancelled",
        "owner_action": True,
    }


async def test_permissions_and_validation():
    w = World(bili_permissions=[])
    with pytest.raises(PermissionError, match="interact"):
        await w.host.call("bili", "login", {"method": "qr"})
    with pytest.raises(PermissionError, match="owner_ui"):
        await w.bili.ctx.interactions()
    with pytest.raises(PermissionError, match="owner_ui"):
        await w.bili.ctx.submit_interaction("x", {})
    with pytest.raises(PermissionError, match="凭据"):
        await w.bili.ctx.get_secret("password")

    svc = w.interactions
    bad = [
        InteractionRequest(kind="form", title="空表单"),
        InteractionRequest(kind="display", title="什么都没有"),
        InteractionRequest(
            kind="display", title="坏链接", url="javascript:alert(1)", message="点我"
        ),
        InteractionRequest(
            kind="form",
            title="坏正则",
            fields=[InteractionField(name="a", label="A", pattern="(")],
        ),
        InteractionRequest(
            kind="form",
            title="重名",
            fields=[InteractionField(name="a", label="A"), InteractionField(name="a", label="B")],
        ),
    ]
    for req in bad:
        with pytest.raises(InteractionError):
            svc.request("bili", req)

    ok = InteractionRequest(kind="display", title="看", message="看一下")
    first = svc.request("bili", ok)
    # 同一件事重复请求：合并成一个
    assert svc.request("bili", ok).id == first.id
    for i in range(MAX_PENDING_PER_PLUGIN - 1):
        svc.request("bili", ok.model_copy(update={"title": f"看{i}"}))
    with pytest.raises(InteractionError, match="太多"):
        svc.request("bili", ok.model_copy(update={"title": "再看"}))
    # 只能更新、结束自己发起的
    with pytest.raises(InteractionError):
        await svc.update("other", first.id, "伪造进度")
    with pytest.raises(InteractionError):
        await svc.close("other", first.id, True)


async def test_interactions_expire():
    start = datetime(2026, 10, 20, 12, tzinfo=UTC)
    clock = SimClock(start)
    bus = EventBus()
    rec = Recorder(bus)
    notified: list[tuple[str, dict[str, str] | None]] = []

    async def notify(a: Interaction, values: dict[str, str] | None) -> None:
        notified.append((a.status, values))

    svc = InteractionService(bus, clock)
    svc.bind(notify)
    a = svc.request("bili", InteractionRequest(kind="display", title="扫码", qr=QR, expires_in_s=1))
    assert a.expires_at == start + timedelta(seconds=30)  # 下限 30 秒
    clock.advance_to(start + timedelta(seconds=31))
    assert svc.pending() == []
    await settle()
    assert notified == [("expired", None)]
    assert rec.events[-1].payload["text"] == "「扫码」过期了"
    assert rec.events[-1].priority == Priority.P2
    with pytest.raises(InteractionError, match="过期"):
        await svc.submit(a.id, {})


async def test_process_plugin_interactions_and_secrets(monkeypatch):
    monkeypatch.setattr(ProcessPlugin, "ping_interval_s", 5.0)
    bus = EventBus()
    rec = Recorder(bus)
    store = SecretStore(MemoryBackend(), env_map={}, environ={}, user_env=lambda _: None)
    redactor = Redactor({})
    interactions = InteractionService(bus, RealClock())
    host = PluginHost(bus, RealClock(), interactions=interactions, secrets=store, redactor=redactor)
    spec = PluginSpec(
        id="fixture",
        entry=[sys.executable, str(FIXTURE)],
        permissions=["interact"],
        secrets=["token"],
    )
    host.add(ProcessPlugin(spec))
    await host.start()
    try:
        interaction_id = (await host.call("fixture", "interact", {}))["id"]
        (pending,) = interactions.pending()
        assert pending.id == interaction_id and pending.message == "验证码已发送"
        await interactions.submit(interaction_id, {"code": "135790"})
        seen: list[dict[str, Any]] = []

        async def poll() -> bool:
            seen[:] = (await host.call("fixture", "interactions_seen", {}))["seen"]
            return bool(seen)

        for _ in range(50):
            if await poll():
                break
            await asyncio.sleep(0.05)
        assert seen == [{"id": interaction_id, "status": "submitted", "values": {"code": "135790"}}]

        token = "tok-0123456789abcdef"
        assert (await host.call("fixture", "secret", {"value": token}))["value"] == token
        assert store.get("plugin.fixture.token").get_secret_value() == token  # type: ignore[union-attr]
        assert token not in redactor(f"x {token} y")
        await wait_until(lambda: any(e.type == "interaction_update" for e in rec.events), 5)
    finally:
        await host.stop()
