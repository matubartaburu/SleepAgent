"""Tests del workout_router."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from agents.workout.workout_router import VALID_INTENTS, route


def _mock_haiku(payload: dict):
    fake = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps(payload))]
    fake.messages.create.return_value = resp
    return patch("agents.workout.workout_router.Anthropic", return_value=fake)


def test_route_log_workout():
    with _mock_haiku({"intent": "log_workout", "confidence": 0.95}):
        r = route("hice press banca 4 de 4 con 80")
    assert r.intent == "log_workout"
    assert r.confidence == 0.95


def test_route_retrieve_with_muscle():
    with _mock_haiku({"intent": "retrieve_workout", "muscle_group": "espalda", "confidence": 0.9}):
        r = route("qué hice de espalda la última vez?")
    assert r.intent == "retrieve_workout"
    assert r.muscle_group == "espalda"


def test_route_sleep_question():
    with _mock_haiku({"intent": "sleep_question", "confidence": 0.85}):
        r = route("a qué hora me dormí el martes?")
    assert r.intent == "sleep_question"


def test_route_empty_returns_other():
    r = route("")
    assert r.intent == "other"


def test_route_invalid_intent_falls_to_other():
    with _mock_haiku({"intent": "blablabla_inventado", "confidence": 0.5}):
        r = route("test")
    assert r.intent == "other"


def test_route_all_valid_intents_listed():
    expected = {
        "setup_plan", "edit_plan",
        "log_workout", "log_cardio",
        "retrieve_workout", "retrieve_running",
        "day_brief", "next_day",
        "correction",
        "cross_domain", "sleep_question", "other",
    }
    assert VALID_INTENTS == expected
