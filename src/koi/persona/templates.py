"""人设模板：`templates/<名字>/` 下的 profile.md 和 params.yaml，以及 profile.md 里登记的图片。

创建实例时把模板复制进实例的 persona 目录，之后两者互不影响。
"""

import shutil
from pathlib import Path

import yaml

from koi.persona.appearance import resolve_file
from koi.persona.profile import PersonaProfile, parse_profile

TEMPLATES = Path(__file__).parent / "templates"
DEFAULT_TEMPLATE = "xiaoli"
FILES = ("profile.md", "params.yaml")


class TemplateError(ValueError):
    pass


def list_templates() -> dict[str, PersonaProfile]:
    return {
        d.name: parse_profile((d / "profile.md").read_text(encoding="utf-8"))
        for d in sorted(TEMPLATES.iterdir())
        if (d / "profile.md").is_file()
    }


def _template_dir(name: str) -> Path:
    d = TEMPLATES / name
    if not (d / "profile.md").is_file():
        available = "、".join(list_templates()) or "无"
        raise TemplateError(f"没有这个人设模板：{name}（可用的：{available}）")
    return d


def resolve(template: str | None, source: Path | None) -> tuple[Path, Path]:
    """找到要用的 profile.md 和 params.yaml。source 可以是 profile.md 文件或含它的目录。

    自带的人设没有 params.yaml 时用默认模板的参数。
    """
    if template and source:
        raise TemplateError("--template 和 --from 只能选一个")
    if source is None:
        profile_src = _template_dir(template or DEFAULT_TEMPLATE) / "profile.md"
    elif source.is_dir():
        profile_src = source / "profile.md"
    else:
        profile_src = source
    if not profile_src.is_file():
        raise TemplateError(f"找不到人设文件：{profile_src}")
    params_src = profile_src.with_name("params.yaml")
    if not params_src.is_file():
        params_src = _template_dir(DEFAULT_TEMPLATE) / "params.yaml"
    return profile_src, params_src


def install(
    persona_dir: Path,
    *,
    template: str | None = None,
    source: Path | None = None,
    name: str | None = None,
) -> PersonaProfile:
    profile_src, params_src = resolve(template, source)
    text = profile_src.read_text(encoding="utf-8")
    if name:
        text = rename(text, name)
    profile = parse_profile(text)
    persona_dir.mkdir(parents=True, exist_ok=True)
    (persona_dir / "profile.md").write_text(text, encoding="utf-8")
    shutil.copyfile(params_src, persona_dir / "params.yaml")
    _copy_images(profile, profile_src.parent, persona_dir)
    return profile


def _copy_images(profile: PersonaProfile, src_dir: Path, persona_dir: Path) -> None:
    """人设里登记的图片跟着复制过去，保持相对路径。找不到的留给启动时报警告。"""
    for rel in profile.image_files:
        src, dst = resolve_file(src_dir, rel), resolve_file(persona_dir, rel)
        if src is None or dst is None or not src.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def ensure(persona_dir: Path) -> None:
    """补上缺失的设定文件（默认实例首次启动时）。"""
    base = _template_dir(DEFAULT_TEMPLATE)
    persona_dir.mkdir(parents=True, exist_ok=True)
    fresh = not (persona_dir / "profile.md").exists()
    for f in FILES:
        if not (persona_dir / f).exists():
            shutil.copyfile(base / f, persona_dir / f)
    if fresh:
        profile = parse_profile((base / "profile.md").read_text(encoding="utf-8"))
        _copy_images(profile, base, persona_dir)


def rename(text: str, name: str) -> str:
    """改写 front-matter 里的 name；正文里写到的旧名字也一并替换。"""
    meta: dict[str, object] = {}
    body = text
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front) or {}
    old = meta.get("name")
    meta["name"] = name
    if isinstance(old, str) and old:
        body = body.replace(old, name)
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    return f"---\n{front}---\n{body.lstrip(chr(10))}"
