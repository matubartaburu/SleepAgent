"""Unit tests para helpers puros de main.py."""

from datetime import date

from main import _hours_to_min, _index_by_date, _num, _parse_dt, _to_pg_ts


def test_hours_to_min_basic():
    assert _hours_to_min(7.5) == 450
    assert _hours_to_min(0) == 0
    assert _hours_to_min(1.2) == 72


def test_hours_to_min_invalid():
    assert _hours_to_min(None) is None
    assert _hours_to_min("garbage") is None
    assert _hours_to_min([]) is None


def test_num_round_to_2_decimals():
    # Python usa banker's rounding, así que evitamos el caso ambiguo de .x5
    assert _num(58.346) == 58.35
    assert _num(12.341) == 12.34
    assert _num("12.6") == 12.6
    assert _num(0) == 0.0


def test_num_invalid():
    assert _num(None) is None
    assert _num("xx") is None


def test_to_pg_ts_returns_iso():
    ts = _to_pg_ts("2026-05-15 08:00:00 -0300")
    assert ts is not None
    assert ts.startswith("2026-05-15T08:00:00")
    # debe traer offset
    assert "-03:00" in ts or "-0300" in ts


def test_to_pg_ts_invalid():
    assert _to_pg_ts(None) is None
    assert _to_pg_ts("") is None
    assert _to_pg_ts("not-a-date") is None


def test_parse_dt_roundtrip():
    dt = _parse_dt("2026-05-15 08:00:00 -0300")
    assert dt.year == 2026
    assert dt.month == 5
    assert dt.day == 15


def test_index_by_date_indexes_correctly():
    metric = {
        "data": [
            {"date": "2026-05-15 00:00:00 -0300", "qty": 50},
            {"date": "2026-05-14 00:00:00 -0300", "qty": 60},
        ]
    }
    idx = _index_by_date(metric)
    assert idx[date(2026, 5, 15)]["qty"] == 50
    assert idx[date(2026, 5, 14)]["qty"] == 60


def test_index_by_date_skips_invalid():
    metric = {
        "data": [
            {"date": "2026-05-15 00:00:00 -0300", "qty": 50},
            {"qty": 99},                  # sin date
            {"date": "garbage", "qty": 1},
        ]
    }
    idx = _index_by_date(metric)
    assert len(idx) == 1


def test_index_by_date_none_or_empty():
    assert _index_by_date(None) == {}
    assert _index_by_date({"data": []}) == {}
    assert _index_by_date({}) == {}
