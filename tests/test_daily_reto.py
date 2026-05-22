"""Verifica que el reto se inyecta solo si la noche fue corta (<7h10)."""

from report import _build_user_message


def _ctx(total_min):
    return {
        "last_night": {
            "night_date": "2026-05-15",
            "total_sleep_minutes": total_min,
            "in_bed_minutes": 450,
            "rem_minutes": 80,
            "core_minutes": 240,
            "deep_minutes": 70,
            "awake_minutes": 20,
            "hrv_sdnn_ms": 58, "resting_hr_bpm": 53,
            "avg_hr_bpm": 60, "min_hr_bpm": 48, "max_hr_bpm": 90,
            "respiratory_rate_brpm": 14.2,
        },
        "baseline": {"sleep_min": 440, "deep_min": 72, "rem_min": 80,
                     "hrv": 58, "rhr": 53, "rr": 14.2},
        "n_baseline": 7,
    }


def test_no_reto_when_above_threshold():
    msg = _build_user_message(_ctx(440))   # 7h20
    assert "FLAG INTERNO" not in msg
    assert "umbral autoimpuesto" not in msg


def test_reto_when_exactly_at_threshold_minus_1():
    msg = _build_user_message(_ctx(429))   # 7h09
    assert "FLAG INTERNO" in msg
    assert "7h10" in msg
    assert "reproche" in msg.lower() or "no le suaves" in msg.lower()


def test_no_reto_at_threshold():
    msg = _build_user_message(_ctx(430))   # 7h10 exacto, no rete
    assert "FLAG INTERNO" not in msg


def test_reto_when_short():
    msg = _build_user_message(_ctx(360))   # 6h
    assert "FLAG INTERNO" in msg
    assert "6h00" in msg


def test_handles_missing_total_sleep():
    ctx = _ctx(None)
    msg = _build_user_message(ctx)
    # No debería romper ni inyectar reto
    assert "FLAG INTERNO" not in msg
