"""人格体实例：实例目录、实例配置、登记表和单实例锁。

实例目录布局（`koi init` 生成）：

    <root>/koi.yaml      实例配置，由主人维护
    <root>/data/         核心数据目录
    <root>/workspace/    Harness 工作区（可在 koi.yaml 里改）

没有 koi.yaml 时使用默认实例：数据在平台数据目录，工作区在"文档/KoiWorkspace"。
登记表记下本机所有实例，供 `koi list` 使用；每个实例的 Harness 都把所有实例的
数据目录和配置文件当作黑名单，人格体之间不能互相读写。
"""

import contextlib
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from koi.kernel.paths import KoiPaths

CONFIG_NAME = "koi.yaml"
DEFAULT_PORT = 8765
REGISTRY_NAME = "instances.json"


class InstanceError(RuntimeError):
    pass


class InstanceConfig(BaseModel):
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    # 相对路径从实例目录算起
    workspace: str = "workspace"
    harness: bool = True
    model: str | None = None


@dataclass(frozen=True)
class Instance:
    root: Path
    config: InstanceConfig
    paths: KoiPaths
    # 默认实例没有配置文件
    config_path: Path | None = None

    @property
    def protected(self) -> list[Path]:
        """人格体自己不能碰的路径：数据目录和实例配置。"""
        return [self.paths.data, *([self.config_path] if self.config_path else [])]


def default_instance() -> Instance:
    paths = KoiPaths.default()
    return Instance(root=paths.data, config=InstanceConfig(), paths=paths)


def load_instance(root: Path) -> Instance:
    root = root.resolve()
    config_path = root / CONFIG_NAME
    if not config_path.is_file():
        raise InstanceError(f"{root} 不是人格体实例目录（没有 {CONFIG_NAME}），可以先运行 koi init")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config = InstanceConfig.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as e:
        raise InstanceError(f"{config_path} 格式有误：{e}") from None
    workspace = Path(os.path.expandvars(os.path.expanduser(config.workspace)))
    if not workspace.is_absolute():
        workspace = root / workspace
    paths = KoiPaths(data=root / "data", workspace=workspace.resolve())
    if paths.workspace.is_relative_to(paths.data.resolve()):
        raise InstanceError(f"工作区不能放在数据目录里：{paths.workspace}")
    return Instance(root=root, config=config, paths=paths, config_path=config_path)


def find_instance(start: Path) -> Instance | None:
    """像 git 一样，从 start 往上找 koi.yaml。"""
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / CONFIG_NAME).is_file():
            return load_instance(d)
    return None


# ---------------------------------------------------------------------- 登记表


class InstanceRecord(BaseModel):
    root: str
    data: str
    name: str
    port: int
    protected: list[str]
    registered_at: datetime


class Registry:
    """本机实例登记表（JSON）。Harness 直接读这个文件里的 protected 字段。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def default(cls) -> "Registry":
        return cls(KoiPaths.default().data / REGISTRY_NAME)

    def records(self) -> list[InstanceRecord]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        records = []
        for item in raw.get("instances", []):
            with contextlib.suppress(ValidationError):
                r = InstanceRecord.model_validate(item)
                # 目录被删掉的实例不再列出，下次登记时从文件里清掉
                if Path(r.data).exists():
                    records.append(r)
        return records

    def register(self, instance: Instance, name: str) -> None:
        root = str(instance.root)
        records = [r for r in self.records() if r.root != root]
        records.append(
            InstanceRecord(
                root=root,
                data=str(instance.paths.data),
                name=name,
                port=instance.config.port,
                protected=[str(p) for p in instance.protected],
                registered_at=datetime.now().astimezone(),
            )
        )
        self._write(records)

    def protected_paths(self) -> list[Path]:
        return [Path(p) for r in self.records() for p in r.protected]

    def ports(self) -> set[int]:
        return {r.port for r in self.records()}

    def _write(self, records: list[InstanceRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"instances": [r.model_dump(mode="json") for r in records]}
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # Windows 上目标文件正被别人读时 replace 会失败，稍等重试
        for attempt in range(5):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)


def pick_port(taken: set[int], start: int = DEFAULT_PORT) -> int:
    """从 start 起找一个没被登记、也没被占用的本机端口。"""
    for port in range(start, start + 200):
        if port in taken:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise InstanceError(f"{start} 起的 200 个端口都被占用了")


# ---------------------------------------------------------------------- 单实例锁


class InstanceBusy(InstanceError):
    def __init__(self, data: Path, info: dict[str, Any]) -> None:
        self.info = info
        detail = f"PID {info.get('pid', '?')}，端口 {info.get('port', '?')}" if info else "信息未知"
        super().__init__(f"这个人格体已经在运行了（{detail}）：{data}")


def _try_lock(f: IO[bytes]) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(f: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class InstanceLock:
    """操作系统级文件锁：进程退出（包括崩溃）时自动释放。"""

    def __init__(self, data: Path) -> None:
        self.data = data
        self.lock_path = data / "koi.lock"
        self.info_path = data / "run.json"
        self._file: IO[bytes] | None = None

    def acquire(self, **info: Any) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        f = self.lock_path.open("a+b")
        if not _try_lock(f):
            f.close()
            raise InstanceBusy(self.data, self._read_info())
        self._file = f
        payload = {"pid": os.getpid(), "started_at": datetime.now().astimezone().isoformat()}
        self.info_path.write_text(
            json.dumps({**payload, **info}, ensure_ascii=False), encoding="utf-8"
        )

    def release(self) -> None:
        if self._file is None:
            return
        with contextlib.suppress(OSError):
            self.info_path.unlink()
        with contextlib.suppress(OSError):
            _unlock(self._file)
        self._file.close()
        self._file = None

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def _read_info(self) -> dict[str, Any]:
        try:
            return json.loads(self.info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @classmethod
    def probe(cls, data: Path) -> dict[str, Any] | None:
        """实例在运行时返回运行信息，否则返回 None。"""
        lock = cls(data)
        if not lock.lock_path.exists():
            return None
        with lock.lock_path.open("a+b") as f:
            if _try_lock(f):
                _unlock(f)
                return None
        return lock._read_info()
