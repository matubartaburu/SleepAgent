"""Tests del answerer conversacional."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _row(date_str, total_min, start="2026-05-14T23:30:00-03:00",
         end="2026-05-15T07:00:00-03:00"):
    return {
        "night_date": date_str,
        "total_sleep_minutes": total_min,
        "rem_minutes": 80, "core_minutes": 240, "deep_minutes": 70,
        "awake_minutes": 20, "in_bed_minutes": total_min + 25,
        "hrv_sdnn_ms": 58.0, "resting_hr_bpm": 53.0,
        "avg_hr_bpm": 60, "min_hr_bpm": 48, "max_hr_bpm": 90,
        "respiratory_rate_brpm": 14.2,
        "sleep_start": start, "sleep_end": end,
    }


def _patch_anthropic(reply_text):
    fake = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=reply_text)]
    resp.usage = MagicMock(input_tokens=300, output_tokens=80)
    fake.messages.create.return_value = resp
    # answerer hace `from anthropic import Anthropic` al tope, entonces
    # patcheamos donde está bindeado, no en el módulo origen.
    return patch("agents.answerer.Anthropic", return_value=fake)


def test_compact_row_includes_key_fields():
    from agents.answerer import _compact_row
    line = _compact_row(_row("2026-05-14", 420))
    assert "2026-05-14" in line
    assert "total=" in line
    assert "hrv=" in line


def test_build_user_message_includes_question_and_rows():
    from agents.answerer import _build_user_message
    rows = [_row("2026-05-14", 420), _row("2026-05-13", 480)]
    msg = _build_user_message("a qué hora me dormí el 14?", rows)
    assert "a qué hora me dormí el 14?" in msg
    assert "2026-05-14" in msg
    assert "2026-05-13" in msg
    assert "Hoy es" in msg


def test_build_user_message_handles_empty():
    from agents.answerer import _build_user_message
    msg = _build_user_message("cualquier cosa", [])
    assert "sin filas" in msg


def test_answer_question_returns_text():
    from agents import answerer
    with patch("db.get_last_n_nights", return_value=[_row("2026-05-14", 420)]), \
         patch("twilio_client.get_recent_conversation", return_value=[]), \
         _patch_anthropic("Te dormiste a las 23:30 y te despertaste a las 07:00, dale."):
        result = answerer.answer_question("a qué hora dormí el 14 de mayo?")
    assert "23:30" in result.text or "dormiste" in result.text.lower()
    assert result.n_nights_used == 1
    assert result.chars > 0


def test_answer_question_with_no_data():
    from agents import answerer
    with patch("db.get_last_n_nights", return_value=[]), \
         patch("twilio_client.get_recent_conversation", return_value=[]), \
         _patch_anthropic("Ojo, todavía no tengo data en el sistema."):
        result = answerer.answer_question("cómo viene mi HRV?")
    assert result.n_nights_used == 0
    assert result.text != ""


def test_answer_question_passes_history_to_messages():
    """La history se traduce a multi-turn messages con roles correctos."""
    from agents import answerer
    history = [
        {"role": "user", "content": "a qué hora dormí el 3 de marzo?", "ts": "1", "sid": "S1"},
        {"role": "assistant", "content": "A las 02:01 te dormiste.", "ts": "2", "sid": "S2"},
    ]
    captured = {}
    def fake_create(**kwargs):
        captured.update(kwargs)
        resp = MagicMock()
        resp.content = [MagicMock(text="Te despertaste a las 08:30.")]
        resp.usage = MagicMock(input_tokens=300, output_tokens=20)
        return resp

    fake = MagicMock()
    fake.messages.create.side_effect = fake_create

    with patch("db.get_last_n_nights", return_value=[_row("2026-03-03", 420)]), \
         patch("twilio_client.get_recent_conversation", return_value=history), \
         patch("agents.answerer.Anthropic", return_value=fake):
        result = answerer.answer_question("y a qué hora me desperté?")

    assert result.n_history_turns == 2
    # El array messages debe tener al menos 3 entries (history + nueva pregunta)
    msgs = captured["messages"]
    assert len(msgs) >= 2
    assert msgs[0]["role"] == "user"
    assert "3 de marzo" in msgs[0]["content"]
    assert msgs[1]["role"] == "assistant"
    # El último user message debe traer la pregunta nueva + data
    assert msgs[-1]["role"] == "user"
    assert "y a qué hora me desperté?" in msgs[-1]["content"]


def test_answer_question_skip_history():
    """skip_history=True no llama a Twilio."""
    from agents import answerer
    with patch("db.get_last_n_nights", return_value=[_row("2026-05-14", 420)]), \
         patch("twilio_client.get_recent_conversation") as mock_hist, \
         _patch_anthropic("dale"):
        result = answerer.answer_question("test", skip_history=True)
    mock_hist.assert_not_called()
    assert result.n_history_turns == 0


def test_build_messages_dedupes_current_question_from_history():
    """Si la history ya trae la pregunta actual (race con webhook), no la dupliques."""
    from agents.answerer import _build_messages
    history = [
        {"role": "user", "content": "hola", "ts": "1", "sid": "S1"},
        {"role": "assistant", "content": "qué tal", "ts": "2", "sid": "S2"},
        {"role": "user", "content": "qué hora me dormí?", "ts": "3", "sid": "S3"},
    ]
    msgs = _build_messages("qué hora me dormí?", [_row("2026-05-14", 420)], history)
    # No debe haber dos "qué hora me dormí?" en el array
    occurrences = sum(1 for m in msgs if "qué hora me dormí?" in m["content"])
    # Una vez en el último user message (con data), no dos veces
    assert occurrences == 1


def test_build_messages_collapses_consecutive_same_role():
    """Anthropic requiere alternancia; si hay dos user seguidos los colapsamos."""
    from agents.answerer import _build_messages
    history = [
        {"role": "user", "content": "ping", "ts": "1", "sid": "S1"},
        {"role": "user", "content": "ping 2", "ts": "2", "sid": "S2"},
    ]
    msgs = _build_messages("nueva pregunta", [], history)
    # Buscamos secuencias de user consecutivas — no debe haber
    for i in range(len(msgs) - 1):
        assert not (msgs[i]["role"] == "user" and msgs[i + 1]["role"] == "user")
