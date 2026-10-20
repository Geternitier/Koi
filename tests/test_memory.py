from datetime import date, datetime, timedelta, timezone

from koi.persona.memory import (
    Diary,
    DiaryMeta,
    Memory,
    format_diary,
    logical_date,
    parse_diary,
    sample_old,
)
from koi.persona.params import MemoryParams

TZ = timezone(timedelta(hours=8))
TODAY = date(2026, 10, 20)


def diary(d: date, **meta) -> Diary:
    body = meta.pop("body", f"{d} 的日记")
    return Diary(meta=DiaryMeta(date=d, **meta), body=body)


def test_logical_date_starts_at_mid_sleep():
    mid = 3.3
    assert logical_date(datetime(2026, 10, 21, 1, tzinfo=TZ), mid) == TODAY
    assert logical_date(datetime(2026, 10, 21, 4, tzinfo=TZ), mid) == date(2026, 10, 21)
    # 早睡早起型（睡眠中点在前一天 23 点）：23:30 已经算第二天
    assert logical_date(datetime(2026, 10, 20, 23, 30, tzinfo=TZ), 23.0) == date(2026, 10, 21)


def test_diary_roundtrip_and_persistence(tmp_path):
    d = diary(TODAY, importance="important", mood_v=0.123456, tags=["B站"], body="今天---很开心")
    assert parse_diary(format_diary(d)).meta.mood_v == 0.123

    mem = Memory(tmp_path)
    mem.write_diary(d)
    mem.set_notebook("## 主人\n- 喜欢喝咖啡")
    mem.add_notes(["主人周末起得晚", "主人周末起得晚", "  "])
    assert (tmp_path / "diary" / "2026" / "10" / "20.md").exists()

    again = Memory(tmp_path)
    loaded = again.diary(TODAY)
    assert loaded is not None and loaded.body == "今天---很开心"
    assert loaded.meta.tags == ["B站"] and loaded.meta.importance == "important"
    assert again.notebook.startswith("## 主人")
    assert again.inbox == ["主人周末起得晚"]
    again.clear_inbox()
    assert Memory(tmp_path).inbox == []


def test_broken_diary_file_is_skipped(tmp_path):
    mem = Memory(tmp_path)
    mem.write_diary(diary(TODAY))
    bad = tmp_path / "diary" / "2026" / "10" / "19.md"
    bad.write_text("没有 front-matter", encoding="utf-8")
    assert [x.meta.date for x in Memory(tmp_path).diaries] == [TODAY]


def test_view_sections_and_order():
    mem = Memory(params=MemoryParams(sample_k=5))
    mem.write_diary(diary(TODAY - timedelta(days=400), importance="important"))
    mem.write_diary(diary(TODAY - timedelta(days=200), summary="很久以前"))
    for age in (1, 2, 30, 31):
        mem.write_diary(diary(TODAY - timedelta(days=age)))

    # 想起的日记已经被抽中时不重复；不存在的日期忽略
    view = mem.view(TODAY, recalls={}, recalled=[TODAY - timedelta(days=200), TODAY])
    sections = [(d.section, (TODAY - d.meta.date).days) for d in view.diaries]
    assert sections == [
        ("important", 400),
        ("recent", 30),
        ("recent", 2),
        ("recent", 1),
        ("sampled", 200),
        ("sampled", 31),
    ]
    # 超过 90 天的普通日记只注入摘要；重要日记始终是全文
    by_age = {(TODAY - d.meta.date).days: d for d in view.diaries}
    assert by_age[400].body is not None
    assert by_age[200].body is None and by_age[31].body is not None


def test_recalled_diaries_are_appended_after_sampled():
    mem = Memory(params=MemoryParams(sample_k=0))
    old = TODAY - timedelta(days=100)
    mem.write_diary(diary(old))
    mem.write_diary(diary(TODAY - timedelta(days=1)))
    view = mem.view(TODAY, recalls={}, recalled=[old])
    assert [d.section for d in view.diaries] == ["recent", "recalled"]


def test_sampling_is_stable_within_a_day_and_favors_recall_and_emotion():
    p = MemoryParams(sample_k=1)
    metas = [
        DiaryMeta(date=TODAY - timedelta(days=100), mood_peak=0.1),
        DiaryMeta(date=TODAY - timedelta(days=101), mood_peak=0.1),
        DiaryMeta(date=TODAY - timedelta(days=102), mood_peak=0.9),
    ]
    recalled = {metas[1].date: 8}
    assert sample_old(metas, TODAY, recalled, p) == sample_old(metas, TODAY, recalled, p)

    counts = dict.fromkeys((m.date for m in metas), 0)
    for i in range(2000):
        (d,) = sample_old(metas, TODAY + timedelta(days=i), recalled, p)
        counts[d] += 1
    plain, rehearsed, intense = (counts[m.date] for m in metas)
    assert rehearsed > plain * 3
    assert intense > plain * 3


def test_recent_and_important_diaries_are_not_sampled():
    p = MemoryParams(sample_k=5)
    metas = [
        DiaryMeta(date=TODAY - timedelta(days=5)),
        DiaryMeta(date=TODAY - timedelta(days=60), importance="important"),
    ]
    assert sample_old(metas, TODAY, {}, p) == []


def test_search_scores_tags_over_body_and_respects_exclude():
    mem = Memory()
    a, b, c = (TODAY - timedelta(days=n) for n in (50, 60, 70))
    mem.write_diary(diary(a, tags=["西湖"], body="和主人去散步"))
    mem.write_diary(diary(b, summary="聊起西湖", body="……"))
    mem.write_diary(diary(c, body="提了一句西湖"))
    assert mem.search(["西湖"], exclude=set(), limit=3) == [a, b, c]
    assert mem.search(["西湖"], exclude={a}, limit=1) == [b]
    assert mem.search(["火星"], exclude=set(), limit=3) == []


def test_important_quota_per_month():
    mem = Memory(params=MemoryParams(important_per_month=2))
    mem.write_diary(diary(date(2026, 10, 3), importance="important"))
    mem.write_diary(diary(date(2026, 10, 9), importance="important"))
    mem.write_diary(diary(date(2026, 9, 30), importance="important"))
    assert mem.important_left(TODAY) == 0
    # 重写已经是重要日记的那天不占新名额
    assert mem.important_left(date(2026, 10, 9)) == 1
    assert mem.important_left(date(2026, 11, 1)) == 2
