"""Tests del workout_parser. Llamadas a Sonnet mockeadas."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

from agents.workout.workout_parser import (
    _resolve_date,
    parse,
)


def _mock_sonnet(payload: dict):
    fake = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps(payload))]
    fake.messages.create.return_value = resp
    return patch("agents.workout.workout_parser.Anthropic", return_value=fake)


def test_parse_three_exercises_single_audio():
    with _mock_sonnet({
        "exercises": [
            {"exercise": "press banca", "sets": 4, "reps": 4, "weight_kg": 80, "rir": None, "notes": ""},
            {"exercise": "apertura", "sets": 3, "reps": 10, "weight_kg": 20, "rir": None, "notes": ""},
            {"exercise": "press inclinado", "sets": 3, "reps": 8, "weight_kg": 60, "rir": 2, "notes": "último set me costó"},
        ],
        "date_hint": "today",
        "session_notes": "",
    }):
        result = parse("hice press banca 4x4 con 80, apertura 3x10 con 20, press inclinado 3x8 con 60 rir 2")

    assert result.has_exercises
    assert len(result.exercises) == 3
    assert result.exercises[0].exercise == "press banca"
    assert result.exercises[0].weight_kg == 80
    assert result.exercises[2].rir == 2
    assert result.resolved_date == date.today()


def test_parse_rpe_legacy_gets_converted_to_rir():
    """Si Sonnet (legacy) devuelve RPE en vez de RIR, lo convertimos: RIR = 10 - RPE."""
    with _mock_sonnet({
        "exercises": [
            {"exercise": "press banca", "sets": 4, "reps": 4, "weight_kg": 80, "rpe": 8, "notes": ""},
        ],
        "date_hint": "today",
        "session_notes": "",
    }):
        result = parse("press banca 4x4 80 rpe 8")
    assert result.exercises[0].rir == 2  # 10 - 8


def test_parse_retrospective_yesterday():
    with _mock_sonnet({
        "exercises": [{"exercise": "sentadilla", "sets": 4, "reps": 6, "weight_kg": 100, "rpe": None, "notes": ""}],
        "date_hint": "yesterday",
        "session_notes": "",
    }):
        result = parse("ayer hice sentadilla 4 de 6 con 100")
    from datetime import timedelta
    assert result.resolved_date == date.today() - timedelta(days=1)


def test_parse_empty_returns_no_exercises():
    result = parse("")
    assert not result.has_exercises


def test_parse_no_workout_detected():
    with _mock_sonnet({
        "exercises": [],
        "date_hint": "today",
        "session_notes": "no_workout_detected",
    }):
        result = parse("hola como va")
    assert not result.has_exercises


def test_resolve_date_relative():
    today = date(2026, 5, 16)  # un sábado
    from datetime import timedelta
    assert _resolve_date("today", today) == today
    assert _resolve_date("yesterday", today) == today - timedelta(days=1)
    assert _resolve_date("day_before_yesterday", today) == today - timedelta(days=2)
    assert _resolve_date("N_days_ago:5", today) == today - timedelta(days=5)


def test_resolve_date_weekday():
    # Sábado 16-may → "lunes" → lunes anterior = 11-may
    today = date(2026, 5, 16)
    from datetime import timedelta
    assert _resolve_date("weekday:lunes", today) == today - timedelta(days=5)
