"""Tests del muscle_classifier."""

from __future__ import annotations

from unittest.mock import patch

from agents.workout.muscle_classifier import (
    _normalize,
    classify,
    ClassificationResult,
)


def test_normalize_lowercase_strip_accents():
    assert _normalize("Press Banca") == "press banca"
    assert _normalize("Jalón al Pecho") == "jalon al pecho"
    assert _normalize("  EXTENSIÓN  ") == "extension"


def test_builtin_match():
    r = classify("press banca")
    assert r.canonical_name == "press banca"
    assert "pecho" in r.muscle_groups
    assert r.source == "builtin"
    assert r.confidence == 1.0


def test_builtin_with_accents():
    r = classify("Jalón al Pecho")
    assert r.canonical_name == "jalón al pecho"
    assert "espalda" in r.muscle_groups
    assert r.source == "builtin"


def test_compound_muscle_groups():
    r = classify("peso muerto")
    assert r.source == "builtin"
    assert "espalda" in r.muscle_groups
    assert "piernas" in r.muscle_groups


def test_unknown_falls_to_llm_or_unknown():
    # Sin LLM mockeado, debe caer a unknown (config sin key o sin acceso a notion)
    with patch("agents.workout.muscle_classifier._lookup_alias", return_value=None), \
         patch("agents.workout.muscle_classifier._llm_classify",
               return_value=ClassificationResult(canonical_name="ejercicio raro",
                                                  source="unknown",
                                                  confidence=0.0)):
        r = classify("ejercicio que no existe")
    assert r.source == "unknown"


def test_llm_fallback_marks_new_alias():
    with patch("agents.workout.muscle_classifier._lookup_alias", return_value=None), \
         patch("agents.workout.muscle_classifier._llm_classify",
               return_value=ClassificationResult(canonical_name="press banca",
                                                  muscle_groups=["pecho"],
                                                  source="llm",
                                                  confidence=0.85)):
        r = classify("bench press")
    assert r.is_new_alias is True
    assert r.muscle_groups == ["pecho"]
