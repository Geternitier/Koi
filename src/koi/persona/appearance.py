"""形象：人设里登记的头像和参考图（profile.md 的 avatar、appearance、images）。

图片放在 persona 目录里，和人设一样只能由主人修改。启动时读进内存并校验：
- 路径相对 persona 目录，不能跳出去；
- 只认 PNG、JPEG、WebP、GIF，按文件头判断，不看扩展名（SVG 能带脚本，不收）；
- 单张不超过 MAX_BYTES，参考图最多 MAX_IMAGES 张。
不合格的跳过并记警告，不影响启动。图片不进 prompt。
"""

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from koi.persona.profile import PersonaProfile
from koi.protocol import sniff_image

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 8
AVATAR_ID = "avatar"

ImageKind = Literal["avatar", "reference"]


@dataclass(frozen=True)
class Image:
    id: str
    kind: ImageKind
    file: str
    caption: str
    media_type: str
    sha256: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class Appearance:
    name: str
    description: str = ""
    avatar: Image | None = None
    images: tuple[Image, ...] = ()

    def get(self, image_id: str) -> Image | None:
        if self.avatar is not None and image_id == AVATAR_ID:
            return self.avatar
        return next((img for img in self.images if img.id == image_id), None)


def resolve_file(base: Path, rel: str) -> Path | None:
    """rel 落在 base 里面时返回真实路径，否则返回 None。"""
    if not rel or Path(rel).is_absolute() or Path(rel).drive:
        return None
    root = base.resolve()
    path = (root / rel).resolve()
    return path if path.is_relative_to(root) else None


def _read(base: Path, rel: str) -> tuple[bytes, str] | None:
    path = resolve_file(base, rel)
    if path is None:
        log.warning("形象图片路径不在人设目录里，已跳过：%s", rel)
        return None
    if not path.is_file():
        log.warning("形象图片不存在，已跳过：%s", path)
        return None
    if path.stat().st_size > MAX_BYTES:
        log.warning("形象图片超过 %d MB，已跳过：%s", MAX_BYTES // 1024 // 1024, path)
        return None
    data = path.read_bytes()
    media_type = sniff_image(data)
    if media_type is None:
        log.warning("形象图片不是 PNG、JPEG、WebP 或 GIF，已跳过：%s", path)
        return None
    return data, media_type


def _image(image_id: str, kind: ImageKind, rel: str, caption: str, base: Path) -> Image | None:
    loaded = _read(base, rel)
    if loaded is None:
        return None
    data, media_type = loaded
    return Image(
        id=image_id,
        kind=kind,
        file=rel,
        caption=caption,
        media_type=media_type,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def load_appearance(persona_dir: Path, profile: PersonaProfile) -> Appearance:
    avatar = None
    if profile.avatar:
        avatar = _image(AVATAR_ID, "avatar", profile.avatar, "头像", persona_dir)
    if len(profile.images) > MAX_IMAGES:
        log.warning("参考图最多 %d 张，多出来的忽略", MAX_IMAGES)
    images = []
    for i, ref in enumerate(profile.images[:MAX_IMAGES], 1):
        if img := _image(f"ref-{i}", "reference", ref.file, ref.caption, persona_dir):
            images.append(img)
    return Appearance(
        name=profile.name,
        description=profile.appearance.strip(),
        avatar=avatar,
        images=tuple(images),
    )
