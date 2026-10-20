"""角色设定（文字部分）：profile.md，YAML front-matter + Markdown 正文。"""

from pathlib import Path

import yaml
from pydantic import BaseModel


class PersonaProfile(BaseModel):
    name: str = "Koi"
    owner_call: str = "主人"
    body: str = ""


def parse_profile(text: str) -> PersonaProfile:
    meta: dict[str, object] = {}
    body = text
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front) or {}
    return PersonaProfile.model_validate({**meta, "body": body.strip()})


def load_profile(path: Path) -> PersonaProfile:
    return parse_profile(path.read_text(encoding="utf-8"))
