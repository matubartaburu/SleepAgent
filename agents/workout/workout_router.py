"""
agents/workout/workout_router.py — router de intents para mensajes
entrantes de Mateo.

Usa Haiku 4.5 (barato) para clasificar el mensaje en uno de los intents
soportados. Devuelve también un argumento opcional (ej: el muscle_group
para retrieve queries).

Intents:
- setup_plan        "el plan es: día 1 pecho y hombro, día 2 espalda..."
- edit_plan         "agregale gemelos al día 3"
- log_workout       "hice press banca 4x4 con 80..."
- log_cardio        "corrí 8k en 45 min"
- retrieve_workout  "qué hice de pecho la última vez?" / "última sentadilla"
- retrieve_running  "cuánto corrí la última vez?" / "como vengo en running"
- next_day          "qué me toca hoy?" / "qué entreno hoy"
- correction        "no, el press fue 85 no 80"
- cross_domain      "como dormí ayer y como entrené?"
- sleep_question    "a qué hora me dormí el lunes?" (delega a Oscar answerer)
- other             nada de lo de arriba
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY

log = logging.getLogger(__name__)

_HAIKU = "claude-haiku-4-5-20251001"


VALID_INTENTS = {
    "setup_plan", "edit_plan",
    "log_workout", "log_cardio",
    "retrieve_workout", "retrieve_running",
    "day_brief",     # "hoy entreno pecho y hombros" → trae info del día
    "next_day",
    "correction",
    "cross_domain",
    "sleep_question",
    "other",
}


SYSTEM_PROMPT = """\
Sos un clasificador de intenciones para mensajes que Mateo (uruguayo) le
manda a su agente personal por WhatsApp. Recibís el texto (puede venir de
audio transcrito o ser escrito directo) y devolvés JSON con el intent y
argumentos opcionales.

Intents posibles:
- "setup_plan": Mateo describe un PLAN/SPLIT completo de varios días.
- "edit_plan": Modifica UN día del plan existente (agregar/quitar músculo o ejercicio).
- "log_workout": Cuenta lo que hizo en el gimnasio (musculación con peso/reps).
- "log_cardio": Cuenta una sesión de cardio (correr, futbol, yoga, etc.).
- "retrieve_workout": Pregunta por su último entrenamiento de algún músculo o ejercicio.
- "retrieve_running": Pregunta específicamente por corridas (distancia, pace).
- "day_brief": Mateo dice "hoy entreno pecho y hombros" (o similar) — quiere que
  Oscar le traiga los ejercicios registrados para ese día del plan. El campo
  `muscles` lleva los músculos mencionados.
- "next_day": Pregunta GENÉRICA "qué me toca hoy?" sin mencionar músculos.
- "correction": Corrige un dato del log anterior (peso/reps/etc.).
- "cross_domain": Pregunta que cruza sueño + entrenamiento.
- "sleep_question": Pregunta sobre sueño/HRV/recovery únicamente.
- "other": ninguna de las anteriores (saludo, charla, etc.).

OUTPUT — JSON estricto:
{
  "intent": "log_workout",
  "muscle_group": null,        // si retrieve_workout: pecho/espalda/etc.
  "muscles": [],               // si day_brief: lista de músculos mencionados
  "exercise": null,            // si retrieve_workout: nombre del ejercicio
  "confidence": 0.95
}

REGLAS:
- Si vos dudás entre 2 intents, elegí el más específico.
- log_workout vs log_cardio: si menciona series/reps/peso → musculación. Si menciona
  distancia/duración/deporte → cardio.
- retrieve_workout sin músculo específico pero pregunta por "última vez de gym" →
  muscle_group=null y dejá que el retriever maneje.
- correction solo si el mensaje empieza con "no", "ah no", "perdón fue", "espera el ...".
"""


@dataclass
class RouterResult:
    intent: str
    muscle_group: str | None = None
    exercise: str | None = None
    confidence: float = 0.0
    raw_response: str = ""
    muscles: list[str] | None = None   # para day_brief: lista de músculos


def route(text: str) -> RouterResult:
    """Clasifica un texto en un intent. Nunca lanza; devuelve 'other' si falla."""
    if not text or not text.strip():
        return RouterResult(intent="other", confidence=0.0)

    if not ANTHROPIC_API_KEY:
        log.warning("workout_router: no hay ANTHROPIC_API_KEY, devuelvo 'other'")
        return RouterResult(intent="other", confidence=0.0)

    raw = _call_haiku(text)
    data = _extract_json(raw)

    intent = str(data.get("intent") or "other").lower()
    if intent not in VALID_INTENTS:
        log.warning("workout_router: intent inválido %r, fallback a 'other'", intent)
        intent = "other"

    return RouterResult(
        intent=intent,
        muscle_group=(data.get("muscle_group") or None),
        exercise=(data.get("exercise") or None),
        confidence=float(data.get("confidence") or 0.0),
        raw_response=raw,
        muscles=list(data.get("muscles") or []),
    )


# ── Internals ──────────────────────────────────────────────────────────────

def _call_haiku(text: str) -> str:
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=_HAIKU, max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        return resp.content[0].text.strip() if resp.content else ""
    except Exception as exc:
        log.warning("workout_router Haiku call falló: %s", exc)
        return ""


def _extract_json(text: str) -> dict:
    try: return json.loads(text)
    except json.JSONDecodeError: pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except json.JSONDecodeError: pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try: return json.loads(m.group(0))
        except json.JSONDecodeError: pass
    return {}
