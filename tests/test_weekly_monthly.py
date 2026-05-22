"""Tests de weekly + monthly contexts (Supabase mockeado)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _row(date_str, total_min, deep=70, awake=20, hrv=58, rhr=53, rr=14.2,
         start="T23:30:00-03:00", end="T07:00:00-03:00"):
    return {
        "night_date": date_str,
        "total_sleep_minutes": total_min,
        "in_bed_minutes": total_min + 25,
        "rem_minutes": 80,
        "core_minutes": 240,
        "deep_minutes": deep,
        "awake_minutes": awake,
        "hrv_sdnn_ms": hrv,
        "resting_hr_bpm": rhr,
        "respiratory_rate_brpm": rr,
        "sleep_start": f"{date_str.replace('2026-05-', '2026-05-')}{start}",
        "sleep_end":   f"{date_str}{end}",
    }


def _patch_supabase(rows, notes=None, prev_rows=None):
    """
    Helper para mockear el chaineo de Supabase. El weekly hace 1 query a
    sleep_logs y 1 a sleep_notes. El monthly hace 1 a sleep_logs (curr),
    1 a sleep_notes, y 1 a sleep_logs (prev).
    """
    queries: list[MagicMock] = []

    def _new_query(data):
        q = MagicMock()
        for method in ("select", "gte", "lte", "lt", "order", "limit"):
            getattr(q, method).return_value = q
        q.execute.return_value = MagicMock(data=data)
        return q

    sleep_logs_q1 = _new_query(rows)
    sleep_notes_q = _new_query(notes or [])
    sleep_logs_q2 = _new_query(prev_rows or [])

    table_calls = {"sleep_logs": [sleep_logs_q1, sleep_logs_q2],
                   "sleep_notes": [sleep_notes_q]}

    def table_side_effect(name):
        return table_calls[name].pop(0) if table_calls.get(name) else _new_query([])

    fake_client = MagicMock()
    fake_client.table.side_effect = table_side_effect
    return fake_client


def test_weekly_context_returns_none_without_rows():
    from report import _build_weekly_context
    fake = _patch_supabase([])
    with patch("report._supabase", return_value=fake):
        assert _build_weekly_context(days=7) is None


def test_weekly_context_aggregates():
    from report import _build_weekly_context
    rows = [
        _row("2026-05-15", 420),
        _row("2026-05-14", 480),
        _row("2026-05-13", 390),
        _row("2026-05-12", 450),
        _row("2026-05-11", 510),
    ]
    fake = _patch_supabase(rows)
    with patch("report._supabase", return_value=fake):
        ctx = _build_weekly_context(days=7)

    assert ctx is not None
    assert ctx["n_nights"] == 5
    assert ctx["sleep_min_avg"] == pytest.approx(450)
    # Buckets: 360-419 6-7h | 420-479 7-8h | 480+ 8h+
    assert ctx["distribution"]["6-7h"] == 1     # 390
    assert ctx["distribution"]["7-8h"] == 2     # 420, 450
    assert ctx["distribution"]["8h+"] == 2      # 480, 510
    assert ctx["best_night"]["total_sleep_minutes"] == 510
    assert ctx["worst_night"]["total_sleep_minutes"] == 390


def test_weekly_user_message_includes_reto_when_avg_below():
    from report import _build_weekly_user_message
    ctx = {
        "label": "test", "period_days": 7, "n_nights": 5,
        "sleep_min_avg": 400,     # 6h40, debajo de 7h10
        "deep_avg": 60, "rem_avg": 80, "awake_avg": 25,
        "hrv_avg": 55, "rhr_avg": 55, "rr_avg": 15,
        "bedtime_sd_min": 30, "bedtime_mean_min": 700,
        "waketime_sd_min": 20, "waketime_mean_min": 420,
        "distribution": {"<6h": 1, "6-7h": 2, "7-8h": 2, "8h+": 0},
        "best_night": None, "worst_night": None,
        "tag_counts": {},
    }
    msg = _build_weekly_user_message(ctx)
    assert "FLAG INTERNO" in msg
    assert "7h10" in msg


def test_weekly_user_message_no_reto_when_avg_above():
    from report import _build_weekly_user_message
    ctx = {
        "label": "test", "period_days": 7, "n_nights": 5,
        "sleep_min_avg": 460,
        "deep_avg": 75, "rem_avg": 80, "awake_avg": 20,
        "hrv_avg": 58, "rhr_avg": 53, "rr_avg": 14.2,
        "bedtime_sd_min": 20, "bedtime_mean_min": 700,
        "waketime_sd_min": 15, "waketime_mean_min": 420,
        "distribution": {"<6h": 0, "6-7h": 1, "7-8h": 3, "8h+": 1},
        "best_night": None, "worst_night": None,
        "tag_counts": {},
    }
    msg = _build_weekly_user_message(ctx)
    assert "FLAG INTERNO" not in msg


def test_weekly_user_message_includes_tag_counts():
    from report import _build_weekly_user_message
    ctx = {
        "label": "test", "period_days": 7, "n_nights": 5,
        "sleep_min_avg": 460,
        "deep_avg": 75, "rem_avg": 80, "awake_avg": 20,
        "hrv_avg": 58, "rhr_avg": 53, "rr_avg": 14.2,
        "bedtime_sd_min": 20, "bedtime_mean_min": 700,
        "waketime_sd_min": 15, "waketime_mean_min": 420,
        "distribution": {"<6h": 0, "6-7h": 1, "7-8h": 3, "8h+": 1},
        "best_night": None, "worst_night": None,
        "tag_counts": {"comida_tarde": 2, "alcohol": 1},
    }
    msg = _build_weekly_user_message(ctx)
    assert "comida_tarde" in msg
    assert "2" in msg


def test_monthly_context_compares_with_previous_month():
    from report import _build_monthly_context
    curr_rows = [_row(f"2026-05-{d:02d}", 450) for d in range(1, 16)]
    prev_rows = [_row(f"2026-04-{d:02d}", 420) for d in range(1, 16)]

    fake = _patch_supabase(curr_rows, prev_rows=prev_rows)
    with patch("report._supabase", return_value=fake):
        ctx = _build_monthly_context(days=30)

    assert ctx is not None
    assert ctx["n_nights"] == 15
    assert ctx["sleep_min_avg"] == pytest.approx(450)
    assert ctx["prev_sleep_min_avg"] == pytest.approx(420)
    assert ctx["prev_n_nights"] == 15


def test_monthly_user_message_includes_comparison():
    from report import _build_monthly_user_message
    ctx = {
        "label": "test mes", "period_days": 30, "n_nights": 30,
        "sleep_min_avg": 460,
        "deep_avg": 75, "rem_avg": 80, "awake_avg": 20,
        "hrv_avg": 58, "rhr_avg": 53, "rr_avg": 14.2,
        "bedtime_sd_min": 20, "bedtime_mean_min": 700,
        "waketime_sd_min": 15, "waketime_mean_min": 420,
        "distribution": {"<6h": 1, "6-7h": 5, "7-8h": 20, "8h+": 4},
        "best_night": None, "worst_night": None,
        "tag_counts": {},
        "prev_sleep_min_avg": 420,
        "prev_hrv_avg": 55, "prev_rhr_avg": 55,
        "prev_n_nights": 30,
    }
    msg = _build_monthly_user_message(ctx)
    assert "COMPARACIÓN VS MES ANTERIOR" in msg
    assert "MENSUAL" in msg
    # +40 min de mejora
    assert "+40 min" in msg
