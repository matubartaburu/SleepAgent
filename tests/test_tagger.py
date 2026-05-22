"""Tests del tagger (Haiku, mockeado)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from agents.tagger import CONTROLLED_TAGS, tag_answer_haiku


def _mock_anthropic_response(payload: dict):
    fake_resp = MagicMock()
    fake_resp.content = [MagicMock(text=json.dumps(payload))]
    fake_resp.usage = MagicMock(input_tokens=50, output_tokens=30)
    return fake_resp


def _patch_anthropic(payload):
    fake = MagicMock()
    fake.messages.create.return_value = _mock_anthropic_response(payload)
    # Anthropic se importa lazy dentro de la función → parchamos en el módulo origen
    return patch("anthropic.Anthropic", return_value=fake)


def test_tag_answer_extracts_known_tags():
    with _patch_anthropic({"tags": ["comida_tarde", "alcohol"],
                            "confidence": 0.9, "notes": "ok"}):
        r = tag_answer_haiku("comí pasta y tomé vino")
    assert "comida_tarde" in r.tags
    assert "alcohol" in r.tags
    assert r.confidence == 0.9


def test_tag_answer_filters_unknown_tags():
    with _patch_anthropic({"tags": ["comida_tarde", "tag_inventado"],
                            "confidence": 0.7, "notes": ""}):
        r = tag_answer_haiku("algo")
    assert r.tags == ["comida_tarde"]


def test_tag_answer_empty_input_returns_nada_without_api_call():
    # No debe llamar a la API si la respuesta es vacía
    with patch("anthropic.Anthropic") as mock_cls:
        r = tag_answer_haiku("")
    assert r.tags == ["nada"]
    mock_cls.assert_not_called()


def test_tag_answer_falls_back_to_otro_on_bad_json():
    fake = MagicMock()
    bad_resp = MagicMock()
    bad_resp.content = [MagicMock(text="no es json")]
    bad_resp.usage = MagicMock(input_tokens=10, output_tokens=5)
    fake.messages.create.return_value = bad_resp
    with patch("anthropic.Anthropic", return_value=fake):
        r = tag_answer_haiku("respuesta cualquiera")
    assert r.tags == ["otro"]


def test_controlled_tags_includes_common_causes():
    # Sanity check: lista esperada
    for must_have in ("comida_tarde", "alcohol", "deporte_tarde", "estres",
                       "viaje", "nada", "otro"):
        assert must_have in CONTROLLED_TAGS
