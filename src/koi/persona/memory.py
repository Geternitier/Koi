"""长期记忆：日记、笔记本与日记激活规则。

memory/
  diary/YYYY/MM/DD.md   YAML front-matter（元数据）+ 正文
  notes/notebook.md     笔记本正文，每周精炼
  notes/inbox.md        笔记本待整理区，每行一条

复述次数、今日激活等运行状态放在 MemoryState 里，随人格体状态一起保存。
"""

import logging
import math
import random
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from koi.persona.params import MemoryParams

log = logging.getLogger(__name__)

Importance = Literal["normal", "important"]
Section = Literal["important", "recent", "sampled", "recalled"]


class DiaryMeta(BaseModel):
    date: date
    importance: Importance = "normal"
    mood_v: float = 0.0
    mood_a: float = 0.0
    # 当天情绪最强烈的程度 [0, 1]
    mood_peak: float = 0.0
    people: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    summary: str = ""


class Diary(BaseModel):
    meta: DiaryMeta
    body: str


class DayMood(BaseModel):
    """一天中清醒时情绪的时长加权平均与峰值。"""

    hours: float = 0.0
    v_sum: float = 0.0
    a_sum: float = 0.0
    peak: float = 0.0

    def add(self, v: float, a: float, hours: float) -> None:
        self.hours += hours
        self.v_sum += v * hours
        self.a_sum += a * hours
        self.peak = max(self.peak, min(1.0, math.hypot(v, a)))

    @property
    def mean(self) -> tuple[float, float]:
        if self.hours <= 0:
            return 0.0, 0.0
        return self.v_sum / self.hours, self.a_sum / self.hours


class MemoryState(BaseModel):
    # 经历记录中已巩固到的时间点
    consolidated_until: datetime | None = None
    notebook_refined_at: datetime | None = None
    # 日记日期 -> 被对话激活的次数
    recalls: dict[date, int] = Field(default_factory=dict)
    activation_day: date | None = None
    recalled_today: list[date] = Field(default_factory=list)
    day_moods: dict[date, DayMood] = Field(default_factory=dict)


class ActiveDiary(BaseModel):
    section: Section
    meta: DiaryMeta
    # None 表示只注入摘要
    body: str | None


class MemoryView(BaseModel):
    """注入 prompt 的长期记忆。"""

    notebook: str = ""
    notes: list[str] = Field(default_factory=list)
    diaries: list[ActiveDiary] = Field(default_factory=list)


def logical_date(t: datetime, mid_sleep_hour: float) -> date:
    """一天从睡眠中点开始算：凌晨还没睡时仍算前一天。"""
    offset = (mid_sleep_hour + 12) % 24 - 12
    return (t - timedelta(hours=offset)).date()


def format_diary(diary: Diary) -> str:
    data = diary.meta.model_dump(mode="json")
    for k in ("mood_v", "mood_a", "mood_peak"):
        data[k] = round(data[k], 3)
    front = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return f"---\n{front}---\n\n{diary.body.strip()}\n"


def parse_diary(text: str) -> Diary:
    if not text.startswith("---"):
        raise ValueError("缺少 front-matter")
    _, front, body = text.split("---", 2)
    return Diary(meta=DiaryMeta.model_validate(yaml.safe_load(front) or {}), body=body.strip())


def sample_old(
    metas: Iterable[DiaryMeta], today: date, recalls: dict[date, int], p: MemoryParams
) -> list[date]:
    """远期日记的加权无放回抽样（Efraimidis–Spirakis），以日期为种子，当天结果不变。"""
    pool = sorted(
        (m for m in metas if m.importance != "important" and (today - m.date).days > p.recent_days),
        key=lambda m: m.date,
    )
    if not pool or p.sample_k <= 0:
        return []
    rng = random.Random(today.toordinal())
    keyed = []
    for m in pool:
        age = (today - m.date).days
        w = (
            age**-p.forget_exponent
            * (1 + recalls.get(m.date, 0)) ** p.recall_exponent
            * max(m.mood_peak, p.mood_floor) ** p.mood_exponent
        )
        keyed.append((math.log(1 - rng.random()) / w, m.date))
    keyed.sort(reverse=True)
    return sorted(d for _, d in keyed[: p.sample_k])


class Memory:
    def __init__(self, directory: Path | None = None, params: MemoryParams | None = None) -> None:
        self.directory = directory
        self.params = params or MemoryParams()
        self._diaries: dict[date, Diary] = {}
        self._notebook = ""
        self._inbox: list[str] = []
        if directory is not None:
            (directory / "diary").mkdir(parents=True, exist_ok=True)
            (directory / "notes").mkdir(parents=True, exist_ok=True)
            self._load()

    # ------------------------------------------------------------------ 存储

    def _diary_path(self, d: date) -> Path:
        assert self.directory is not None
        return self.directory / "diary" / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}.md"

    @property
    def _notebook_path(self) -> Path:
        assert self.directory is not None
        return self.directory / "notes" / "notebook.md"

    @property
    def _inbox_path(self) -> Path:
        assert self.directory is not None
        return self.directory / "notes" / "inbox.md"

    def _load(self) -> None:
        assert self.directory is not None
        for path in sorted((self.directory / "diary").glob("*/*/*.md")):
            try:
                diary = parse_diary(path.read_text(encoding="utf-8"))
            except Exception:
                log.warning("日记解析失败，已跳过：%s", path, exc_info=True)
                continue
            self._diaries[diary.meta.date] = diary
        if self._notebook_path.exists():
            self._notebook = self._notebook_path.read_text(encoding="utf-8").strip()
        if self._inbox_path.exists():
            lines = self._inbox_path.read_text(encoding="utf-8").splitlines()
            self._inbox = [s[2:].strip() for s in lines if s.startswith("- ") and s[2:].strip()]

    def _write_inbox(self) -> None:
        if self.directory is not None:
            text = "".join(f"- {n}\n" for n in self._inbox)
            self._inbox_path.write_text(text, encoding="utf-8")

    # ------------------------------------------------------------------ 日记

    def diary(self, d: date) -> Diary | None:
        return self._diaries.get(d)

    @property
    def diaries(self) -> list[Diary]:
        return [self._diaries[d] for d in sorted(self._diaries)]

    def write_diary(self, diary: Diary) -> None:
        self._diaries[diary.meta.date] = diary
        if self.directory is not None:
            path = self._diary_path(diary.meta.date)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(format_diary(diary), encoding="utf-8")

    def important_left(self, d: date) -> int:
        """当月还能标记几篇重要日记（不计 d 当天）。"""
        used = sum(
            1
            for m in (x.meta for x in self._diaries.values())
            if m.importance == "important"
            and (m.date.year, m.date.month) == (d.year, d.month)
            and m.date != d
        )
        return max(0, self.params.important_per_month - used)

    # ------------------------------------------------------------------ 笔记本

    @property
    def notebook(self) -> str:
        return self._notebook

    def set_notebook(self, text: str) -> None:
        self._notebook = text.strip()
        if self.directory is not None:
            self._notebook_path.write_text(self._notebook + "\n", encoding="utf-8")

    @property
    def inbox(self) -> list[str]:
        return list(self._inbox)

    def add_notes(self, notes: Iterable[str]) -> None:
        added = False
        for n in notes:
            n = " ".join(n.split())
            if n and n not in self._inbox:
                self._inbox.append(n)
                added = True
        if added:
            self._write_inbox()

    def clear_inbox(self) -> None:
        self._inbox = []
        self._write_inbox()

    # ------------------------------------------------------------------ 激活

    def view(self, today: date, recalls: dict[date, int], recalled: list[date]) -> MemoryView:
        """按 §5.3 的规则选出今天激活的日记。

        顺序固定为 重要 → 近期 → 抽样 → 对话激活（按想起的先后），
        当天新想起的只追加在末尾，不打乱前面的前缀。
        远期抽样的日记只给摘要；重要的和对话中想起的给全文。
        """
        p = self.params
        important: list[Diary] = []
        recent: list[Diary] = []
        for diary in self.diaries:
            age = (today - diary.meta.date).days
            if diary.meta.importance == "important":
                important.append(diary)
            elif 0 <= age <= p.recent_days:
                recent.append(diary)
        sampled = sample_old((d.meta for d in self.diaries), today, recalls, p)

        out: list[ActiveDiary] = []
        shown: set[date] = set()

        def push(section: Section, diary: Diary | None) -> None:
            if diary is None or diary.meta.date in shown:
                return
            shown.add(diary.meta.date)
            age = (today - diary.meta.date).days
            full = section in ("important", "recalled") or age <= p.summary_only_after_days
            out.append(
                ActiveDiary(section=section, meta=diary.meta, body=diary.body if full else None)
            )

        for d in important:
            push("important", d)
        for d in recent:
            push("recent", d)
        for dt in sampled:
            push("sampled", self._diaries.get(dt))
        for dt in recalled:
            push("recalled", self._diaries.get(dt))
        return MemoryView(notebook=self._notebook, notes=self.inbox, diaries=out)

    def search(self, keywords: Iterable[str], *, exclude: set[date], limit: int) -> list[date]:
        """对话激活：按关键词在标签、人物、摘要、正文中检索。"""
        kws = [k.strip() for k in keywords if k.strip()]
        if not kws or limit <= 0:
            return []
        scored: list[tuple[int, date]] = []
        for diary in self._diaries.values():
            m = diary.meta
            if m.date in exclude:
                continue
            score = 0
            for k in kws:
                if any(k in x for x in (*m.tags, *m.people)):
                    score += 3
                if k in m.summary:
                    score += 2
                if k in diary.body:
                    score += 1
            if score:
                scored.append((score, m.date))
        scored.sort(reverse=True)
        return [d for _, d in scored[:limit]]
