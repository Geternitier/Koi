"""角色设定（文字部分）：profile.md，YAML front-matter + Markdown 正文。

front-matter 里除了名字和称呼，还可以登记她的形象（都是可选的，见 koi.persona.appearance）：

    avatar: avatar.png              头像
    appearance: 栗棕色长卷发……       一段外貌描述，留给生图模型当提示词
    images:                         参考图，留给生图模型保持人物一致
      - file: images/full.png
        caption: 全身，白T恤配灰色荷叶边短裙
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ProfileImage(BaseModel):
    # 相对 persona 目录的路径
    file: str
    caption: str = ""


class PersonaProfile(BaseModel):
    name: str = "Koi"
    owner_call: str = "主人"
    avatar: str | None = None
    appearance: str = ""
    images: list[ProfileImage] = Field(default_factory=list)
    body: str = ""

    @property
    def image_files(self) -> list[str]:
        """登记过的所有图片文件（头像在前）。"""
        files = [self.avatar] if self.avatar else []
        return files + [img.file for img in self.images]


def parse_profile(text: str) -> PersonaProfile:
    meta: dict[str, object] = {}
    body = text
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front) or {}
    return PersonaProfile.model_validate({**meta, "body": body.strip()})


def load_profile(path: Path) -> PersonaProfile:
    return parse_profile(path.read_text(encoding="utf-8"))
