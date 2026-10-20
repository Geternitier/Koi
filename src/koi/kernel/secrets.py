"""凭据管理：API key、插件凭据等。

读取顺序：进程环境变量 → 用户级环境变量（Windows 注册表）→ 系统钥匙串。
写入只经过钥匙串，由配置服务调用。凭据只在内存中以 SecretStr 持有，
并提供脱敏工具，防止经由子进程环境、日志、prompt 或 Harness 输出泄露。
"""

import contextlib
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping
from typing import Literal, Protocol

from pydantic import SecretStr

DEEPSEEK_API_KEY = "llm.deepseek"

# 凭据名 → 可覆盖它的环境变量名（按优先级）
DEFAULT_ENV: dict[str, tuple[str, ...]] = {
    DEEPSEEK_API_KEY: ("CHAT_DEEPSEEK",),
}

KEYRING_SERVICE = "Koi"

Source = Literal["env", "user_env", "keyring"]


class MissingSecretError(LookupError):
    pass


class SecretBackend(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class KeyringBackend:
    """Windows 凭据管理器 / macOS 钥匙串。"""

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self.service = service

    def get(self, name: str) -> str | None:
        import keyring
        from keyring.errors import KeyringError

        try:
            return keyring.get_password(self.service, name)
        except KeyringError:
            return None

    def set(self, name: str, value: str) -> None:
        import keyring

        keyring.set_password(self.service, name, value)

    def delete(self, name: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        with contextlib.suppress(PasswordDeleteError):
            keyring.delete_password(self.service, name)


class MemoryBackend:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.data.get(name)

    def set(self, name: str, value: str) -> None:
        self.data[name] = value

    def delete(self, name: str) -> None:
        self.data.pop(name, None)


def read_windows_user_env(name: str) -> str | None:
    """读取 Windows 用户变量。

    在设置用户变量之前就已启动的进程（如 IDE 及其终端）不会继承它，
    因此直接读注册表兜底。其他平台返回 None。
    """
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    if not isinstance(value, str) or not value:
        return None
    return os.path.expandvars(value) if kind == winreg.REG_EXPAND_SZ else value


class SecretStore:
    def __init__(
        self,
        backend: SecretBackend | None = None,
        *,
        env_map: Mapping[str, tuple[str, ...]] = DEFAULT_ENV,
        environ: Mapping[str, str] | None = None,
        user_env: Callable[[str], str | None] = read_windows_user_env,
    ) -> None:
        self.backend = backend if backend is not None else KeyringBackend()
        self.env_map = dict(env_map)
        self.names = set(self.env_map)
        self._environ = environ if environ is not None else os.environ
        self._user_env = user_env

    def register(self, name: str, *, env: tuple[str, ...] = ()) -> None:
        """登记一个凭据名（如插件凭据），使其参与脱敏。"""
        self.names.add(name)
        if env:
            self.env_map[name] = env

    def lookup(self, name: str) -> tuple[SecretStr, Source] | None:
        for var in self.env_map.get(name, ()):
            if value := self._environ.get(var):
                return SecretStr(value), "env"
        for var in self.env_map.get(name, ()):
            if value := self._user_env(var):
                return SecretStr(value), "user_env"
        if value := self.backend.get(name):
            return SecretStr(value), "keyring"
        return None

    def get(self, name: str) -> SecretStr | None:
        found = self.lookup(name)
        return found[0] if found else None

    def require(self, name: str) -> SecretStr:
        found = self.lookup(name)
        if found is None:
            envs = " / ".join(self.env_map.get(name, ())) or "（无）"
            raise MissingSecretError(
                f"缺少凭据 {name}：请设置环境变量 {envs}，或在配置服务中保存到系统钥匙串"
            )
        return found[0]

    def set(self, name: str, value: str) -> None:
        """只应由配置服务调用。"""
        self.names.add(name)
        self.backend.set(name, value)

    def delete(self, name: str) -> None:
        self.backend.delete(name)

    def masked(self, name: str) -> str | None:
        found = self.lookup(name)
        if found is None:
            return None
        value, source = found
        tail = value.get_secret_value()[-4:]
        return f"****{tail}（来自 {source}）"

    def env_names(self) -> set[str]:
        return {var for vars_ in self.env_map.values() for var in vars_}

    def values(self) -> dict[str, str]:
        """所有当前可解析的凭据明文，仅供脱敏使用。"""
        out: dict[str, str] = {}
        for name in self.names:
            if (v := self.get(name)) is not None:
                out[name] = v.get_secret_value()
        return out


# 常见 API key 形态的兜底匹配
_GENERIC_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")


class Redactor:
    """把已知凭据的明文与疑似 key 替换为占位符。"""

    def __init__(self, secrets: Mapping[str, str]) -> None:
        # 先替换长的，避免某个凭据是另一个的子串时残留
        self._secrets = sorted(
            ((v, n) for n, v in secrets.items() if len(v) >= 8), key=lambda x: -len(x[0])
        )

    @classmethod
    def from_store(cls, store: SecretStore) -> "Redactor":
        return cls(store.values())

    def __call__(self, text: str) -> str:
        for value, name in self._secrets:
            text = text.replace(value, f"[REDACTED:{name}]")
        return _GENERIC_KEY.sub("[REDACTED]", text)


def scrubbed_env(store: SecretStore, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """给 Harness 等子进程用的环境变量：去掉凭据变量，以及值等于任一凭据的变量。"""
    environ = environ if environ is not None else os.environ
    values = set(store.values().values())
    names = {n.upper() for n in store.env_names()}
    return {k: v for k, v in environ.items() if k.upper() not in names and v not in values}


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redactor(record.getMessage())
        record.args = None
        return True
