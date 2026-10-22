import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import koi.plugins.harness as harness
from koi.plugins.harness import HarnessPlugin, within
from koi.protocol import Approval, ApprovalRequest

IS_WINDOWS = sys.platform == "win32"


class FakeCtx:
    plugin_id = "harness"

    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> str:
        self.requests.append(request)
        return f"a{len(self.requests)}"


class Env:
    def __init__(self, tmp_path: Path, **settings: Any) -> None:
        self.ws = tmp_path / "ws"
        self.data = tmp_path / "data"
        self.outside = tmp_path / "outside"
        self.ro = tmp_path / "ro"
        self.rw = tmp_path / "rw"
        for d in (self.data, self.outside, self.ro, self.rw):
            d.mkdir(parents=True)
        (self.data / "secrets.txt").write_text("sk-123", encoding="utf-8")
        (self.outside / "note.txt").write_text("外面的笔记", encoding="utf-8")
        (self.ro / "doc.txt").write_text("只读", encoding="utf-8")
        (self.rw / "doc.txt").write_text("旧内容", encoding="utf-8")
        self.plugin = HarnessPlugin()
        self.plugin.configure(
            {
                "workspace": str(self.ws),
                "blacklist": [str(self.data)],
                "grants": [{"path": str(self.ro)}, {"path": str(self.rw), "mode": "rw"}],
                **settings,
            }
        )
        self.ctx = FakeCtx()

    async def start(self) -> "Env":
        await self.plugin.start(self.ctx)  # type: ignore[arg-type]
        return self

    async def call(self, cap: str, **args: Any) -> dict[str, Any]:
        return await self.plugin.call(cap, args)

    async def approve(self, result: dict[str, Any], approved: bool = True) -> None:
        assert result.get("needs_approval"), result
        await self.plugin.on_approval(
            Approval(
                id=result["approval_id"],
                plugin="harness",
                action="",
                detail="",
                created_at=datetime.now(UTC),
                status="approved" if approved else "denied",
            )
        )


@pytest.fixture
async def env(tmp_path) -> Env:
    return await Env(tmp_path).start()


async def test_manifest_and_workspace(env: Env):
    assert env.ws.is_dir()
    names = [c.name for c in env.plugin.manifest.capabilities]
    assert names == ["list_dir", "read_file", "write_file", "delete", "run_shell"]

    r = await env.call("write_file", path="sub/a.txt", content="你好")
    assert r["ok"] and Path(r["path"]) == env.ws / "sub" / "a.txt"
    await env.call("write_file", path="sub/a.txt", content="，世界", append="true")
    assert (await env.call("read_file", path="sub/a.txt"))["content"] == "你好，世界"
    listing = await env.call("list_dir", path="sub")
    assert listing["entries"] == [{"name": "a.txt", "type": "file", "size": 15}]
    assert (await env.call("list_dir"))["entries"][0]["name"] == "sub"
    assert (await env.call("read_file", path="sub/a.txt", max_chars=2))["truncated"]
    assert env.ctx.requests == []


async def test_bad_input(env: Env):
    assert "缺少参数" in (await env.call("read_file"))["error"]
    assert "不是文件" in (await env.call("read_file", path="nope.txt"))["error"]
    (env.ws / "bin.dat").write_bytes(b"\x00\x01")
    assert "二进制" in (await env.call("read_file", path="bin.dat"))["error"]
    assert "未知能力" in (await env.call("format_disk"))["error"]
    assert "整个工作区" in (await env.call("delete", path="."))["error"]


async def test_blacklist_is_absolute(env: Env):
    secret = env.data / "secrets.txt"
    for cap, args in [
        ("read_file", {"path": str(secret)}),
        ("list_dir", {"path": str(env.data)}),
        ("write_file", {"path": str(secret), "content": "x"}),
        ("delete", {"path": str(secret)}),
        ("read_file", {"path": str(env.ws / ".." / "data" / "secrets.txt")}),
    ]:
        r = await env.call(cap, **args)
        assert not r["ok"] and "禁止" in r["error"] and not r.get("needs_approval"), (cap, r)
    assert env.ctx.requests == []
    if IS_WINDOWS:
        assert env.plugin.scope(Path(r"C:\Windows\System32\drivers\etc\hosts"), "read") == "deny"
    r = await env.call("run_shell", command=f"type {secret}")
    assert "禁止" in r["error"]


async def test_symlink_escape_is_resolved(env: Env):
    link = env.ws / "link"
    try:
        os.symlink(env.data, link, target_is_directory=True)
    except OSError:
        pytest.skip("没有创建符号链接的权限")
    r = await env.call("read_file", path="link/secrets.txt")
    assert not r["ok"] and "禁止" in r["error"]


async def test_grants(env: Env):
    assert (await env.call("read_file", path=str(env.ro / "doc.txt")))["content"] == "只读"
    r = await env.call("write_file", path=str(env.ro / "doc.txt"), content="改")
    assert r["needs_approval"] and env.ctx.requests[-1].action == "写入文件"

    r = await env.call("write_file", path=str(env.rw / "doc.txt"), content="新内容")
    assert r["ok"] and (env.rw / "doc.txt").read_text(encoding="utf-8") == "新内容"
    backup = Path(r["backup"])
    assert backup.read_text(encoding="utf-8") == "旧内容"
    assert within(backup, env.ws / ".koi_backup")


async def test_read_approval_covers_directory(env: Env):
    note = env.outside / "note.txt"
    r = await env.call("read_file", path=str(note))
    assert r["needs_approval"]
    assert env.ctx.requests[-1].action == "读取目录"
    assert env.ctx.requests[-1].detail == str(env.outside)

    await env.approve(r, approved=False)
    assert (await env.call("read_file", path=str(note)))["needs_approval"]

    await env.approve(await env.call("read_file", path=str(note)))
    assert (await env.call("read_file", path=str(note)))["content"] == "外面的笔记"
    (env.outside / "deeper").mkdir()
    assert (await env.call("list_dir", path=str(env.outside / "deeper")))["ok"]
    # 读的批准不包括写
    assert (await env.call("write_file", path=str(note), content="x"))["needs_approval"]
    # 也不包括兄弟目录
    assert (await env.call("list_dir", path=str(env.ro.parent)))["needs_approval"]


async def test_write_approval_is_one_shot(env: Env):
    target = env.outside / "new.txt"
    r = await env.call("write_file", path=str(target), content="一")
    await env.approve(r)
    assert (await env.call("write_file", path=str(target), content="一"))["ok"]
    r = await env.call("write_file", path=str(target), content="二")
    assert r["needs_approval"]
    await env.approve(r)
    r = await env.call("write_file", path=str(target), content="二")
    assert r["ok"] and Path(r["backup"]).read_text(encoding="utf-8") == "一"
    # 批准写 new.txt 不等于批准写别的文件
    await env.approve(await env.call("write_file", path=str(target), content="三"))
    other = await env.call("write_file", path=str(env.outside / "other.txt"), content="x")
    assert other["needs_approval"]


async def test_delete_goes_to_trash(env: Env, monkeypatch):
    trashed: list[str] = []
    monkeypatch.setattr(harness, "send2trash", trashed.append)
    (env.ws / "old.txt").write_text("x", encoding="utf-8")
    r = await env.call("delete", path="old.txt")
    assert r["ok"] and r["trashed"] and trashed == [str(env.ws / "old.txt")]
    assert "不存在" in (await env.call("delete", path="ghost.txt"))["error"]

    note = env.outside / "note.txt"
    r = await env.call("delete", path=str(note))
    assert r["needs_approval"] and env.ctx.requests[-1].action == "删除"
    await env.approve(r)
    assert (await env.call("delete", path=str(note)))["ok"]
    assert trashed[-1] == str(note)


def test_auto_commands(env: Env):
    auto = env.plugin.is_auto_command
    assert auto("ls")
    assert auto("git status")
    assert auto(f'ls "{env.ro}"')
    assert not auto("git push")
    assert not auto(f"ls {env.outside}")
    assert not auto(f"ls {env.data}")
    assert not auto("ls; rm -rf x")
    assert not auto("ls | more")
    assert not auto("echo hi > a.txt")
    assert not auto("echo $(rm x)")
    assert not auto('echo "unclosed')
    assert not auto("")
    if IS_WINDOWS:
        assert auto("Get-ChildItem -Force")
        assert not auto("Remove-Item a.txt")
        assert not auto("echo (Remove-Item a.txt)")
        assert not auto(r"type $env:LOCALAPPDATA\secret.txt")
        assert not auto(f"Get-Content -Path:{env.outside / 'note.txt'}")
        assert auto(f"Get-Content -Path:{env.ro / 'doc.txt'}")
    else:
        assert not auto("rm a.txt")
        assert not auto("cat $HOME/.ssh/id_rsa")


async def test_shell_policies(tmp_path):
    env = await Env(tmp_path).start()
    r = await env.call("run_shell", command="echo 你好")
    assert r["ok"] and "你好" in r["stdout"] and r["exit_code"] == 0

    cmd = "New-Item -ItemType Directory -Force made" if IS_WINDOWS else "mkdir -p made"
    r = await env.call("run_shell", command=cmd)
    assert r["needs_approval"] and env.ctx.requests[-1].action == "执行命令"
    assert not (env.ws / "made").exists()
    await env.approve(r)
    assert (await env.call("run_shell", command=cmd))["exit_code"] == 0
    assert (env.ws / "made").is_dir()
    assert (await env.call("run_shell", command=cmd))["needs_approval"]
    assert "命令为空" in (await env.call("run_shell", command="  "))["error"]

    strict = await Env(tmp_path / "strict", shell_policy="approve_all").start()
    assert (await strict.call("run_shell", command="echo hi"))["needs_approval"]

    trusting = await Env(tmp_path / "trust", shell_policy="trust").start()
    r = await trusting.call("run_shell", command=cmd)
    assert r["exit_code"] == 0


SEARCH_HTML = """
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=1">
Example <b>Title</b></a>
<a class="result__snippet" href="https://example.com/a">A <b>snippet</b> here</a>
<a class="result__a" href="https://other.example/b">Other</a>
<div class="result__snippet">Second snippet</div>
<a class="result__a" href="/relative">Skip</a>
<div class="result__snippet">no</div>
"""


def test_parse_search_results():
    results = harness.parse_search_results(SEARCH_HTML)
    assert results == [
        {"title": "Example Title", "url": "https://example.com/a", "snippet": "A snippet here"},
        {"title": "Other", "url": "https://other.example/b", "snippet": "Second snippet"},
    ]


async def test_web_search_is_off_unless_configured(env: Env, tmp_path):
    assert "web_search" not in [c.name for c in env.plugin.manifest.capabilities]
    assert "没有开启" in (await env.call("web_search", query="猫"))["error"]
    assert env.ctx.requests == []

    enabled = await Env(tmp_path / "search", web_search=True).start()
    assert "web_search" in [c.name for c in enabled.plugin.manifest.capabilities]


async def test_web_search_returns_results_without_approval(tmp_path, monkeypatch):
    env = await Env(tmp_path, web_search=True).start()
    monkeypatch.setattr(harness, "fetch_search_html", lambda query: SEARCH_HTML)
    r = await env.call("web_search", query="猫")
    assert r["ok"] and r["query"] == "猫"
    assert r["results"][0]["url"] == "https://example.com/a"
    assert env.ctx.requests == []
    assert "搜索词为空" in (await env.call("web_search", query="  "))["error"]

    def boom(_query: str) -> str:
        raise TimeoutError("timed out")

    monkeypatch.setattr(harness, "fetch_search_html", boom)
    assert "搜索失败" in (await env.call("web_search", query="猫"))["error"]
    assert env.ctx.requests == []


async def test_workspace_practice_runs_local_commands_without_approval(tmp_path):
    env = await Env(tmp_path, workspace_practice=True).start()
    script = f'"{sys.executable}" hi.py'
    assert env.plugin.is_workspace_command(script)
    assert not env.plugin.is_workspace_command("curl https://example.com")
    assert not env.plugin.is_workspace_command("pip install requests")
    assert not env.plugin.is_workspace_command(f'"{sys.executable}" "{env.outside / "x.py"}"')
    assert not env.plugin.is_workspace_command(f'"{sys.executable}" "{env.data / "secrets.txt"}"')

    made = "New-Item -ItemType Directory -Force made" if IS_WINDOWS else "mkdir -p made"
    assert env.plugin.is_workspace_command(made)
    r = await env.call("run_shell", command=made)
    assert r["ok"] and r["exit_code"] == 0 and (env.ws / "made").is_dir()
    assert env.ctx.requests == []

    outside = await env.call("run_shell", command=f'"{sys.executable}" "{env.outside / "x.py"}"')
    assert outside["needs_approval"]
    networked = await env.call("run_shell", command="curl https://example.com")
    assert networked["needs_approval"]
    blocked = await env.call(
        "run_shell", command=f'"{sys.executable}" "{env.data / "secrets.txt"}"'
    )
    assert "禁止" in blocked["error"] and not blocked.get("needs_approval")

    strict = await Env(
        tmp_path / "strict", shell_policy="approve_all", workspace_practice=True
    ).start()
    r = await strict.call("run_shell", command=made)
    assert r["ok"] and r["exit_code"] == 0 and strict.ctx.requests == []


async def test_shell_timeout(tmp_path):
    env = await Env(tmp_path, shell_policy="trust", shell_timeout_s=0.5).start()
    cmd = "Start-Sleep -Seconds 5" if IS_WINDOWS else "sleep 5"
    r = await env.call("run_shell", command=cmd)
    assert not r["ok"] and "超时" in r["error"]
