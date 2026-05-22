"""
agents/workout/workout_parser.py — parser de musculación.

Recibe texto transcrito (de Whisper) tipo:
  "hice press banca 4 series de 4 con 80, después apertura 3 de 10 con 20,
   y press inclinado 3x8 con 60, el último set me costó rpe 8"

Devuelve lista estructurada:
  [
    {exercise: "press banca", sets: 4, reps: 4, weight_kg: 80, rpe: null, notes: ""},
    {exercise: "apertura", sets: 3, reps: 10, weight_kg: 20, rpe: null, notes: ""},
    {exercise: "press inclinado", sets: 3, reps: 8, weight_kg: 60, rpe: 8, notes: "último set me costó"}
  ]

También extrae:
- date_hint: si vos dijiste "ayer" / "el lunes" / "hace 3 días" → fecha resuelta
- session_notes: comentarios generales sobre la sesión
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY

log = logging.getLogger(__name__)

_SONNET = "claude-sonnet-4-6"


SYSTEM_PROMPT = """\
Sos un parser BEST-EFFORT de entrenamientos de gimnasio en español
rioplatense. Tu objetivo: extraer LO MÁS QUE PUEDAS, aunque el texto sea
informal, conversacional, con muletillas, sin formato estructurado.

FILOSOFÍA: Mateo te habla natural. NO esperes que use "4 series de 4 con 80
kilos". Más probable que diga "hice press banca 80 cuatro veces" o "presa
de pierna con 100 kilos hice como tres de diez". Vos sacás los datos.

REGLAS DE EXTRACCIÓN:

Ejercicios — reconocé cualquier nombre conocido aunque venga embebido en
una frase larga. Ejemplos:
- "hice press banca" → exercise="press banca"
- "después estuve en la apertura" → exercise="apertura"
- "le di a la sentadilla" → exercise="sentadilla"
- "polea baja, ¿te acordás?" → exercise="polea baja"

Series, reps y peso — extraé desde cualquier forma:
- "4 series de 4 con 80" → sets=4, reps=4, weight_kg=80
- "4x4 con 80" → sets=4, reps=4, weight_kg=80
- "cuatro veces cuatro con ochenta" → sets=4, reps=4, weight_kg=80
- "press banca 80" (solo peso) → sets=null, reps=null, weight_kg=80
- "hice 3 de 10 con 20" → sets=3, reps=10, weight_kg=20
- "cuarenta kilos diez veces" → sets=1, reps=10, weight_kg=40
- Si no podés determinar algún campo, poné null. NO INVENTES.

Múltiples ejercicios — un solo audio puede mencionar varios, separados
por "después", "y", "luego", o simplemente pausas. Cada uno es un objeto.

Si el mismo ejercicio aparece 2 veces (corrección), usá el ÚLTIMO valor.

RIR (Reps In Reserve) — opcional. Es cuántas reps le quedaban en el tanque
al fallo. Escala 0-10:
- "rir 2" / "rir dos" / "me quedaban 2 reps" → rir=2
- "rir 0" / "al fallo" / "no podía más" → rir=0
- "rir 3" / "me quedaban 3" → rir=3

Si Mateo usa RPE en su lugar (escala vieja), convertí a RIR (RIR = 10 - RPE):
- "rpe 8" → rir=2  (porque 10 - 8 = 2)
- "rpe 9" → rir=1
- "rpe 10" → rir=0
- "rpe 7" → rir=3

Por descripciones verbales (sin número):
- "me costó" / "duro" → rir≈1-2
- "fácil" / "tranqui" → rir≈3-4
- "al límite" / "no podía más" → rir=0

Si no hay info de esfuerzo → null. NO INVENTES.

Fecha — detectá:
- "ayer", "anteayer", "hace N días", "el lunes/martes/..."
- date_hint = "yesterday" | "day_before_yesterday" | "N_days_ago:N" | "weekday:lunes"
- default: "today"

Notas — TODO comentario o información subjetiva que NO sea sets/reps/peso/rir
va a `notes` del ejercicio relevante. Capturá tanto notas implícitas como
explícitas:

  Implícitas (detectálas solas):
  - "el último set me costó"           → notes="último set duro"
  - "cambié agarre"                    → notes="cambié agarre"
  - "rodilla me molestó"               → notes="rodilla molestó"

  Explícitas (Mateo te las marca como nota):
  - "anotale que..."                   → notes="..."
  - "agregale comentario..."           → notes="..."
  - "y la nota es ..."                 → notes="..."
  - "ponele que ..." (cuando NO refiere a un campo concreto)  → notes="..."
  - "comentario: el press fue ..."     → notes="..."

  Si el comentario es sobre la sesión EN GENERAL (no un ejercicio específico),
  va a session_notes.

  Si Mateo dice "anotá esto bajo press banca: la barra estaba pesada",
  la nota va al ejercicio press banca.

  IMPORTANTE: una nota PRESERVA los demás datos. Si el ejercicio ya existía,
  esto es un partial update solo del campo notes.

OUTPUT — JSON estricto, sin texto extra:
{
  "exercises": [
    {"exercise": "press banca", "sets": 4, "reps": 4, "weight_kg": 80, "rir": null, "notes": ""}
  ],
  "date_hint": "today",
  "session_notes": ""
}

IMPORTANTE: si tenés DUDAS, intentá EXTRAER ALGO igual. Es preferible
extraer un ejercicio con datos parciales (nulls en algunos campos) que
devolver lista vacía.

Solo devolvé "exercises: []" si REALMENTE no hay mención de ningún
ejercicio de gym (charla random, saludos, etc.).
"""


@dataclass
class ParsedExercise:
    exercise: str
    sets: int | None = None
    reps: int | None = None
    weight_kg: float | None = None
    # RIR = Reps In Reserve (0-10). Cuántas reps te quedaban al fallo.
    # RIR + RPE = 10. Si user dice RPE, convertir a RIR antes.
    rir: int | None = None
    notes: str = ""


@dataclass
class WorkoutParseResult:
    exercises: list[ParsedExercise] = field(default_factory=list)
    resolved_date: date = field(default_factory=date.today)
    date_hint: str = "today"
    session_notes: str = ""
    raw_response: str = ""

    @property
    def has_exercises(self) -> bool:
        return len(self.exercises) > 0


# ── Public API ─────────────────────────────────────────────────────────────

def parse(text: str, *, today: date | None = None) -> WorkoutParseResult:
    """
    Parsea un texto de musculación. `today` es inyectable para tests.
    """
    if not text or not text.strip():
        return WorkoutParseResult(date_hint="today")

    today = today or date.today()
    raw = _call_sonnet(text)
    data = _extract_json(raw)

    exercises_data = data.get("exercises") or []
    exercises = [
        ParsedExercise(
            exercise=str(e.get("exercise", "")).strip().lower(),
            sets=_to_int(e.get("sets")),
            reps=_to_int(e.get("reps")),
            weight_kg=_to_float(e.get("weight_kg")),
            # Soportamos ambos: si Sonnet devuelve rir, lo usamos; si todavía
            # devuelve rpe (legacy), lo convertimos a rir.
            rir=_to_int(e.get("rir")) if e.get("rir") is not None
                else (10 - _to_int(e.get("rpe"))) if e.get("rpe") is not None
                else None,
            notes=str(e.get("notes", "")).strip(),
        )
        for e in exercises_data
        if e.get("exercise")
    ]

    date_hint = str(data.get("date_hint") or "today")
    resolved = _resolve_date(date_hint, today)

    return WorkoutParseResult(
        exercises=exercises,
        resolved_date=resolved,
        date_hint=date_hint,
        session_notes=str(data.get("session_notes") or "").strip(),
        raw_response=raw,
    )


# ── Internals ──────────────────────────────────────────────────────────────

def _call_sonnet(text: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY no seteado")
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=_SONNET, max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return resp.content[0].text.strip() if resp.content else ""


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except json.JSONDecodeError: pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try: return json.loads(m.group(0))
        except json.JSONDecodeError: pass
    log.warning("workout_parser: no pude extraer JSON de %r", text[:200])
    return {}


def _to_int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_WEEKDAYS = {
    "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
    "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6,
}


def _resolve_date(hint: str, today: date) -> date:
    """Convierte hint relativo a fecha absoluta."""
    if not hint or hint == "today":
        return today
    if hint == "yesterday":
        return today - timedelta(days=1)
    if hint == "day_before_yesterday":
        return today - timedelta(days=2)
    m = re.match(r"N_days_ago:(\d+)", hint)
    if m:
        return today - timedelta(days=int(m.group(1)))
    m = re.match(r"weekday:(\w+)", hint)
    if m:
        target = _WEEKDAYS.get(m.group(1).lower())
        if target is not None:
            # Más reciente día de la semana = target
            delta = (today.weekday() - target) % 7
            if delta == 0:
                delta = 7  # "lunes" cuando hoy es lunes → lunes pasado
            return today - timedelta(days=delta)
    return today
