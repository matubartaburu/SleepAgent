"""Tests del cardio_parser."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

from agents.workout.cardio_parser import parse


def _mock_sonnet(payload: dict):
    fake = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps(payload))]
    fake.messages.create.return_value = resp
    return patch("agents.workout.cardio_parser.Anthropic", return_value=fake)


def test_parse_running():
    with _mock_sonnet({
        "sport": "running",
        "duration_min": 45,
        "distance_km": 8.0,
        "intensity": "intensa",
        "rpe": 7,
        "notes": "",
        "date_hint": "today",
    }):
        r = parse("corrí 8km en 45 minutos, intenso, rpe 7")
    assert r.is_cardio
    assert r.sport == "running"
    assert r.distance_km == 8.0
    assert r.duration_min == 45
    assert r.intensity == "intensa"
    assert r.rpe == 7


def test_parse_futbol():
    with _mock_sonnet({
        "sport": "futbol",
        "duration_min": 60,
        "distance_km": None,
        "intensity": "moderada",
        "rpe": None,
        "notes": "",
        "date_hint": "today",
    }):
        r = parse("hice 1 hora de fútbol moderado")
    assert r.is_cardio
    assert r.sport == "futbol"
    assert r.distance_km is None


def test_parse_empty():
    r = parse("")
    assert not r.is_cardio


def test_parse_not_cardio():
    with _mock_sonnet({
        "sport": None, "duration_min": None, "distance_km": None,
        "intensity": None, "rpe": None, "notes": "no_cardio_detected",
        "date_hint": "today",
    }):
        r = parse("hice press banca 4x4")
    assert not r.is_cardio
