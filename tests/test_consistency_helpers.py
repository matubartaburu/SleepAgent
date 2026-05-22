"""Helpers de consistencia y reto en report.py."""

from report import (
    SLEEP_MIN_THRESHOLD,
    _consistency_label,
    _detect_anomalies,
    _minutes_since_midnight_to_clock,
    _minutes_to_clock,
    _stddev_minutes,
    _to_minutes_since_midnight,
    _to_minutes_since_noon,
)


def test_threshold_is_7h10():
    assert SLEEP_MIN_THRESHOLD == 430


def test_to_minutes_since_noon_evening():
    # 23:30 → 11h después de mediodía → 11*60+30 = 690
    assert _to_minutes_since_noon("2026-05-15T23:30:00-03:00") == 690


def test_to_minutes_since_noon_after_midnight_continuous():
    # 00:30 → 12*60+30 = 750 (continuo con bedtimes <00)
    assert _to_minutes_since_noon("2026-05-15T00:30:00-03:00") == 750


def test_to_minutes_since_noon_handles_space_format():
    # Postgres a veces devuelve con espacio en vez de T
    assert _to_minutes_since_noon("2026-05-15 01:00:00-03:00") == 780


def test_to_minutes_since_noon_invalid():
    assert _to_minutes_since_noon(None) is None
    assert _to_minutes_since_noon("") is None
    assert _to_minutes_since_noon("garbage") is None


def test_to_minutes_since_midnight():
    assert _to_minutes_since_midnight("2026-05-15T07:30:00-03:00") == 7 * 60 + 30
    assert _to_minutes_since_midnight("2026-05-15T11:00:00-03:00") == 660


def test_stddev_minutes_with_values():
    # 5 valores idénticos → SD = 0
    assert _stddev_minutes([700, 700, 700, 700, 700]) == 0
    # Algo de spread
    sd = _stddev_minutes([690, 700, 710, 720, 730])
    assert sd is not None and 13 < sd < 16


def test_stddev_minutes_ignores_none():
    sd = _stddev_minutes([700, None, 700, None, 700])
    assert sd == 0


def test_stddev_minutes_too_few_values():
    assert _stddev_minutes([]) is None
    assert _stddev_minutes([700]) is None
    assert _stddev_minutes([None, None]) is None


def test_consistency_label_buckets():
    assert _consistency_label(10) == "muy consistente"
    assert _consistency_label(25) == "muy consistente"
    assert _consistency_label(40) == "bastante consistente"
    assert _consistency_label(70) == "variable"
    assert _consistency_label(120) == "muy variable"
    assert _consistency_label(None) == "sin data"


def test_minutes_to_clock_roundtrip():
    # 690 = 23:30 (bedtime)
    assert _minutes_to_clock(690) == "23:30"
    # 750 = 00:30 (after midnight)
    assert _minutes_to_clock(750) == "00:30"
    assert _minutes_to_clock(None) == "—"


def test_minutes_since_midnight_to_clock():
    assert _minutes_since_midnight_to_clock(7 * 60 + 30) == "07:30"
    assert _minutes_since_midnight_to_clock(None) == "—"


def test_detect_anomalies_short_sleep():
    night = {"total_sleep_minutes": 360, "deep_minutes": 70, "awake_minutes": 30,
             "hrv_sdnn_ms": 60, "resting_hr_bpm": 52, "respiratory_rate_brpm": 14}
    baseline = {"deep_min": 70, "hrv": 60, "rhr": 52, "rr": 14}
    anomalies = _detect_anomalies(night, baseline)
    assert any("sueño_corto" in a for a in anomalies)


def test_detect_anomalies_deep_low():
    night = {"total_sleep_minutes": 450, "deep_minutes": 30, "awake_minutes": 30,
             "hrv_sdnn_ms": 60, "resting_hr_bpm": 52, "respiratory_rate_brpm": 14}
    baseline = {"deep_min": 90, "hrv": 60, "rhr": 52, "rr": 14}
    anomalies = _detect_anomalies(night, baseline)
    assert any("deep_flaco" in a for a in anomalies)


def test_detect_anomalies_clean_night():
    night = {"total_sleep_minutes": 460, "deep_minutes": 80, "awake_minutes": 25,
             "hrv_sdnn_ms": 58, "resting_hr_bpm": 53, "respiratory_rate_brpm": 14.2}
    baseline = {"deep_min": 75, "hrv": 60, "rhr": 52, "rr": 14}
    anomalies = _detect_anomalies(night, baseline)
    assert anomalies == []
