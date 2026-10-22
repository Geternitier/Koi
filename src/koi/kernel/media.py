"""插件交给她看的画面与图片：只读插件工作目录里的相对路径，字节不进入事件。

- 视频帧只接受 JPEG，单张不超过 MAX_FRAME_BYTES；
- 静态图片接受 PNG、JPEG、WebP，单张不超过 MAX_IMAGE_BYTES。
"""

from pathlib import Path, PurePosixPath

from koi.protocol import sniff_image

MAX_FRAMES = 8
MAX_FRAME_BYTES = 200_000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_MEDIA_ENERGY = 30.0
MEDIA_TEXT_CHARS = 2000
FRAME_SUFFIXES = frozenset({".jpg", ".jpeg"})
IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


class MediaError(ValueError):
    pass


def frame_caption(seconds: float) -> str:
    """给她看的时间点，例如「第 45 秒」「第 1 分 5 秒」。"""
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"第 {minutes} 分 {secs} 秒"
    return f"第 {secs} 秒"


def _resolve_rel(root: Path, rel: str) -> tuple[Path, PurePosixPath]:
    """把工作目录内的相对路径解析成真实路径。越界、绝对路径都拒绝。"""
    if not isinstance(rel, str) or not rel or rel != rel.strip():
        raise MediaError("图片路径无效")
    if rel.startswith(("/", "\\")) or ":" in rel:
        raise MediaError("图片路径必须是工作目录内的相对路径")
    posix = PurePosixPath(rel.replace("\\", "/"))
    if posix.is_absolute() or ".." in posix.parts or not posix.parts:
        raise MediaError("图片路径必须是工作目录内的相对路径")
    path = (root / Path(*posix.parts)).resolve()
    if not path.is_relative_to(root.resolve()):
        raise MediaError("图片路径超出了工作目录")
    if not path.is_file():
        raise MediaError(f"找不到图片：{posix.as_posix()}")
    return path, posix


def resolve_frame(root: Path, rel: str) -> Path:
    """把相对路径解析到 root 内的 JPEG 文件。越界、绝对路径和非 JPEG 都拒绝。"""
    path, _ = _resolve_rel(root, rel)
    if path.suffix.lower() not in FRAME_SUFFIXES:
        raise MediaError("画面只接受 JPEG")
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


def resolve_image(root: Path, rel: str) -> Path:
    """把相对路径解析到 root 内的文件。是否真是图片由 read_image 按文件头判断。"""
    path, _ = _resolve_rel(root, rel)
    return path


def read_image(path: Path) -> tuple[bytes, str]:
    """读一张静态图片，返回 (字节, media_type)。按文件头判断类型。"""
    try:
        data = path.read_bytes()
    except OSError as e:
        raise MediaError(f"读不到图片：{e}") from e
    if len(data) > MAX_IMAGE_BYTES:
        raise MediaError("图片太大")
    media_type = sniff_image(data)
    if media_type not in IMAGE_MEDIA_TYPES:
        raise MediaError("图片只接受 PNG、JPEG、WebP")
    return data, media_type


def relative_posix(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()
