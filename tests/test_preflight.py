"""Tests del preflight check."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch


def test_preflight_ok_when_data_today():
    from agents import preflight

    today = date.today().isoformat()
    with patch.object(preflight, "_check_data",
                      return_value=(True, today, [])), \
         patch.object(preflight, "_check_twilio",
                      return_value=(True, [])), \
         patch.object(preflight, "_check_anthropic",
                      return_value=(True, [])):
        res = preflight.preflight_check()

    assert res.data_ok is True
    assert res.twilio_ok is True
    assert res.anthropic_ok is True
    assert res.all_ok() is True
    assert res.issues == []
    assert res.last_night_date == today


def test_preflight_fails_when_data_missing():
    from agents import preflight

    with patch.object(preflight, "_check_data",
                      return_value=(False, "2026-05-14", ["última fila es de 2026-05-14, hoy es 2026-05-15"])), \
         patch.object(preflight, "_check_twilio",
                      return_value=(True, [])), \
         patch.object(preflight, "_check_anthropic",
                      return_value=(True, [])):
        res = preflight.preflight_check()

    assert res.data_ok is False
    assert res.all_ok() is False
    assert any("2026-05-14" in i for i in res.issues)


def test_preflight_fails_when_twilio_down():
    from agents import preflight

    with patch.object(preflight, "_check_data",
                      return_value=(True, date.today().isoformat(), [])), \
         patch.object(preflight, "_check_twilio",
                      return_value=(False, ["twilio_error: 401"])), \
         patch.object(preflight, "_check_anthropic",
                      return_value=(True, [])):
        res = preflight.preflight_check()

    assert res.all_ok() is False
    assert res.twilio_ok is False


def test_send_preflight_alert_skipped_when_all_ok():
    from agents import preflight
    res = preflight.PreflightResult(
        data_ok=True, twilio_ok=True, anthropic_ok=True,
        last_night_date=date.today().isoformat(),
    )
    assert preflight.send_preflight_alert(res) is None


def test_send_preflight_alert_sends_when_data_missing():
    from agents import preflight

    res = preflight.PreflightResult(
        data_ok=False, twilio_ok=True, anthropic_ok=True,
        last_night_date="2026-05-14",
        issues=["última fila es de 2026-05-14, hoy es 2026-05-15"],
    )
    with patch("twilio_client.send_whatsapp_text",
               return_value="SMfake") as mock_send:
        sid = preflight.send_preflight_alert(res)

    assert sid == "SMfake"
    mock_send.assert_called_once()
    body = mock_send.call_args.kwargs.get("body") or mock_send.call_args.args[0]
    assert "Oscar preflight" in body
    assert "2026-05-14" in body
