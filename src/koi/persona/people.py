"""人际关系：人物档案（文字）与关系状态（数值）。

memory/people/<id>.md   YAML front-matter（name、ids、relation、first_met）+ 印象正文
  - name：她对这个人的称呼
  - ids：这个人在各渠道的账号，写成「插件 ID:平台用户 ID」。主人可以手动把同一个人在
    不同平台的账号合并到一张档案里
  - relation：一句话说明和她是什么关系
  - 正文：她对这个人的印象，每晚记忆巩固时由她自己更新
  owner.md 是主人的档案，name 固定为人设里的称呼，ids 不起作用：主人只按事件的信任级别认。

数值部分（近期好感、长期感情、熟悉度）只由核心写，随人格体状态保存，按闭式解惰性推进。
"""

import hashlib
import logging
import re
from datetime import date, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from koi.kernel.clock import hours_between
from koi.persona.params import PeopleParams

log = logging.getLogger(__name__)

OWNER_ID = "owner"
_SLUG = re.compile(r"[^A-Za-z0-9_.-]")


class PersonCard(BaseModel):
    id: str
    name: str
    ids: list[str] = Field(default_factory=list)
    relation: str = ""
    first_met: date | None = None
    impression: str = ""


class Relation(BaseModel):
    updated_at: datetime
    # 近期好感 [-1, 1]，以半衰期回到 bond
    affinity: float
    # 长期感情 [-1, 1]，每天睡前向近期好感靠拢一点
    bond: float
    # 熟悉度 [0, 1]
    familiarity: float = 0.0
    # 主人登记的熟人（known_person）
    known: bool = False
    contacts: int = 0
    last_contact: datetime | None = None


class PersonBrief(BaseModel):
    """注入 prompt 的人物信息。"""

    id: str
    name: str
    relation: str = ""
    impression: str = ""
    is_owner: bool = False
    # 消息里显示的名字和称呼不同时，列出消息里的写法
    shown_as: list[str] = Field(default_factory=list)
    # 和她认识的另一个人同名（可能是冒充），填那个人的称呼
    namesake: str | None = None
    familiarity: float = 0.0
    affinity: float = 0.0
    first_contact: bool = False


class PersonUpdate(BaseModel):
    """记忆巩固时她对某人的新印象。"""

    id: str
    impression: str = ""
    relation: str = ""


# ---------------------------------------------------------------------- 数值


def new_relation(now: datetime, *, known: bool, p: PeopleParams) -> Relation:
    affinity = p.known_affinity if known else p.stranger_affinity
    return Relation(
        updated_at=now,
        affinity=affinity,
        bond=affinity,
        familiarity=p.known_familiarity if known else 0.0,
        known=known,
    )


def _familiarity_floor(rel: Relation, p: PeopleParams) -> float:
    return p.known_familiarity if rel.known else 0.0


def advance_relation(rel: Relation, now: datetime, p: PeopleParams) -> None:
    """推进到 now：近期好感回到长期感情，熟悉度慢慢淡去（熟人不低于下限）。"""
    h = hours_between(rel.updated_at, now)
    if h <= 0:
        return
    rel.affinity = rel.bond + (rel.affinity - rel.bond) * 0.5 ** (h / p.affinity_half_life_h)
    floor = _familiarity_floor(rel, p)
    if rel.familiarity > floor:
        decay = 0.5 ** (h / 24 / p.familiarity_half_life_days)
        rel.familiarity = floor + (rel.familiarity - floor) * decay
    rel.updated_at = now


def record_contact(rel: Relation, now: datetime, *, known: bool, p: PeopleParams) -> None:
    """来往一次。间隔很短的连续消息算同一次。"""
    advance_relation(rel, now, p)
    rel.known = known
    rel.familiarity = max(rel.familiarity, _familiarity_floor(rel, p))
    last = rel.last_contact
    if last is None or hours_between(last, now) * 60 >= p.contact_gap_min:
        rel.familiarity += p.familiarity_gain * (1 - rel.familiarity)
        rel.contacts += 1
    rel.last_contact = now


def feel_about(rel: Relation, valence: str, magnitude: float, p: PeopleParams) -> None:
    if valence == "positive":
        rel.affinity = min(1.0, rel.affinity + p.affinity_gain * magnitude)
    elif valence == "negative":
        rel.affinity = max(-1.0, rel.affinity - p.affinity_loss * magnitude)


def settle_bond(bond: float, affinity: float, p: PeopleParams, floor: float = -1.0) -> float:
    """睡一觉，近期的感觉沉淀一点进长期感情。"""
    return max(floor, min(1.0, bond + p.bond_rate * (affinity - bond)))


def impact_scale(rel: Relation, p: PeopleParams) -> float:
    """越熟的人带来的情绪冲击越接近原样；陌生人说什么都隔着一层。"""
    return p.stranger_impact + (1 - p.stranger_impact) * rel.familiarity


def familiarity_text(f: float) -> str:
    if f >= 0.7:
        return "很熟"
    if f >= 0.4:
        return "挺熟的"
    if f >= 0.15:
        return "打过几次交道"
    return "不太熟"


def affinity_text(a: float) -> str:
    if a >= 0.5:
        return "很喜欢"
    if a >= 0.2:
        return "印象不错"
    if a > -0.2:
        return "说不上好坏"
    if a > -0.5:
        return "有点烦"
    return "很讨厌"


# ---------------------------------------------------------------------- 档案


def account_key(source: str, sender_id: object) -> str | None:
    if isinstance(sender_id, bool) or not isinstance(sender_id, str | int):
        return None
    uid = str(sender_id).strip()
    if not uid or not source:
        return None
    return f"{source}:{uid}"


def _slug(key: str) -> str:
    slug = _SLUG.sub("_", key.replace(":", "-"))
    if slug != key.replace(":", "-") or slug == OWNER_ID:
        slug += "-" + hashlib.sha1(key.encode()).hexdigest()[:6]
    return slug


def format_card(card: PersonCard) -> str:
    meta = card.model_dump(mode="json", exclude={"id", "impression"})
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    return f"---\n{front}---\n\n{card.impression.strip()}\n"


def parse_card(person_id: str, text: str) -> PersonCard:
    if not text.startswith("---"):
        raise ValueError("缺少 front-matter")
    _, front, body = text.split("---", 2)
    meta = yaml.safe_load(front) or {}
    meta["ids"] = [str(x) for x in meta.get("ids") or []]
    return PersonCard.model_validate({**meta, "id": person_id, "impression": body.strip()})


class People:
    def __init__(self, directory: Path | None = None, *, owner_call: str = "主人") -> None:
        self.directory = directory
        self.owner_call = owner_call
        self._cards: dict[str, PersonCard] = {}
        self._index: dict[str, str] = {}
        self._saved: set[str] = set()
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            self._load()
        owner = self._cards.get(OWNER_ID) or PersonCard(id=OWNER_ID, name=owner_call)
        self._cards[OWNER_ID] = owner.model_copy(
            update={"name": owner_call, "ids": [], "relation": owner.relation or "主人"}
        )

    def _path(self, person_id: str) -> Path:
        assert self.directory is not None
        return self.directory / f"{person_id}.md"

    def _load(self) -> None:
        assert self.directory is not None
        for path in sorted(self.directory.glob("*.md")):
            try:
                card = parse_card(path.stem, path.read_text(encoding="utf-8"))
            except Exception:
                log.warning("人物档案解析失败，已跳过：%s", path, exc_info=True)
                continue
            self._cards[card.id] = card
            self._saved.add(card.id)
            if card.id == OWNER_ID:
                continue
            for key in card.ids:
                if key in self._index:
                    log.warning(
                        "账号 %s 同时出现在 %s 和 %s 的档案里", key, self._index[key], card.id
                    )
                    continue
                self._index[key] = card.id

    def card(self, person_id: str) -> PersonCard | None:
        return self._cards.get(person_id)

    @property
    def owner(self) -> PersonCard:
        return self._cards[OWNER_ID]

    def saved(self, person_id: str) -> bool:
        return person_id in self._saved

    def meet(self, key: str, name: str, today: date) -> PersonCard:
        """按账号找到这个人；第一次见就建一张（暂不落盘的）档案。永远不会返回主人。"""
        person_id = self._index.get(key)
        if person_id is not None:
            card = self._cards[person_id]
            if person_id not in self._saved and name and card.name != name:
                card.name = name
            return card
        person_id = _slug(key)
        if person_id in self._cards:
            person_id += "-" + hashlib.sha1(key.encode()).hexdigest()[:6]
        card = PersonCard(id=person_id, name=name or key, ids=[key], first_met=today)
        self._cards[person_id] = card
        self._index[key] = person_id
        return card

    def namesake(self, card: PersonCard) -> str | None:
        """另一个人（包括主人）也叫这个名字时，返回那个人的称呼。"""
        if card.id == OWNER_ID:
            return None
        if card.name == self.owner_call:
            return self.owner_call
        for other in self._cards.values():
            if (
                other.id not in (card.id, OWNER_ID)
                and other.name == card.name
                and (other.id in self._saved or card.id not in self._saved)
            ):
                return other.name
        return None

    def update(self, upd: PersonUpdate, *, max_chars: int) -> None:
        card = self._cards.get(upd.id)
        if card is None:
            return
        impression = upd.impression.strip()
        if len(impression) > max_chars:
            impression = impression[:max_chars] + "……"
        if impression:
            card.impression = impression
        if upd.relation.strip() and card.id != OWNER_ID:
            card.relation = upd.relation.strip()[:40]
        self.write(card)

    def write(self, card: PersonCard) -> None:
        self._cards[card.id] = card
        self._saved.add(card.id)
        if self.directory is not None:
            self._path(card.id).write_text(format_card(card), encoding="utf-8")

    def forget(self, person_id: str) -> None:
        """忘掉还没留下印象的人。落了盘的档案不删。"""
        if person_id in self._saved or person_id == OWNER_ID:
            return
        card = self._cards.pop(person_id, None)
        if card is not None:
            for key in card.ids:
                if self._index.get(key) == person_id:
                    del self._index[key]
