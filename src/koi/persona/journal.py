"""经历记录：按天写入 jsonl，保留最近若干天；内存中保留最近条目供 prompt 使用。"""

from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

EntryKind = Literal[
    "in", "out", "thought", "activity", "decision", "tool", "wake", "sleep", "error"
]


class JournalEntry(BaseModel):
    time: datetime
    kind: EntryKind
    who: str = ""
    text: str


class Journal:
    def __init__(
        self, directory: Path | None = None, *, keep_days: int = 7, max_recent: int = 500
    ) -> None:
        self.directory = directory
        self.keep_days = keep_days
        self._recent: deque[JournalEntry] = deque(maxlen=max_recent)
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)

    def _file(self, d: date) -> Path:
        assert self.directory is not None
        return self.directory / f"{d.isoformat()}.jsonl"

    def add(self, entry: JournalEntry) -> None:
        self._recent.append(entry)
        if self.directory is not None:
            with self._file(entry.time.date()).open("a", encoding="utf-8") as f:
                f.write(entry.model_dump_json() + "\n")

    def recent(self, n: int) -> list[JournalEntry]:
        return list(self._recent)[-n:] if n > 0 else []

    def _read(self, d: date) -> list[JournalEntry]:
        path = self._file(d)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [JournalEntry.model_validate_json(s) for s in lines if s.strip()]

    def load(self, now: datetime, days: int = 2) -> None:
        """启动时从磁盘恢复最近几天的条目。"""
        if self.directory is None:
            return
        for offset in range(days, -1, -1):
            self._recent.extend(self._read(now.date() - timedelta(days=offset)))

    def between(self, since: datetime, until: datetime) -> list[JournalEntry]:
        """since < time <= until 的条目。"""
        if self.directory is None:
            entries = list(self._recent)
        else:
            entries = []
            d = since.date() - timedelta(days=1)  # 跨时区写入时文件日期可能早一天
            while d <= until.date():
                entries.extend(self._read(d))
                d += timedelta(days=1)
        return [e for e in entries if since < e.time <= until]

    def prune(self, today: date) -> None:
        if self.directory is None:
            return
        cutoff = today - timedelta(days=self.keep_days)
        for path in self.directory.glob("*.jsonl"):
            try:
                d = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if d < cutoff:
                path.unlink()
