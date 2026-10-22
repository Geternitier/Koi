"""插件交给她看的画面：只接受插件工作目录里的 JPEG，相对路径不能越出目录。"""

from pathlib import Path, PurePosixPath

MAX_FRAMES = 8
MAX_FRAME_BYTES = 200_000
MAX_MEDIA_ENERGY = 30.0
MEDIA_TEXT_CHARS = 2000


class MediaError(ValueError):
    pass


def frame_caption(seconds: float) -> str:
    """给她看的时间点，例如「第 45 秒」「第 1 分 5 秒」。"""
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"第 {minutes} 分 {secs} 秒"
    return f"第 {secs} 秒"


def resolve_frame(root: Path, rel: str) -> Path:
    """把相对路径解析到 root 内的 JPEG 文件。越界、绝对路径和非 JPEG 都拒绝。"""
    if not isinstance(rel, str) or not rel or rel != rel.strip():
        raise MediaError("画面路径无效")
    if rel.startswith(("/", "\\")) or ":" in rel:
        raise MediaError("画面路径必须是工作目录内的相对路径")
    posix = PurePosixPath(rel.replace("\\", "/"))
    if posix.is_absolute() or ".." in posix.parts or not posix.parts:
        raise MediaError("画面路径必须是工作目录内的相对路径")
    path = (root / Path(*posix.parts)).resolve()
    root_resolved = root.resolve()
    if not path.is_relative_to(root_resolved):
        raise MediaError("画面路径超出了工作目录")
    if path.suffix.lower() not in {".jpg", ".jpeg"}:
        raise MediaError("画面只接受 JPEG")
    if not path.is_file():
        raise MediaError(f"找不到画面：{posix.as_posix()}")
    return path


def read_frame(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as e:
        raise MediaError(f"读不到画面：{e}") from e
    if len(data) > MAX_FRAME_BYTES:
        raise MediaError("画面太大")
    if not data.startswith(b"\xff\xd8"):
        raise MediaError("画面不是 JPEG")
    return data


def relative_posix(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()
