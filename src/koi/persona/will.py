"""意愿与拒绝：对主人请求计算意愿度、分档，并维护坚持压力账本。

决策由这里确定性地给出，LLM 只负责表达。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from koi.kernel.clock import hours_between
from koi.persona.params import WillParams

Aversion = Literal["like", "neutral", "dislike", "hate"]
Tier = Literal["eager", "bargain", "refuse", "reluctant", "stop"]

REASON_TEXT = {
    "aversion": "不喜欢做这类事",
    "mood": "心情不好",
    "energy": "太累了",
    "irritation": "正烦躁着",
    "affinity": "对主人还有点情绪",
}


class RequestAssessment(BaseModel):
    """LLM 从主人消息中识别出的请求。"""

    event: int
    intent: str
    # 与账本中某条是同一件事时，原样填写那条的 intent
    same_as: str | None = None
    kind: Literal["request", "stop"] = "request"
    aversion: Aversion = "neutral"
    insistence: Literal["normal", "strong"] = "normal"


class WillEntry(BaseModel):
    intent: str
    first_asked: datetime
    last_asked: datetime
    asks: int = 1
    refusals: int = 0
    pressure: float = 0.0
    strong_insists: int = 0


class WillDecision(BaseModel):
    event: int
    intent: str
    tier: Tier
    willingness: float
    contributions: dict[str, float]
    reason: str | None = None
    asks: int = 1
    forced: bool = False

    @property
    def reason_text(self) -> str | None:
        return REASON_TEXT.get(self.reason) if self.reason else None


def willingness(
    aversion: Aversion,
    *,
    valence: float,
    arousal: float,
    energy: float,
    affinity: float,
    p: WillParams,
) -> tuple[float, dict[str, float]]:
    aversion_value = {
        "like": p.aversion_like,
        "neutral": p.aversion_neutral,
        "dislike": p.aversion_dislike,
        "hate": p.aversion_hate,
    }[aversion]
    contributions = {
        "base": p.base,
        "aversion": -aversion_value,
        "mood": p.coef_valence * valence,
        "energy": p.coef_energy * (energy - 50) / 50,
        "irritation": -p.coef_irritation * max(0.0, arousal) if valence < 0 else 0.0,
        "affinity": p.coef_affinity * affinity,
    }
    return sum(contributions.values()), contributions


def prune_ledger(ledger: dict[str, WillEntry], now: datetime, p: WillParams) -> None:
    for key in [k for k, e in ledger.items() if hours_between(e.last_asked, now) > p.ledger_ttl_h]:
        del ledger[key]


def decide(
    req: RequestAssessment,
    ledger: dict[str, WillEntry],
    *,
    now: datetime,
    valence: float,
    arousal: float,
    energy: float,
    affinity: float,
    p: WillParams,
) -> WillDecision:
    """决定对一个请求的态度，并就地更新账本。"""
    prune_ledger(ledger, now, p)
    if req.kind == "stop":
        return WillDecision(
            event=req.event, intent=req.intent, tier="stop", willingness=1.0, contributions={}
        )

    key = req.same_as if req.same_as in ledger else req.intent
    strong = req.insistence == "strong"
    entry = ledger.get(key)
    if entry is None:
        entry = WillEntry(intent=key, first_asked=now, last_asked=now)
    else:
        idle_h = max(0.0, hours_between(entry.last_asked, now) - p.pressure_grace_min / 60)
        decay = 0.5 ** (idle_h / p.pressure_half_life_h)
        entry.pressure = entry.pressure * decay + (
            p.pressure_strong if strong else p.pressure_normal
        )
        entry.asks += 1
        entry.last_asked = now
    if strong:
        entry.strong_insists += 1

    w, contributions = willingness(
        req.aversion, valence=valence, arousal=arousal, energy=energy, affinity=affinity, p=p
    )
    p_max = p.stubbornness * (1 + p.mood_stubbornness * max(0.0, -valence))
    forced = entry.pressure >= p_max or entry.strong_insists >= p.strong_insist_limit

    tier: Tier
    if w >= p.tau_high:
        tier = "eager"
    elif forced:
        tier = "reluctant"
    elif w >= p.tau_low:
        tier = "bargain"
    else:
        tier = "refuse"

    reason = None
    if tier != "eager":
        negatives = {k: v for k, v in contributions.items() if k != "base" and v < 0}
        if negatives:
            reason = min(negatives, key=lambda k: negatives[k])

    if tier in ("bargain", "refuse"):
        entry.refusals += 1
        ledger[key] = entry
    else:
        ledger.pop(key, None)

    return WillDecision(
        event=req.event,
        intent=key,
        tier=tier,
        willingness=w,
        contributions=contributions,
        reason=reason,
        asks=entry.asks,
        forced=tier == "reluctant",
    )
