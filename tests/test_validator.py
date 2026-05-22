"""Tests del agente validator (LLM-as-judge). No llama a Claude (mockeado)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from agents.validator import _extract_json, validate_report


def test_extract_json_plain():
    assert _extract_json('{"approved": true, "score": 9}') == {"approved": True, "score": 9}


def test_extract_json_with_code_fence():
    raw = 'aquí va el veredicto:\n```json\n{"approved": false, "score": 4}\n```\nfin.'
    assert _extract_json(raw) == {"approved": False, "score": 4}


def test_extract_json_inline_object():
    raw = 'comentario {"approved": true, "score": 8} más texto'
    assert _extract_json(raw) == {"approved": True, "score": 8}


def test_validate_report_dry_run_returns_approved():
    v = validate_report("texto", raw_data={}, dry_run=True)
    assert v.approved is True
    assert v.score == 10


def test_validate_report_approved_path():
    fake_resp = MagicMock(
        text=json.dumps({
            "approved": True, "score": 9,
            "issues": [], "fact_check": [],
            "suggested_fix_hint": "",
        }),
        input_tokens=100, output_tokens=50, cost_usd=0.01,
        model="claude-opus-4-7", dry_run=False,
    )
    with patch("agents.validator.call_claude", return_value=fake_resp):
        v = validate_report("Dale Mateo, dormiste 7h ayer, bárbaro.",
                            raw_data={"total_sleep_minutes": 420}, baseline={})
    assert v.approved is True
    assert v.score == 9
    assert v.issues == []


def test_validate_report_rejected_path():
    fake_resp = MagicMock(
        text=json.dumps({
            "approved": False, "score": 4,
            "issues": ["usa la palabra 'perfecto'", "tiene emojis"],
            "fact_check": [{"claim": "dormiste 10h", "supported": False, "evidence": "real eran 7h"}],
            "suggested_fix_hint": "sacá emojis y la palabra perfecto",
        }),
        input_tokens=200, output_tokens=120, cost_usd=0.05,
        model="claude-opus-4-7", dry_run=False,
    )
    with patch("agents.validator.call_claude", return_value=fake_resp):
        v = validate_report("Perfecto Mateo 🌙 dormiste 10h",
                            raw_data={"total_sleep_minutes": 420}, baseline={})
    assert v.approved is False
    assert v.score == 4
    assert len(v.issues) == 2
    assert v.suggested_fix_hint != ""


def test_validate_report_invalid_json_marks_unapproved():
    fake_resp = MagicMock(
        text="no es json válido para nada",
        input_tokens=50, output_tokens=20, cost_usd=0.005,
        model="claude-opus-4-7", dry_run=False,
    )
    with patch("agents.validator.call_claude", return_value=fake_resp):
        v = validate_report("texto", raw_data={}, baseline={})
    assert v.approved is False
    assert any("validator_invalid_json" in i for i in v.issues)
