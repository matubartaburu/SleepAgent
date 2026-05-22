"""Tests del workout_orchestrator (handle_message)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agents.workout.orchestrator import handle_message
from agents.workout.workout_router import RouterResult


def _mock_route(intent: str, muscle_group: str | None = None, exercise: str | None = None):
    return patch("agents.workout.workout_router.route",
                  return_value=RouterResult(intent=intent, muscle_group=muscle_group,
                                             exercise=exercise, confidence=0.9))


def test_empty_message_returns_unhandled():
    result = handle_message("")
    assert result.intent == "other"
    assert not result.handled_by_workout


def test_sleep_question_delegates_to_answerer():
    with _mock_route("sleep_question"):
        result = handle_message("a qué hora dormí ayer?")
    assert result.intent == "sleep_question"
    assert not result.handled_by_workout
    assert not result.reply_text


def test_log_workout_happy_path():
    from agents.workout.workout_parser import WorkoutParseResult, ParsedExercise
    from datetime import date
    parsed = WorkoutParseResult(
        exercises=[ParsedExercise(exercise="press banca", sets=4, reps=4, weight_kg=80)],
        resolved_date=date.today(),
    )
    log_result = {
        "logged": 1, "skipped": 0, "session_id": "abc",
        "resolved_date": date.today().isoformat(),
        "exercises": [{
            "exercise": "press banca", "day_num": 1, "muscle_groups": ["pecho"],
            "sets": 4, "reps": 4, "weight_kg": 80, "rpe": None,
            "previous": {"weight_kg": 75, "sets": 4, "reps": 4, "rpe": None},
            "action": "updated",
        }],
        "skipped_exercises": [],
    }
    with _mock_route("log_workout"), \
         patch("agents.workout.workout_parser.parse", return_value=parsed), \
         patch("agents.workout.workout_logger.log_workout_session",
               return_value=log_result):
        result = handle_message("hice press banca 4x4 con 80")
    assert result.intent == "log_workout"
    assert result.handled_by_workout
    assert "press banca" in result.reply_text
    assert "Día 1" in result.reply_text
    assert "+5kg" in result.reply_text  # diff vs previous


def test_retrieve_workout_no_muscle_asks_clarification():
    with _mock_route("retrieve_workout"):
        result = handle_message("qué hice la última vez?")
    assert result.intent == "retrieve_workout"
    assert "músculo" in result.reply_text.lower() or "decime" in result.reply_text.lower()


def test_retrieve_running_no_data():
    with _mock_route("retrieve_running"), \
         patch("agents.workout.workout_retriever.last_running",
               return_value={"found": False, "reason": "no_data"}):
        result = handle_message("cuánto corrí la última vez?")
    assert result.intent == "retrieve_running"
    assert "no tengo" in result.reply_text.lower()


def test_setup_plan_creates_days():
    from agents.workout.plan_setup import PlanSetupResult, TrainingDay
    plan = PlanSetupResult(
        mode="setup",
        days=[
            TrainingDay(day_label="Día 1", muscle_groups=["pecho", "hombros"]),
            TrainingDay(day_label="Día 2", muscle_groups=["espalda", "brazos"]),
            TrainingDay(day_label="Día 3", muscle_groups=["piernas"]),
        ],
    )
    with _mock_route("setup_plan"), \
         patch("agents.workout.plan_setup.parse", return_value=plan), \
         patch("agents.workout.workout_logger.upsert_training_day", return_value=True):
        result = handle_message("plan: día 1 pecho y hombro, día 2 espalda y brazos, día 3 piernas")
    assert result.intent == "setup_plan"
    assert "Día 1" in result.reply_text
    assert "Plan guardado" in result.reply_text
