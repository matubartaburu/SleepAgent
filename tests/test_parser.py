"""Unit tests para parse_health_auto_export."""

from main import parse_health_auto_export


def test_parses_single_night(sample_payload):
    rows = parse_health_auto_export(sample_payload)
    assert len(rows) == 1
    r = rows[0]
    assert r["night_date"] == "2026-05-15"
    assert r["source"] == "webhook"
    assert r["total_sleep_minutes"] == 432   # 7.20h
    assert r["rem_minutes"] == 87
    assert r["deep_minutes"] == 66
    assert r["awake_minutes"] == 33


def test_in_bed_minutes_from_timestamps(sample_payload):
    rows = parse_health_auto_export(sample_payload)
    # inBedStart 00:15 → inBedEnd 07:50 = 7h35min = 455min
    assert rows[0]["in_bed_minutes"] == 455


def test_joins_associated_metrics(sample_payload):
    r = parse_health_auto_export(sample_payload)[0]
    assert r["hrv_sdnn_ms"] == 58.3
    assert r["resting_hr_bpm"] == 52.0
    assert r["avg_hr_bpm"] == 61.4
    assert r["min_hr_bpm"] == 48.0
    assert r["max_hr_bpm"] == 88.0
    assert r["respiratory_rate_brpm"] == 14.2


def test_returns_empty_when_no_sleep_metric():
    payload = {"data": {"metrics": [
        {"name": "heart_rate_variability", "data": [
            {"date": "2026-05-15 00:00:00 -0300", "qty": 50}
        ]},
    ]}}
    assert parse_health_auto_export(payload) == []


def test_returns_empty_when_sleep_data_empty():
    payload = {"data": {"metrics": [
        {"name": "sleep_analysis", "data": []},
    ]}}
    assert parse_health_auto_export(payload) == []


def test_handles_empty_payload():
    assert parse_health_auto_export({}) == []
    assert parse_health_auto_export({"data": {}}) == []
    assert parse_health_auto_export({"data": {"metrics": []}}) == []


def test_skips_sleep_item_without_valid_date():
    payload = {"data": {"metrics": [
        {"name": "sleep_analysis", "data": [
            {"date": "not-a-date", "totalSleep": 7},
            {"date": "2026-05-15 08:00:00 -0300", "totalSleep": 7},
        ]},
    ]}}
    rows = parse_health_auto_export(payload)
    assert len(rows) == 1
    assert rows[0]["night_date"] == "2026-05-15"


def test_unmatched_metric_dates_yield_none(sample_payload):
    # Mover HRV un día atrás → no debería matchear
    for m in sample_payload["data"]["metrics"]:
        if m["name"] == "heart_rate_variability":
            m["data"][0]["date"] = "2026-05-13 00:00:00 -0300"
    r = parse_health_auto_export(sample_payload)[0]
    assert r["hrv_sdnn_ms"] is None
    # Las demás siguen matcheando
    assert r["resting_hr_bpm"] == 52.0


def test_two_nights_produce_two_rows(sample_payload_two_nights):
    rows = parse_health_auto_export(sample_payload_two_nights)
    assert len(rows) == 2
    dates = sorted(r["night_date"] for r in rows)
    assert dates == ["2026-05-14", "2026-05-15"]


def test_raw_payload_preserved(sample_payload):
    r = parse_health_auto_export(sample_payload)[0]
    # Tras el fix multi-session, raw_payload usa 'sessions' (array).
    assert "sessions" in r["raw_payload"]
    assert r["raw_payload"]["n_sessions"] == 1
    assert r["raw_payload"]["sessions"][0]["totalSleep"] == 7.20
    assert r["raw_payload"]["hrv"]["qty"] == 58.3
