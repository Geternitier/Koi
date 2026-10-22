import asyncio
import contextlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from koi.kernel import Event, EventBus, RealClock, SimClock
from koi.kernel.approvals import APPROVAL_SOURCE, ApprovalError, ApprovalService
from koi.kernel.bus import Priority, Trust
from koi.kernel.plugins import PluginError, PluginHost, _Context
from koi.kernel.process import PluginSpec, ProcessPlugin, load_specs
from koi.kernel.secrets import Redactor
from koi.kernel.store import Store
from koi.persona.journal import Journal, JournalEntry
from koi.plugins.web import WebPlugin
from koi.protocol import (
    Approval,
    ApprovalRequest,
    Capability,
    EventIn,
    Manifest,
    MemorySnapshot,
    SharedDiary,
)
from koi.protocol.rpc import METHOD_NOT_FOUND, RpcError, RpcPeer

FIXTURE = Path(__file__).with_name("plugin_fixture.py")


class FakeCtx:
    plugin_id = "web"

    def __init__(self) -> None:
        self.emitted: list[EventIn] = []

    async def emit(self, event: EventIn) -> None:
        self.emitted.append(event)

    async def state(self) -> dict[str, Any]:
        return {"name": "小鲤"}

    async def approvals(self) -> list[Approval]:
        return []

    async def interactions(self) -> list:
        return []

    async def memory_view(self) -> MemorySnapshot:
        return MemorySnapshot(
            shared=True,
            diaries=1,
            pending_notes=1,
            entries=[
                SharedDiary(
                    date="2026-10-21",
                    summary="把想说的话先写下来",
                    body="今天的日记正文。",
                )
            ],
            notebook="哥哥喝奶茶要三分糖。",
            notes=["周末去新开的店"],
        )


def client(
    plugin: WebPlugin, host: str = "127.0.0.1", base_url: str = "http://127.0.0.1:8765"
) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=plugin.create_app(), client=(host, 1234))
    return httpx.AsyncClient(transport=transport, base_url=base_url)


class Recorder:
    def __init__(self, bus: EventBus) -> None:
        self.events: list[Event] = []
        bus.bind(self.on_interrupt)

    async def on_interrupt(self, e: Event) -> None:
        self.events.append(e)

    async def wait_for(self, type_: str, timeout: float = 5.0) -> Event:
        async def poll() -> Event:
            while True:
                for e in self.events:
                    if e.type == type_:
                        return e
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(poll(), timeout)


# ---------------------------------------------------------------------- Web


async def test_web_message_roundtrip():
    plugin = WebPlugin()
    ctx = FakeCtx()
    plugin._ctx = ctx
    async with client(plugin) as c:
        r = await c.post("/api/messages", json={"text": "  你好  "})
        assert r.status_code == 202
        assert ctx.emitted[0].trust == "owner" and ctx.emitted[0].payload == {"text": "你好"}
        await plugin.call("send_message", {"text": "嗨"})
        history = (await c.get("/api/history")).json()
        assert [(m["role"], m["text"]) for m in history["messages"]] == [
            ("owner", "你好"),
            ("koi", "嗨"),
        ]
        assert all(m.get("id") for m in history["messages"])
        assert (await c.get("/api/state")).json() == {"name": "小鲤"}
        assert (await c.post("/api/messages", json={"text": ""})).status_code == 422
        assert "Koi" in (await c.get("/")).text


async def test_web_history_paginates_with_stable_ids():
    plugin = WebPlugin()
    plugin._ctx = FakeCtx()
    async with client(plugin) as c:
        await c.post("/api/messages", json={"text": "一"})
        await plugin.call("send_message", {"text": "二"})
        await c.post("/api/messages", json={"text": "三"})

        latest = (await c.get("/api/history?limit=2")).json()
        assert [m["text"] for m in latest["messages"]] == ["二", "三"]
        assert latest["has_more"] is True
        assert latest["next_before"] == latest["messages"][0]["id"]

        older = (await c.get("/api/history?limit=2&before=" + latest["next_before"])).json()
        assert [m["text"] for m in older["messages"]] == ["一"]
        assert older["has_more"] is False
        assert older["next_before"] is None
        assert (await c.get("/api/history?before=missing")).status_code == 404


async def test_web_state_loop_pushes_named_state_events():
    plugin = WebPlugin()
    plugin._ctx = FakeCtx()
    queue: asyncio.Queue[dict] = asyncio.Queue()
    plugin._subscribers.add(queue)
    task = asyncio.create_task(plugin._state_loop())
    try:
        event = await asyncio.wait_for(queue.get(), 2)
        assert event["kind"] == "state"
        assert event["data"] == {"name": "小鲤"}
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_web_serves_static_assets_and_security_headers():
    plugin = WebPlugin()
    plugin._ctx = FakeCtx()
    async with client(plugin) as c:
        page = await c.get("/")
        assert page.status_code == 200
        assert "Koi" in page.text
        assert "/static/app.css" in page.text and "/static/app.js" in page.text
        assert page.headers["cache-control"] == "no-store"
        csp = page.headers["content-security-policy"]
        assert csp.startswith("default-src 'self'") and "frame-ancestors 'none'" in csp
        assert page.headers["x-content-type-options"] == "nosniff"
        assert page.headers["x-frame-options"] == "DENY"

        css = await c.get("/static/app.css")
        assert css.status_code == 200 and "text/css" in css.headers["content-type"]
        assert "koi-app" in css.text
        js = await c.get("/static/app.js")
        assert js.status_code == 200 and "javascript" in js.headers["content-type"]
        assert "/api/state" in js.text
        memory = await c.get("/api/memory")
        assert memory.status_code == 200
        assert memory.json()["entries"][0]["date"] == "2026-10-21"
        assert memory.json()["notebook"].startswith("哥哥")
        history = await c.get("/api/history")
        assert history.headers["cache-control"] == "no-store"


async def test_web_rejects_non_local_clients_and_foreign_hosts():
    plugin = WebPlugin()
    plugin._ctx = FakeCtx()
    async with client(plugin, host="192.168.1.20") as c:
        assert (await c.get("/api/history")).status_code == 403
    # DNS 重绑定：连接来自本机，但 Host 头是别人的域名
    async with client(plugin, base_url="http://evil.example:8765") as c:
        assert (await c.get("/api/history")).status_code == 403
    async with client(plugin, base_url="http://localhost:8765") as c:
        assert (await c.get("/api/history")).status_code == 200


# ---------------------------------------------------------------------- 宿主与审批


class _Echo:
    def __init__(self, max_trust: str = "external", permissions: list[str] | None = None) -> None:
        self.manifest = Manifest(
            id="echo",
            version="0",
            max_trust=max_trust,  # type: ignore[arg-type]
            permissions=permissions or [],  # type: ignore[arg-type]
            capabilities=[
                Capability(name="say", timeout_s=1),
                Capability(name="boom"),
                Capability(name="slow", timeout_s=0.05),
                Capability(name="hidden", tool=False),
            ],
        )
        self.ctx: Any = None
        self.approvals: list[Approval] = []

    async def start(self, ctx) -> None:
        self.ctx = ctx

    async def stop(self) -> None:
        pass

    async def on_approval(self, approval: Approval) -> None:
        self.approvals.append(approval)

    async def call(self, capability: str, args: dict[str, Any]) -> dict[str, Any]:
        match capability:
            case "boom":
                raise ValueError("炸了")
            case "slow":
                await asyncio.sleep(1)
        return {"ok": True, "echo": args}


async def test_memory_view_requires_owner_ui_and_provider():
    host = PluginHost(EventBus(), RealClock())
    plugin = _Echo()
    host.add(plugin)
    ctx = _Context(host, plugin)
    with pytest.raises(PermissionError, match="owner_ui"):
        await ctx.memory_view()

    plugin.manifest.permissions = ["owner_ui"]
    with pytest.raises(PluginError, match="未启用"):
        await ctx.memory_view()

    host.set_memory_provider(
        lambda: {
            "shared": True,
            "diaries": 1,
            "pending_notes": 0,
            "entries": [{"date": "2026-10-21", "body": "正文"}],
        }
    )
    view = await ctx.memory_view()
    assert view.shared and view.entries[0].date.isoformat() == "2026-10-21"


async def test_host_downgrades_trust_and_tags_source():
    bus = EventBus()
    rec = Recorder(bus)
    host = PluginHost(bus, RealClock())
    plugin = _Echo("external")
    host.add(plugin)
    await host.start()
    await plugin.ctx.emit(EventIn(type="x", priority=0, trust="owner"))
    await asyncio.sleep(0)
    assert rec.events[0].trust == Trust.EXTERNAL and rec.events[0].source == "echo"
    with pytest.raises(PluginError):
        await host.call("echo", "undeclared", {})
    with pytest.raises(PluginError):
        host.add(_Echo())
    assert [c.name for _, c in host.tools()] == ["say", "boom", "slow"]
    await host.stop()


async def test_host_audits_tool_calls_with_redaction():
    store = Store()
    host = PluginHost(
        EventBus(), RealClock(), store=store, redactor=Redactor({"llm": "secret-value"})
    )
    host.add(_Echo())
    await host.start()
    assert (await host.call("echo", "say", {"key": "secret-value"}))["ok"]
    with pytest.raises(ValueError):
        await host.call("echo", "boom", {})
    with pytest.raises(PluginError, match="超时"):
        await host.call("echo", "slow", {})
    await host.call("echo", "hidden", {})  # 非工具能力不记审计
    records = store.recent_audit()
    assert [(r.capability, r.ok) for r in records] == [
        ("slow", False),
        ("boom", False),
        ("say", True),
    ]
    assert "secret-value" not in records[2].args + records[2].summary
    assert "炸了" in records[1].summary


async def test_approval_permissions_and_flow():
    bus = EventBus()
    rec = Recorder(bus)
    clock = RealClock()
    approvals = ApprovalService(bus, clock)
    host = PluginHost(bus, clock, approvals=approvals, store=Store())
    echo = _Echo()
    web = WebPlugin()
    host.add(echo)
    host.add(web)
    # 不启动 uvicorn，直接接上下文
    echo.ctx = _Context(host, echo)
    web._ctx = _Context(host, web)

    req = ApprovalRequest(action="删除", detail="D:/x.txt")
    approval_id = await echo.ctx.request_approval(req)
    assert await echo.ctx.request_approval(req) == approval_id  # 重复请求合并
    with pytest.raises(PermissionError):
        await echo.ctx.approvals()  # 没有 approve 权限的插件看不到、批不了
    with pytest.raises(PermissionError):
        await echo.ctx.resolve_approval(approval_id, True)

    async with client(web) as c:
        pending = (await c.get("/api/approvals")).json()
        assert [(a["id"], a["plugin"], a["action"]) for a in pending] == [
            (approval_id, "echo", "删除")
        ]
        assert (await c.post(f"/api/approvals/{approval_id}", json={"approved": True})).is_success
        assert (
            await c.post(f"/api/approvals/{approval_id}", json={"approved": True})
        ).status_code == 409
        assert (await c.get("/api/approvals")).json() == []
        assert (await c.get("/api/audit")).json() == []

    assert [(a.id, a.status) for a in echo.approvals] == [(approval_id, "approved")]
    event = await rec.wait_for("approval_resolved")
    assert event.priority == Priority.P0 and event.trust == Trust.SYSTEM
    assert event.source == APPROVAL_SOURCE and "批准" in event.payload["text"]
    assert web.history[-1]["role"] == "system"
    with pytest.raises(ApprovalError):
        await approvals.resolve("nope", True)


async def test_approvals_expire():
    start = datetime(2026, 10, 20, 12, tzinfo=UTC)
    clock = SimClock(start)
    svc = ApprovalService(EventBus(), clock, ttl=timedelta(hours=1))
    a = svc.request("echo", ApprovalRequest(action="写入", detail="x"))
    assert [p.id for p in svc.pending()] == [a.id]
    clock.advance_to(start + timedelta(minutes=61))
    assert svc.pending() == []
    assert (got := svc.get(a.id)) is not None and got.status == "expired"
    with pytest.raises(ApprovalError):
        await svc.resolve(a.id, True)
    clock.advance_to(start + timedelta(hours=3))
    svc.pending()
    assert svc.get(a.id) is None


# ---------------------------------------------------------------------- JSON-RPC


async def test_rpc_peers_roundtrip():
    a_to_b: asyncio.Queue[bytes] = asyncio.Queue()
    b_to_a: asyncio.Queue[bytes] = asyncio.Queue()

    async def serve_b(method: str, params: dict[str, Any]) -> Any:
        if method == "add":
            return params["x"] + params["y"]
        if method == "ask_back":
            return await b.request("whoami")  # 处理请求时可以反向调用
        if method == "note":
            notes.append(params)
            return None
        raise RpcError(METHOD_NOT_FOUND, f"未知方法：{method}")

    async def serve_a(method: str, params: dict[str, Any]) -> Any:
        return "a"

    notes: list[dict[str, Any]] = []
    a = RpcPeer(a_to_b.put, serve_a, name="a")
    b = RpcPeer(b_to_a.put, serve_b, name="b")

    async def pump(q: asyncio.Queue[bytes], peer: RpcPeer) -> None:
        while True:
            await peer.feed(await q.get())

    pumps = [asyncio.create_task(pump(a_to_b, b)), asyncio.create_task(pump(b_to_a, a))]
    try:
        assert await a.request("add", {"x": 1, "y": 2}) == 3
        assert await a.request("ask_back") == "a"
        with pytest.raises(RpcError) as err:
            await a.request("missing")
        assert err.value.code == METHOD_NOT_FOUND
        await a.notify("note", {"n": 1})
        await b.feed(b"not json\n")  # 坏消息被忽略
        await asyncio.sleep(0.05)
        assert notes == [{"n": 1}]
        pending = asyncio.create_task(a.request("add", {"x": 1}))  # 对端会报 KeyError
        with pytest.raises(RpcError):
            await pending
        hanging = asyncio.create_task(b.request("never", timeout=5))
        await asyncio.sleep(0)
        b.close()
        with pytest.raises(ConnectionError):
            await hanging
    finally:
        for t in pumps:
            t.cancel()


# ---------------------------------------------------------------------- 进程外插件


def fixture_spec(**kw: Any) -> PluginSpec:
    return PluginSpec(
        id="fixture",
        entry=[sys.executable, str(FIXTURE)],
        settings={"greeting": "嗨"},
        **kw,
    )


@pytest.fixture
def fast_supervisor(monkeypatch):
    monkeypatch.setattr(ProcessPlugin, "restart_min_s", 0.05)
    monkeypatch.setattr(ProcessPlugin, "ping_interval_s", 0.3)
    monkeypatch.setattr(ProcessPlugin, "ping_timeout_s", 2.0)


async def wait_until(pred, timeout: float = 15.0) -> None:
    async def poll() -> None:
        while not pred():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout)


async def test_process_plugin_lifecycle(fast_supervisor, caplog):
    bus = EventBus()
    rec = Recorder(bus)
    clock = RealClock()
    approvals = ApprovalService(bus, clock)
    host = PluginHost(bus, clock, approvals=approvals, store=Store())
    host.set_state_provider(lambda: {"name": "小鲤"})
    plugin = ProcessPlugin(fixture_spec(), env=None)
    host.add(plugin)
    with pytest.raises(PluginError, match="未就绪"):
        await plugin.call("echo", {})
    await host.start()
    try:
        assert plugin.status == "running"
        # 核心侧配置覆盖插件自报的 id、信任上限和权限
        assert plugin.manifest.id == "fixture" and plugin.manifest.max_trust == "external"
        assert plugin.manifest.permissions == []
        assert "echo" in [c.name for _, c in host.tools()]

        echoed = await host.call("fixture", "echo", {"x": "中文"})
        assert echoed["args"] == {"x": "中文"} and echoed["settings"] == {"greeting": "嗨"}
        first_pid = echoed["pid"]

        await host.call("fixture", "emit", {"text": "叮"})
        event = await rec.wait_for("ping")
        assert event.source == "fixture" and event.trust == Trust.EXTERNAL
        assert event.payload == {"text": "叮"}

        assert (await host.call("fixture", "state", {}))["state"] == {"name": "小鲤"}
        with pytest.raises(PluginError, match="approve"):
            await host.call("fixture", "approvals", {})

        # print 不会污染协议通道，而是进日志
        caplog.set_level("INFO", logger="koi.plugin.fixture")
        assert (await host.call("fixture", "print", {}))["ok"]
        await wait_until(lambda: "这行应该进 stderr" in caplog.text, 5)

        # 审批回到插件进程
        approval_id = (await host.call("fixture", "ask", {"detail": "y"}))["id"]
        await approvals.resolve(approval_id, False)
        seen: list[dict[str, Any]] = []

        async def fetch_seen() -> None:
            seen[:] = (await host.call("fixture", "seen", {}))["seen"]

        for _ in range(50):
            await fetch_seen()
            if seen:
                break
            await asyncio.sleep(0.05)
        assert [(s["id"], s["status"]) for s in seen] == [(approval_id, "denied")]

        # 超时由宿主兜底
        with pytest.raises(PluginError, match="超时"):
            await host.call("fixture", "hang", {})

        # 崩溃后自动重启
        with pytest.raises(PluginError):
            await host.call("fixture", "crash", {})
        await wait_until(lambda: plugin.restarts >= 1 and plugin.status == "running")
        echoed = await host.call("fixture", "echo", {})
        assert echoed["pid"] != first_pid
    finally:
        await host.stop()
    assert plugin.status == "stopped" and plugin.pid is None


async def test_process_plugin_env_and_failed_spawn(fast_supervisor):
    env = {k: v for k, v in __import__("os").environ.items() if k != "KOI_TEST_SECRET"}
    host = PluginHost(EventBus(), RealClock())
    plugin = ProcessPlugin(fixture_spec(), env=env)
    host.add(plugin)
    await host.start()
    try:
        assert (await host.call("fixture", "env", {}))["value"] is None
    finally:
        await host.stop()

    broken = ProcessPlugin(
        PluginSpec(id="broken", entry=[sys.executable, "-c", "import sys; sys.exit(2)"])
    )
    broken.init_timeout_s = 1.0
    host = PluginHost(EventBus(), RealClock())
    host.add(broken)
    await host.start()  # 不抛出，后台继续重试
    await wait_until(lambda: broken.restarts >= 2, 10)
    assert broken.status in ("starting", "restarting")
    await host.stop()
    assert broken.status == "stopped"


def test_load_specs(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "manifest.yaml").write_text(
        "id: a\nentry: [python, -m, a]\nmax_trust: known_person\n", encoding="utf-8"
    )
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "manifest.yaml").write_text("id: b\nentry: [x]\nenabled: false\n")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "manifest.yaml").write_text("id: c\n")  # 缺 entry
    specs = load_specs(tmp_path)
    assert [(s.id, s.max_trust, s.cwd) for s in specs] == [
        ("a", "known_person", str(tmp_path / "a"))
    ]


# ---------------------------------------------------------------------- 日志


def test_journal_persists_and_prunes(tmp_path):
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    j = Journal(tmp_path)
    j.add(JournalEntry(time=now - timedelta(days=10), kind="in", who="主人", text="很久以前"))
    j.add(JournalEntry(time=now, kind="out", text="现在"))
    restored = Journal(tmp_path)
    restored.load(now)
    assert [e.text for e in restored.recent(10)] == ["现在"]
    j.prune(now.date())
    assert sorted(p.stem for p in tmp_path.iterdir()) == ["2026-10-20"]
