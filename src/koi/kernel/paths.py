"""平台相关路径。Windows 与 macOS 的差异由 platformdirs 处理。"""

from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir, user_documents_dir


@dataclass(frozen=True)
class KoiPaths:
    data: Path
    workspace: Path

    @classmethod
    def default(cls) -> "KoiPaths":
        return cls(
            data=Path(user_data_dir("Koi", appauthor=False)),
            workspace=Path(user_documents_dir()) / "KoiWorkspace",
        )

    @property
    def persona(self) -> Path:
        return self.data / "persona"

    @property
    def state(self) -> Path:
        return self.data / "state"

    @property
    def memory(self) -> Path:
        return self.data / "memory"

    @property
    def plugins(self) -> Path:
        return self.data / "plugins"

    @property
    def db(self) -> Path:
        return self.data / "koi.db"

    def ensure(self) -> None:
        for d in (self.persona, self.state, self.memory, self.plugins, self.workspace):
            d.mkdir(parents=True, exist_ok=True)

    def is_core_path(self, path: Path) -> bool:
        """路径（解析符号链接后）是否落在核心数据目录内。"""
        return path.resolve().is_relative_to(self.data.resolve())
