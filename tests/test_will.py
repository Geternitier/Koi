from datetime import UTC, datetime, timedelta

from koi.persona.params import WillParams
from koi.persona.will import RequestAssessment, WillEntry, decide

P = WillParams()
T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
GOOD = {"valence": 0.3, "arousal": 0.0, "energy": 80.0, "affinity": 0.5}
TIRED = {"valence": 0.0, "arousal": 0.0, "energy": 10.0, "affinity": 0.5}


def req(
    intent="整理目录", *, same_as=None, aversion="neutral", insistence="normal", kind="request"
):
    return RequestAssessment(
        event=1,
        intent=intent,
        same_as=same_as,
        aversion=aversion,
        insistence=insistence,
        kind=kind,
    )


def test_neutral_request_in_good_mood_is_eager():
    ledger: dict[str, WillEntry] = {}
    d = decide(req(), ledger, now=T0, p=P, **GOOD)
    assert d.tier == "eager" and d.reason is None
    assert ledger == {}


def test_tired_and_disliked_is_refused_with_dominant_reason():
    ledger: dict[str, WillEntry] = {}
    d = decide(req(aversion="hate"), ledger, now=T0, p=P, **TIRED)
    assert d.tier == "refuse"
    assert d.reason == "aversion"
    assert ledger["整理目录"].refusals == 1


def test_irritation_only_counts_when_valence_negative():
    ledger: dict[str, WillEntry] = {}
    calm = decide(req(), ledger, now=T0, p=P, valence=0.2, arousal=0.8, energy=50, affinity=0.5)
    cross = decide(req(), {}, now=T0, p=P, valence=-0.2, arousal=0.8, energy=50, affinity=0.5)
    assert calm.contributions["irritation"] == 0
    assert cross.contributions["irritation"] < 0
    assert cross.reason == "irritation"


def test_two_strong_insists_force_reluctant_compliance():
    ledger: dict[str, WillEntry] = {}
    first = decide(req(aversion="hate", insistence="strong"), ledger, now=T0, p=P, **TIRED)
    assert first.tier == "refuse"
    second = decide(
        req("快去整理", same_as="整理目录", aversion="hate", insistence="strong"),
        ledger,
        now=T0 + timedelta(minutes=1),
        p=P,
        **TIRED,
    )
    assert second.tier == "reluctant" and second.forced
    assert second.intent == "整理目录" and second.asks == 2
    assert ledger == {}


def test_normal_repeats_build_pressure_until_stubbornness():
    ledger: dict[str, WillEntry] = {}
    tiers = []
    for i in range(5):
        d = decide(
            req(same_as="整理目录", aversion="hate"),
            ledger,
            now=T0 + timedelta(minutes=i),
            p=P,
            **TIRED,
        )
        tiers.append(d.tier)
        if d.tier == "reluctant":
            break
    # 第 1 次不计压力，之后每次 +1，P_max = 3
    assert tiers == ["refuse", "refuse", "refuse", "reluctant"]


def test_pressure_decays_and_ledger_expires():
    ledger: dict[str, WillEntry] = {}
    decide(req(aversion="hate"), ledger, now=T0, p=P, **TIRED)
    decide(req(same_as="整理目录", aversion="hate"), ledger, now=T0, p=P, **TIRED)
    assert ledger["整理目录"].pressure == 1.0
    later = T0 + timedelta(hours=4, minutes=P.pressure_grace_min)
    decide(req(same_as="整理目录", aversion="hate"), ledger, now=later, p=P, **TIRED)
    assert ledger["整理目录"].pressure == 0.25 + 1.0
    much_later = later + timedelta(hours=P.ledger_ttl_h + 1)
    d = decide(req(same_as="整理目录", aversion="hate"), ledger, now=much_later, p=P, **TIRED)
    assert d.asks == 1


def test_stop_is_always_obeyed():
    d = decide(
        req("停下", kind="stop"), {}, now=T0, p=P, valence=-1, arousal=1, energy=0, affinity=-1
    )
    assert d.tier == "stop"
