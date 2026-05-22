"""
agents/workout/plan_setup.py — agente que parsea el plan/split de
entrenamiento desde un audio o texto.

Input típico:
  "el plan es: primer día pecho y hombro, segundo día espalda y brazos,
   tercer día pierna"

Output: lista de TrainingDay para escribir/actualizar en la DB
"Oscar — Training Plan" en Notion.

También maneja edits incrementales:
  "agregale gemelos al día 3"
  "el día 2 ahora también suma core"
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY

log = logging.getLogger(__name__)

_SONNET = "claude-sonnet-4-6"


SYSTEM_PROMPT = """\
Sos un parser de planes de entrenamiento (split) en español rioplatense.

Entrada: texto que describe los días de la semana de entrenamiento y qué
músculos toca cada día. También puede ser un EDIT a un plan existente
(ej: "agregale gemelos al día 3").

Grupos musculares válidos: pecho, espalda, hombros, brazos, piernas,
glúteos, core, cuello.

Modos:
- "setup": vos dictás un plan completo de varios días.
- "edit_day": modificás UN día existente (agregar/quitar músculos o ejercicios).
- "edit_global": cambio general (ej: "cambialo a 4 días"), implica setup nuevo.

OUTPUT — JSON estricto:

Para setup:
{
  "mode": "setup",
  "days": [
    {
      "day_label": "Día 1",
      "muscle_groups": ["pecho", "hombros"],
      "suggested_exercises": "press banca, press militar, lateral raise",
      "cardio": false,
      "notes": ""
    },
    ...
  ]
}

Para edit_day:
{
  "mode": "edit_day",
  "day_label": "Día 3",
  "operation": "add_muscle",  // add_muscle | remove_muscle | add_exercise | set_cardio
  "value": "gemelos"
}

Si no podés interpretar:
{"mode": "unknown", "raw_text": "..."}

REGLAS:
- Numerá día_label como "Día 1", "Día 2", etc. (con tilde en "Día").
- Si vos decís "pecho y hombro" → muscle_groups = ["pecho", "hombros"].
- suggested_exercises queda vacío si no lo mencionás.
- cardio=true si decís "más cardio" o "cardio al final" en algún día.
"""


@dataclass
class TrainingDay:
    day_label: str
    muscle_groups: list[str]
    suggested_exercises: str = ""
    cardio: bool = False
    notes: str = ""


@dataclass
class PlanSetupResult:
    mode: str = "unknown"   # setup | edit_day | edit_global | unknown
    days: list[TrainingDay] = field(default_factory=list)
    # Para edits
    day_label: str = ""
    operation: str = ""
    value: str = ""
    raw_response: str = ""


def parse(text: str) -> PlanSetupResult:
    if not text or not text.strip():
        return PlanSetupResult(mode="unknown")
    raw = _call_sonnet(text)
    data = _extract_json(raw)
    mode = str(data.get("mode") or "unknown").lower()

    if mode == "setup":
        days_raw = data.get("days") or []
        days = [
            TrainingDay(
                day_label=str(d.get("day_label") or f"Día {i+1}"),
                muscle_groups=list(d.get("muscle_groups") or []),
                suggested_exercises=str(d.get("suggested_exercises") or "").strip(),
                cardio=bool(d.get("cardio", False)),
                notes=str(d.get("notes") or "").strip(),
            )
            for i, d in enumerate(days_raw)
        ]
        return PlanSetupResult(mode="setup", days=days, raw_response=raw)

    if mode == "edit_day":
        return PlanSetupResult(
            mode="edit_day",
            day_label=str(data.get("day_label") or "").strip(),
            operation=str(data.get("operation") or "").strip().lower(),
            value=str(data.get("value") or "").strip(),
            raw_response=raw,
        )

    return PlanSetupResult(mode="unknown", raw_response=raw)


def _call_sonnet(text: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY no seteado")
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=_SONNET, max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return resp.content[0].text.strip() if resp.content else ""


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
