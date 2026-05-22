"""
agents/workout/muscle_classifier.py — clasifica un nombre de ejercicio
en su muscle group.

Estrategia en 3 niveles:
1. Diccionario built-in: cubre los 40-50 ejercicios más comunes en español/inglés.
2. Notion Aliases DB: cuando aprendiste un alias nuevo, queda persistido.
3. LLM fallback (Haiku): si no matchea en (1) ni (2), Haiku infiere y se
   ofrece a guardarlo como alias nuevo en Notion.

API pública:
- classify(exercise) -> ClassificationResult
- learn_alias(alias, canonical, muscle_group): persiste un alias nuevo
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

import notion_store as nc

log = logging.getLogger(__name__)


# ── Diccionario built-in: ejercicio canónico → muscle_groups ────────────────
# Solo lo más común. El alias dinámico cubre el resto.

BUILTIN_EXERCISES: dict[str, list[str]] = {
    # Pecho
    "press banca":            ["pecho"],
    "press inclinado":        ["pecho"],
    "press declinado":        ["pecho"],
    "press mancuernas":       ["pecho"],
    "apertura":               ["pecho"],
    "apertura mancuernas":    ["pecho"],
    "fondos":                 ["pecho", "brazos"],
    "pec deck":               ["pecho"],
    "cruce de poleas":        ["pecho"],

    # Espalda
    "jalón al pecho":         ["espalda"],
    "jalón frontal":          ["espalda"],
    "jalón nuca":             ["espalda"],
    "remo barra":             ["espalda"],
    "remo con barra":         ["espalda"],
    "remo mancuerna":         ["espalda"],
    "remo polea":             ["espalda"],
    "remo bajo":              ["espalda"],
    "dominadas":              ["espalda"],
    "pull over":              ["espalda", "pecho"],
    "peso muerto":            ["espalda", "piernas"],

    # Hombros
    "press militar":          ["hombros"],
    "press hombros":          ["hombros"],
    "press arnold":           ["hombros"],
    "elevación lateral":      ["hombros"],
    "lateral raise":          ["hombros"],
    "elevación frontal":      ["hombros"],
    "pájaros":                ["hombros"],
    "face pull":              ["hombros"],
    "face pulls":             ["hombros"],
    "encogimientos":          ["hombros", "espalda"],

    # Brazos
    "curl bíceps":            ["brazos"],
    "curl con barra":         ["brazos"],
    "curl martillo":          ["brazos"],
    "curl predicador":        ["brazos"],
    "curl alterno":           ["brazos"],
    "extensión tríceps":      ["brazos"],
    "tríceps polea":          ["brazos"],
    "tríceps con mancuerna":  ["brazos"],
    "press francés":          ["brazos"],

    # Piernas
    "sentadilla":             ["piernas"],
    "sentadilla búlgara":     ["piernas", "glúteos"],
    "sentadilla hack":        ["piernas"],
    "prensa":                 ["piernas"],
    "prensa de piernas":      ["piernas"],
    "extensión de cuádriceps":["piernas"],
    "extensiones de pierna":  ["piernas"],
    "curl femoral":           ["piernas"],
    "peso muerto rumano":     ["piernas", "glúteos"],
    "gemelos":                ["piernas"],
    "elevación de gemelos":   ["piernas"],

    # Glúteos
    "hip thrust":             ["glúteos"],
    "puente de glúteos":      ["glúteos"],
    "patada glúteo":          ["glúteos"],

    # Core
    "abdominales":            ["core"],
    "crunch":                 ["core"],
    "plancha":                ["core"],
    "rueda abdominal":        ["core"],
    "elevación piernas":      ["core"],
    "russian twist":          ["core"],
}


# ── Normalización ──────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase + strip + sin tildes. Para comparar nombres tolerantemente."""
    if not text:
        return ""
    text = text.strip().lower()
    text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    text = re.sub(r"\s+", " ", text)
    return text


# Versión normalizada del diccionario built-in (precomputada).
_NORMALIZED_BUILTIN = {_normalize(k): (k, v) for k, v in BUILTIN_EXERCISES.items()}


# ── Resultado ──────────────────────────────────────────────────────────────

@dataclass
class ClassificationResult:
    canonical_name: str
    muscle_groups: list[str] = field(default_factory=list)
    source: str = ""              # "builtin" | "alias" | "llm" | "unknown"
    confidence: float = 1.0       # 0-1
    is_new_alias: bool = False    # True si fue match por LLM y conviene aprenderlo


# ── Public API ─────────────────────────────────────────────────────────────

def classify(exercise: str) -> ClassificationResult:
    """
    Clasifica un nombre de ejercicio en su muscle_group.

    Orden:
    1) Built-in dict
    2) Notion Aliases DB
    3) Haiku fallback (LLM)
    4) Unknown (sin match)
    """
    name = _normalize(exercise)
    if not name:
        return ClassificationResult(canonical_name="", source="unknown", confidence=0.0)

    # 1) Built-in
    if name in _NORMALIZED_BUILTIN:
        canonical, muscles = _NORMALIZED_BUILTIN[name]
        return ClassificationResult(
            canonical_name=canonical, muscle_groups=list(muscles),
            source="builtin", confidence=1.0,
        )

    # 2) Notion Aliases DB
    alias_match = _lookup_alias(name)
    if alias_match:
        return alias_match

    # 3) LLM fallback
    llm_match = _llm_classify(exercise)
    if llm_match.confidence >= 0.6:
        llm_match.is_new_alias = True
        return llm_match

    # 4) Unknown
    return ClassificationResult(
        canonical_name=exercise, source="unknown", confidence=0.0,
    )


def learn_alias(alias: str, canonical: str, muscle_groups: list[str]) -> bool:
    """
    Persiste un alias nuevo en la DB de Notion. Devuelve True si se guardó.
    """
    db_id = nc.aliases_db_id()
    if not db_id:
        log.warning("No hay NOTION_DB_ALIASES_ID seteado, no puedo persistir el alias")
        return False
    row = nc.create_page_in_db(db_id, {
        "alias":        nc.prop_title(_normalize(alias)),
        "canonical":    nc.prop_text(canonical),
        "muscle_group": nc.prop_multi_select(muscle_groups),
    })
    if row:
        log.info("Alias aprendido: %r → %r (%s)", alias, canonical, muscle_groups)
        return True
    return False


# ── Internals ──────────────────────────────────────────────────────────────

def _lookup_alias(normalized_name: str) -> ClassificationResult | None:
    db_id = nc.aliases_db_id()
    if not db_id:
        return None
    rows = nc.query_db(db_id, filter_={
        "property": "alias", "title": {"equals": normalized_name},
    }, page_size=1)
    if not rows:
        return None
    p = nc.read_page_props(rows[0])
    canonical = (p.get("canonical") or "").strip()
    muscles   = p.get("muscle_group") or []
    if not canonical or not muscles:
        return None
    return ClassificationResult(
        canonical_name=canonical, muscle_groups=list(muscles),
        source="alias", confidence=0.95,
    )


# Modelo Haiku para clasificación barata.
_HAIKU_MODEL = "claude-haiku-4-5-20251001"


def _llm_classify(exercise: str) -> ClassificationResult:
    from anthropic import Anthropic
    from config import ANTHROPIC_API_KEY

    if not ANTHROPIC_API_KEY:
        return ClassificationResult(canonical_name=exercise, source="unknown", confidence=0.0)

    valid = ", ".join(sorted({m for muscles in BUILTIN_EXERCISES.values() for m in muscles}))

    system = (
        "Sos un clasificador de ejercicios de gimnasio. Recibís un nombre "
        "(en español o inglés) y devolvés:\n"
        "1) canonical_name: el nombre en español rioplatense, en minúsculas\n"
        "2) muscle_groups: lista de grupos musculares (de esta lista controlada: "
        f"{valid})\n"
        "3) confidence: 0-1\n\n"
        "Devolvé JSON estricto, sin texto extra:\n"
        '{"canonical_name": "...", "muscle_groups": ["..."], "confidence": 0.0}\n'
        "Si no sabés, devolvé confidence: 0."
    )
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=_HAIKU_MODEL, max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": exercise}],
        )
        text = resp.content[0].text.strip()
        data = _parse_json(text)
        muscles = [m for m in data.get("muscle_groups", []) if m]
        if not muscles:
            return ClassificationResult(canonical_name=exercise, source="unknown", confidence=0.0)
        return ClassificationResult(
            canonical_name=str(data.get("canonical_name", exercise)).strip().lower() or exercise,
            muscle_groups=muscles,
            source="llm",
            confidence=float(data.get("confidence", 0.7)),
        )
    except Exception as exc:
        log.warning("LLM classifier falló: %s", exc)
        return ClassificationResult(canonical_name=exercise, source="unknown", confidence=0.0)


def _parse_json(text: str) -> dict:
    import json
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    return {}
