"""Tests del cliente Twilio con detección de errores de delivery."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _fake_message(sid="SMtest", status="queued", error_code=None, error_message=None):
    m = MagicMock()
    m.sid = sid
    m.status = status
    m.error_code = error_code
    m.error_message = error_message
    return m


def test_send_returns_sid_when_delivered():
    import twilio_client

    created = _fake_message(sid="SM1", status="queued")
    final = _fake_message(sid="SM1", status="delivered")

    fake_client = MagicMock()
    fake_client.messages.create.return_value = created
    fake_client.messages.return_value.fetch.return_value = final

    with patch.object(twilio_client, "_client", return_value=fake_client):
        sid = twilio_client.send_whatsapp_text("hola", to="+59891000000",
                                                confirm_timeout_s=1.0)
    assert sid == "SM1"


def test_send_raises_on_failed_with_sandbox_dead_code():
    import twilio_client

    created = _fake_message(sid="SM2", status="queued")
    final = _fake_message(sid="SM2", status="failed", error_code=63015,
                          error_message=None)

    fake_client = MagicMock()
    fake_client.messages.create.return_value = created
    fake_client.messages.return_value.fetch.return_value = final

    with patch.object(twilio_client, "_client", return_value=fake_client):
        with pytest.raises(twilio_client.TwilioDeliveryError) as exc_info:
            twilio_client.send_whatsapp_text("hola", to="+59891000000",
                                              confirm_timeout_s=1.0)

    err = exc_info.value
    assert err.sid == "SM2"
    assert err.error_code == 63015
    assert "sandbox" in str(err).lower()


def test_send_raises_on_undelivered():
    import twilio_client

    created = _fake_message(sid="SM3", status="queued")
    final = _fake_message(sid="SM3", status="undelivered", error_code=63016)

    fake_client = MagicMock()
    fake_client.messages.create.return_value = created
    fake_client.messages.return_value.fetch.return_value = final

    with patch.object(twilio_client, "_client", return_value=fake_client):
        with pytest.raises(twilio_client.TwilioDeliveryError):
            twilio_client.send_whatsapp_text("hola", to="+59891000000",
                                              confirm_timeout_s=1.0)


def test_send_skips_confirmation_when_disabled():
    import twilio_client

    created = _fake_message(sid="SM4", status="queued")

    fake_client = MagicMock()
    fake_client.messages.create.return_value = created

    with patch.object(twilio_client, "_client", return_value=fake_client):
        sid = twilio_client.send_whatsapp_text("hola", to="+59891000000",
                                                confirm_delivery=False)
    assert sid == "SM4"
    # confirm_delivery=False NO debe llamar a messages(sid).fetch()
    fake_client.messages.return_value.fetch.assert_not_called()


def test_preflight_detects_sandbox_dead():
    """Si el último mensaje a MY_PHONE falló con 63015, preflight rechaza Twilio."""
    from agents import preflight

    bad_message = _fake_message(sid="SMold", status="failed", error_code=63015)
    fake_client = MagicMock()
    fake_client.messages.list.return_value = [bad_message]

    with patch("twilio_client._client", return_value=fake_client):
        ok, issues = preflight._check_twilio()

    assert ok is False
    assert any("sandbox_dead" in i for i in issues)
    assert any("join" in i for i in issues)


def test_preflight_ok_when_most_recent_is_good_even_with_old_failure():
    """Una falla vieja seguida de un envío exitoso reciente NO debe falsa-alarmar."""
    from agents import preflight

    # messages.list devuelve ordenado desc (más reciente primero)
    recent_ok = _fake_message(sid="SMnew", status="delivered", error_code=None)
    fake_client = MagicMock()
    fake_client.messages.list.return_value = [recent_ok]  # solo el más reciente

    with patch("twilio_client._client", return_value=fake_client):
        ok, issues = preflight._check_twilio()

    assert ok is True
    assert issues == []


def test_get_recent_conversation_merges_in_and_out_chronologically():
    """Mezcla sent + received, ordena cronológicamente, mapea roles."""
    import twilio_client

    # outbound (assistant)
    sent_a = _fake_message(sid="A1", status="delivered")
    sent_a.body = "respuesta a"
    sent_a.date_sent = "2026-05-15T10:00:00Z"
    sent_a.date_created = sent_a.date_sent

    sent_b = _fake_message(sid="A2", status="sent")
    sent_b.body = "respuesta b"
    sent_b.date_sent = "2026-05-15T10:30:00Z"
    sent_b.date_created = sent_b.date_sent

    # inbound (user)
    recv_a = _fake_message(sid="U1", status="received")
    recv_a.body = "pregunta a"
    recv_a.date_sent = "2026-05-15T09:59:00Z"
    recv_a.date_created = recv_a.date_sent

    recv_b = _fake_message(sid="U2", status="received")
    recv_b.body = "pregunta b"
    recv_b.date_sent = "2026-05-15T10:29:00Z"
    recv_b.date_created = recv_b.date_sent

    fake_client = MagicMock()
    # Twilio devuelve por filtros distintos para to= y from_=
    def list_side_effect(**kwargs):
        if "to" in kwargs:
            return [sent_b, sent_a]
        if "from_" in kwargs:
            return [recv_b, recv_a]
        return []
    fake_client.messages.list.side_effect = list_side_effect

    with patch.object(twilio_client, "_client", return_value=fake_client):
        items = twilio_client.get_recent_conversation(limit=10)

    # Esperamos orden cronológico: recv_a, sent_a, recv_b, sent_b
    assert [i["role"] for i in items] == ["user", "assistant", "user", "assistant"]
    assert [i["content"] for i in items] == [
        "pregunta a", "respuesta a", "pregunta b", "respuesta b",
    ]


def test_get_recent_conversation_excludes_failed_outbound():
    """Mensajes salientes en failed/undelivered no deben aparecer (no llegaron)."""
    import twilio_client

    good = _fake_message(sid="A1", status="delivered")
    good.body = "ok"
    good.date_sent = "2026-05-15T10:00:00Z"
    good.date_created = good.date_sent

    failed = _fake_message(sid="A2", status="failed", error_code=63015)
    failed.body = "no llegó"
    failed.date_sent = "2026-05-15T11:00:00Z"
    failed.date_created = failed.date_sent

    fake_client = MagicMock()
    def list_side_effect(**kwargs):
        if "to" in kwargs:
            return [failed, good]
        return []
    fake_client.messages.list.side_effect = list_side_effect

    with patch.object(twilio_client, "_client", return_value=fake_client):
        items = twilio_client.get_recent_conversation(limit=10)

    assert len(items) == 1
    assert items[0]["sid"] == "A1"
