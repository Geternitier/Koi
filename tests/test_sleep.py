from datetime import UTC, datetime, timedelta

import pytest

from koi.persona.params import SleepParams
from koi.persona.sleep import (
    base_energy,
    pressure_after,
    reference_mid_sleep_offset,
    thresholds,
    wrap_hours,
)

P = SleepParams()


def test_pressure_closed_form_composes():
    once = pressure_after(0.2, 6.0, asleep=False, p=P)
    twice = pressure_after(pressure_after(0.2, 2.0, asleep=False, p=P), 4.0, asleep=False, p=P)
    assert once == pytest.approx(twice)
    assert pressure_after(0.6, 1e4, asleep=True, p=P) == pytest.approx(0.0)
    assert pressure_after(0.2, 1e4, asleep=False, p=P) == pytest.approx(1.0)


def test_energy_is_full_at_wake_threshold_and_empty_past_margin():
    t = datetime(2026, 1, 1, 7, tzinfo=UTC)
    upper, lower = thresholds(t, P.circadian_peak_hour, P)
    assert base_energy(lower, t, P.circadian_peak_hour, P) == pytest.approx(100)
    assert base_energy(upper + P.stay_up_margin, t, P.circadian_peak_hour, P) == pytest.approx(0)


def test_default_params_entrain_to_a_plausible_night():
    mid = (P.circadian_peak_hour + reference_mid_sleep_offset(P)) % 24
    assert 1.5 <= mid <= 5.0


def test_free_run_sleep_duration():
    step = timedelta(minutes=5)
    h = step.total_seconds() / 3600
    t = datetime(2026, 1, 1, 8, tzinfo=UTC)
    s, asleep, onset, durations = 0.2, False, t, []
    for _ in range(int(5 * 24 / h)):
        s = pressure_after(s, h, asleep=asleep, p=P)
        t += step
        upper, lower = thresholds(t, P.circadian_peak_hour, P)
        if not asleep and s >= upper:
            asleep, onset = True, t
        elif asleep and s <= lower:
            asleep = False
            durations.append((t - onset).total_seconds() / 3600)
    assert all(7 <= d <= 10 for d in durations[1:])


def test_wrap_hours():
    assert wrap_hours(23) == -1
    assert wrap_hours(-13) == 11
    assert wrap_hours(12) == -12
