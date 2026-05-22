"""Tests del endpoint POST /whatsapp/inbound.

Después del refactor a background processing, el endpoint responde 200
inmediato con {"status": "accepted", "processing": "background"} y todo
el trabajo real ocurre asíncrono en _process_inbound_async.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from main import app
    return TestClient(app)


def _form(**fields):
    return {**fields}


def test_inbound_rejects_invalid_signature(client):
    with patch("twilio.request_validator.RequestValidator") as mock_val:
        instance = MagicMock()
        instance.validate.return_value = False
        mock_val.return_value = instance
        r = client.post("/whatsapp/inbound",
                        data=_form(From="whatsapp:+59891000000",
                                   Body="comí pasta",
                                   MessageSid="SMx"),
                        headers={"X-Twilio-Signature": "bad"})
    assert r.status_code == 403


def test_inbound_ignores_unknown_sender(client):
    with patch("twilio.request_validator.RequestValidator") as mock_val:
        instance = MagicMock()
        instance.validate.return_value = True
        mock_val.return_value = instance
        r = client.post("/whatsapp/inbound",
                        data=_form(From="whatsapp:+5491100000000",
                                   Body="hola",
                                   MessageSid="SMx"),
                        headers={"X-Twilio-Signature": "ok"})
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


def test_inbound_known_sender_returns_accepted_immediately(client):
    """El webhook responde 200 'accepted' inmediato, el trabajo va a background."""
    with patch("twilio.request_validator.RequestValidator") as mock_val:
        instance = MagicMock()
        instance.validate.return_value = True
        mock_val.return_value = instance
        r = client.post("/whatsapp/inbound",
                        data=_form(From="whatsapp:+59891000000",
                                   Body="a qué hora dormí ayer?",
                                   MessageSid="SMx"),
                        headers={"X-Twilio-Signature": "ok"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["processing"] == "background"


def test_process_inbound_async_with_open_note_taggea():
    """_process_inbound_async cuando hay nota abierta llama al tagger."""
    from main import _process_inbound_async
    from agents.tagger import TaggerResult

    open_note = {"id": 42, "night_date": "2026-05-15", "question": "qué pasó?"}

    with patch("db.get_open_note", return_value=open_note), \
         patch("agents.tagger.tag_answer_haiku",
               return_value=TaggerResult(tags=["alcohol"], confidence=0.85)) as mock_tag, \
         patch("db.update_note_answer") as mock_update, \
         patch("twilio_client.send_whatsapp_text") as mock_send:
        asyncio.run(_process_inbound_async(
            body="comí vino y pasta",
            message_sid="SMx",
            media_url="",
            media_ct="",
        ))
    mock_tag.assert_called_once_with("comí vino y pasta")
    mock_update.assert_called_once()


def test_process_inbound_async_no_note_goes_to_answerer():
    """Sin nota abierta y sin workout intent → answerer."""
    from main import _process_inbound_async
    from agents.answerer import AnswererResult
    from agents.workout.orchestrator import OrchestratorResult

    with patch("db.get_open_note", return_value=None), \
         patch("agents.workout.orchestrator.handle_message",
               return_value=OrchestratorResult(
                   intent="sleep_question", reply_text="", handled_by_workout=False,
               )), \
         patch("agents.answerer.answer_question",
               return_value=AnswererResult(text="Dormiste a las 23:30",
                                           n_nights_used=5, chars=22)) as mock_ans, \
         patch("twilio_client.send_whatsapp_text",
               return_value="SManswered") as mock_send:
        asyncio.run(_process_inbound_async(
            body="a qué hora dormí ayer?",
            message_sid="SMx",
            media_url="",
            media_ct="",
        ))
    mock_ans.assert_called_once()
    mock_send.assert_called_once()
