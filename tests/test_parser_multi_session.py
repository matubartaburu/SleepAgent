"""Test del fix: combinar múltiples sleep_analysis items para una misma night_date."""

from __future__ import annotations

from main import parse_health_auto_export


def _payload(sessions: list[dict]) -> dict:
    return {"data": {"metrics": [{"name": "sleep_analysis", "data": sessions}]}}


def test_two_sessions_same_night_are_combined():
    """Caso real del 16-may: Mateo durmió 22:59 → 05:41, despertó, luego 05:43 → 07:27."""
    sessions = [
        {
            "date":       "2026-05-16 07:30:00 -0300",
            "sleepStart": "2026-05-15 22:59:00 -0300",
            "sleepEnd":   "2026-05-16 05:41:00 -0300",
            "inBedStart": "2026-05-15 22:55:00 -0300",
            "inBedEnd":   "2026-05-16 05:41:00 -0300",
            "totalSleep": 6.70,   # 6h42
            "rem":  1.5,
            "core": 4.0,
            "deep": 1.0,
            "awake": 0.2,
        },
        {
            "date":       "2026-05-16 07:30:00 -0300",
            "sleepStart": "2026-05-16 05:43:00 -0300",
            "sleepEnd":   "2026-05-16 07:27:00 -0300",
            "inBedStart": "2026-05-16 05:43:00 -0300",
            "inBedEnd":   "2026-05-16 07:27:00 -0300",
            "totalSleep": 1.40,   # 1h24
            "rem":  0.3,
            "core": 1.0,
            "deep": 0.0,
            "awake": 0.1,
        },
    ]
    rows = parse_health_auto_export(_payload(sessions))

    assert len(rows) == 1, "Las dos sesiones de la MISMA night_date deben combinarse en 1 fila"
    r = rows[0]
    assert r["night_date"] == "2026-05-16"

    # Sleep_start = el más temprano (22:59)
    assert "22:59:00" in r["sleep_start"]
    # Sleep_end = el más tarde (07:27)
    assert "07:27:00" in r["sleep_end"]

    # Total dormido = suma de ambas sesiones (no la duración de extremos)
    # 6.70 + 1.40 = 8.10h = 486 minutos
    assert r["total_sleep_minutes"] == 486

    # REM/Core/Deep también suman
    assert r["rem_minutes"] == int(round((1.5 + 0.3) * 60))   # 108
    assert r["core_minutes"] == int(round((4.0 + 1.0) * 60))  # 300
    assert r["deep_minutes"] == int(round((1.0 + 0.0) * 60))  # 60
    # awake_minutes: 0.2 + 0.1 = 0.3h → 18
    assert r["awake_minutes"] == 18

    # raw_payload debe tener 2 sessions
    assert r["raw_payload"]["n_sessions"] == 2


def test_single_session_still_works():
    """El caso normal (1 sesión por noche) sigue funcionando igual."""
    sessions = [{
        "date":       "2026-05-15 08:00:00 -0300",
        "sleepStart": "2026-05-15 00:30:00 -0300",
        "sleepEnd":   "2026-05-15 07:50:00 -0300",
        "inBedStart": "2026-05-15 00:15:00 -0300",
        "inBedEnd":   "2026-05-15 07:55:00 -0300",
        "totalSleep": 7.20,
        "rem": 1.45, "core": 4.0, "deep": 1.0, "awake": 0.55,
    }]
    rows = parse_health_auto_export(_payload(sessions))
    assert len(rows) == 1
    r = rows[0]
    assert r["night_date"] == "2026-05-15"
    assert r["total_sleep_minutes"] == 432  # 7.2 * 60
    assert r["raw_payload"]["n_sessions"] == 1


def test_three_sessions_combine_correctly():
    """Caso extremo: 3 sesiones para la misma noche."""
    sessions = [
        {"date": "2026-05-20 08:00:00 -0300",
         "sleepStart": "2026-05-19 23:00:00 -0300",
         "sleepEnd":   "2026-05-20 02:00:00 -0300",
         "totalSleep": 3.0},
        {"date": "2026-05-20 08:00:00 -0300",
         "sleepStart": "2026-05-20 02:30:00 -0300",
         "sleepEnd":   "2026-05-20 05:00:00 -0300",
         "totalSleep": 2.5},
        {"date": "2026-05-20 08:00:00 -0300",
         "sleepStart": "2026-05-20 05:30:00 -0300",
         "sleepEnd":   "2026-05-20 07:30:00 -0300",
         "totalSleep": 2.0},
    ]
    rows = parse_health_auto_export(_payload(sessions))
    assert len(rows) == 1
    r = rows[0]
    assert r["total_sleep_minutes"] == int((3.0 + 2.5 + 2.0) * 60)   # 450
    assert "23:00:00" in r["sleep_start"]
    assert "07:30:00" in r["sleep_end"]
    assert r["raw_payload"]["n_sessions"] == 3


def test_different_nights_stay_separate():
    """Sleep sessions con night_date distinta NO se combinan."""
    sessions = [
        {"date": "2026-05-15 08:00:00 -0300", "sleepStart": "2026-05-14 23:00:00 -0300",
         "sleepEnd": "2026-05-15 07:00:00 -0300", "totalSleep": 8.0},
        {"date": "2026-05-16 08:00:00 -0300", "sleepStart": "2026-05-15 23:00:00 -0300",
         "sleepEnd": "2026-05-16 07:00:00 -0300", "totalSleep": 8.0},
    ]
    rows = parse_health_auto_export(_payload(sessions))
    assert len(rows) == 2
    nights = sorted(r["night_date"] for r in rows)
    assert nights == ["2026-05-15", "2026-05-16"]
