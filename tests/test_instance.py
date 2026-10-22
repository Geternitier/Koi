import argparse
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from koi.app import harness_spec, init, list_instances, resolve_instance
from koi.kernel.instance import (
    DEFAULT_PORT,
    InstanceBusy,
    InstanceError,
    InstanceLock,
    Registry,
    default_instance,
    find_instance,
    load_instance,
    pick_port,
)
from koi.persona import load_profile
from koi.persona.templates import DEFAULT_TEMPLATE, TemplateError, list_templates, rename
from koi.plugins.harness import HarnessPlugin


def init_args(dir: Path, **kw) -> argparse.Namespace:
    base = {"template": None, "source": None, "name": None, "port": None, "workspace": None}
    return argparse.Namespace(dir=str(dir), **{**base, **kw})


@pytest.fixture
def registry(tmp_path) -> Registry:
    return Registry(tmp_path / "registry" / "instances.json")


def test_init_creates_instance_from_template(tmp_path, registry, capsys):
    root = tmp_path / "雪"
    inst = init(init_args(root, name="雪", port=9001), registry)
    assert (root / "koi.yaml").is_file()
    assert inst.paths.data == root / "data"
    assert inst.paths.workspace == (root / "workspace").resolve()
    assert inst.paths.workspace.is_dir() and (inst.paths.persona / "params.yaml").is_file()
    profile = load_profile(inst.paths.persona / "profile.md")
    assert profile.name == "雪" and "小鲤" not in profile.body
    assert inst.config.port == 9001
    assert "koi run" in capsys.readouterr().out

    (rec,) = registry.records()
    assert (rec.name, rec.port, rec.root) == ("雪", 9001, str(root.resolve()))
    assert set(rec.protected) == {str(root.resolve() / "data"), str(root.resolve() / "koi.yaml")}

    with pytest.raises(InstanceError, match="已经是"):
        init(init_args(root), registry)


def test_init_from_own_profile_and_validation(tmp_path, registry):
    mine = tmp_path / "mine.md"
    mine.write_text("---\nname: 阿青\nowner_call: 老板\n---\n阿青是个冷静的人。", "utf-8")
    inst = init(init_args(tmp_path / "a", source=str(mine)), registry)
    profile = load_profile(inst.paths.persona / "profile.md")
    assert (profile.name, profile.owner_call) == ("阿青", "老板")
    # 自带的人设没有参数文件时用默认参数
    assert (inst.paths.persona / "params.yaml").read_text("utf-8").startswith("# 人格参数")

    with pytest.raises(TemplateError, match="可用的"):
        init(init_args(tmp_path / "b", template="nope"), registry)
    assert not (tmp_path / "b" / "koi.yaml").exists()  # 失败时不留半成品

    with pytest.raises(InstanceError, match="工作区不能放在数据目录里"):
        init(init_args(tmp_path / "c", workspace="data/ws"), registry)
    assert not (tmp_path / "c" / "koi.yaml").exists()

    ws = tmp_path / "shared_ws"
    inst = init(init_args(tmp_path / "d", workspace=str(ws)), registry)
    assert inst.paths.workspace == ws.resolve()


def test_ports_do_not_collide(tmp_path, registry):
    a = init(init_args(tmp_path / "a"), registry)
    b = init(init_args(tmp_path / "b"), registry)
    assert a.config.port != b.config.port
    assert DEFAULT_PORT not in (a.config.port, b.config.port)  # 留给默认实例
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        busy = s.getsockname()[1]
        assert pick_port(set(), start=busy) != busy


def test_find_instance_walks_up(tmp_path, registry, monkeypatch):
    inst = init(init_args(tmp_path / "p"), registry)
    deep = inst.paths.workspace / "x" / "y"
    deep.mkdir(parents=True)
    found = find_instance(deep)
    assert found is not None and found.root == inst.root
    assert find_instance(tmp_path) is None

    monkeypatch.chdir(deep)
    assert resolve_instance(None).root == inst.root
    monkeypatch.chdir(tmp_path)
    assert resolve_instance(None).paths.data == default_instance().paths.data
    with pytest.raises(InstanceError, match="koi init"):
        load_instance(tmp_path)

    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "koi.yaml").write_text("port: 不是数字\n", "utf-8")
    with pytest.raises(InstanceError, match="格式有误"):
        load_instance(tmp_path / "bad")


def test_registry_drops_deleted_instances_and_updates(tmp_path, registry):
    a = init(init_args(tmp_path / "a"), registry)
    init(init_args(tmp_path / "b"), registry)
    import shutil

    shutil.rmtree(tmp_path / "b")
    registry.register(a, "改名了")
    assert [(r.name, Path(r.root).name) for r in registry.records()] == [("改名了", "a")]
    registry.path.write_text("{坏了", "utf-8")
    assert registry.records() == []


def test_instance_lock(tmp_path, registry, capsys):
    data = tmp_path / "data"
    assert InstanceLock.probe(data) is None
    with InstanceLock(data) as lock:
        lock.acquire(port=9100)
        with pytest.raises(InstanceBusy, match="9100") as err:
            InstanceLock(data).acquire(port=9101)
        assert err.value.info["port"] == 9100
        info = InstanceLock.probe(data)
        assert info is not None and info["port"] == 9100
    assert InstanceLock.probe(data) is None
    with InstanceLock(data) as again:
        again.acquire()


def test_lock_is_released_when_process_dies(tmp_path):
    data = tmp_path / "data"
    code = (
        "import os, sys; from pathlib import Path; from koi.kernel.instance import InstanceLock;"
        f"InstanceLock(Path(r'{data}')).acquire(port=1); print('ok', flush=True); os._exit(0)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "ok", out.stderr
    # 进程没来得及 release 就退出了：锁由操作系统收回，只剩过期的 run.json
    assert InstanceLock.probe(data) is None
    with InstanceLock(data) as again:
        again.acquire(port=2)


def test_list_instances(tmp_path, registry, capsys):
    list_instances(registry)
    assert "还没有人格体" in capsys.readouterr().out
    inst = init(init_args(tmp_path / "a", name="雪", port=9200), registry)
    capsys.readouterr()
    lock = InstanceLock(inst.paths.data)
    lock.acquire(port=9200)
    list_instances(registry)
    assert "运行中 http://127.0.0.1:9200" in capsys.readouterr().out
    lock.release()
    list_instances(registry)
    assert "未运行" in capsys.readouterr().out


async def test_harness_protects_every_registered_instance(tmp_path, registry):
    me = init(init_args(tmp_path / "me"), registry)
    other = init(init_args(tmp_path / "other"), registry)
    spec = harness_spec(me, registry)
    assert str(me.paths.data) in spec.settings["blacklist"]
    assert str(me.config_path) in spec.settings["blacklist"]

    harness = HarnessPlugin()
    harness.configure(spec.settings)

    class Ctx:
        async def request_approval(self, request) -> str:
            return "a1"

    await harness.start(Ctx())  # type: ignore[arg-type]
    other_diary = other.paths.memory / "x.md"
    other_diary.write_text("秘密", "utf-8")
    for path in (other_diary, other.config_path, me.config_path):
        r = await harness.call("read_file", {"path": str(path)})
        assert "禁止" in r.get("error", ""), (path, r)
    # 改自己的 koi.yaml 等于给自己换工作区、开权限
    r = await harness.call("write_file", {"path": str(me.config_path), "content": "x"})
    assert "禁止" in r["error"]
    assert (await harness.call("write_file", {"path": "ok.txt", "content": "x"}))["ok"]

    # Harness 运行中新建的实例也立即受保护
    late = init(init_args(tmp_path / "late"), registry)
    r = await harness.call("list_dir", {"path": str(late.paths.data)})
    assert "禁止" in r["error"]
    r = await harness.call("run_shell", {"command": f"dir {late.paths.data}"})
    assert "禁止" in r["error"]

    # 登记表坏了也不会放开
    registry.path.write_text("{坏了", "utf-8")
    r = await harness.call("list_dir", {"path": str(late.paths.data)})
    assert "禁止" in r["error"]


def test_templates():
    available = list_templates()
    assert DEFAULT_TEMPLATE in available and available[DEFAULT_TEMPLATE].name == "林小柚"
    renamed = rename("---\nname: 小鲤\nowner_call: 主人\n---\n小鲤喜欢奶茶。", "雪")
    assert renamed.startswith("---\nname: 雪\nowner_call: 主人\n---\n雪喜欢奶茶。")
    assert rename("没有 front-matter", "雪").startswith("---\nname: 雪\n---\n没有")
