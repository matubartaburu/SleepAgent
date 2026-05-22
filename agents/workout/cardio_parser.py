"""
agents/workout/cardio_parser.py — parser de sesiones de cardio manual
(las que NO graba el Apple Watch: futbol, escalada, yoga, etc.).

Recibe texto:
  "corrí 8km en 45 minutos, intenso"
  "hoy hice 1 hora de futbol con amigos, fue moderado"
  "30 minutos de yoga suave"

Devuelve:
  {sport, duration_min, distance_km, intensity, rpe, notes, resolved_date}
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta


from anthropic import Anthropic

from config import ANTHROPIC_API_KEY

log = logging.getLogger(__name__)

_SONNET = "claude-sonnet-4-6"


SYSTEM_PROMPT = """\
Sos un parser de sesiones de cardio descritas en español rioplatense.
Recibís texto natural y devolvés JSON con estos campos:

- sport: uno de [running, cycling, walking, swimming, hiking, futbol,
                  tenis, escalada, yoga, hiit, otro]
- duration_min: número (minutos totales). null si no se menciona.
- distance_km: número en km. null si no aplica o no se menciona.
- intensity: uno de [suave, moderada, intensa, intervalos]. null si no se menciona.
- rpe: 1-10 si se menciona "rpe N" o equivalente verbal. null si no.
- notes: comentarios libres relevantes.
- date_hint: "today" | "yesterday" | "day_before_yesterday" | "N_days_ago:N" | "weekday:lunes"

REGLAS:
- "corrí 8k" o "corrí 8km" → sport=running, distance_km=8
- "1 hora", "60 min", "una hora y media" → convertir a minutos
- "intenso", "duro" → intensity=intensa
- "tranqui", "suave" → intensity=suave
- "rpe 7" → rpe=7
- "me costó" sin RPE explícito → intensity=intensa, rpe=null
- Si no podés clasificar el deporte → sport="otro"

OUTPUT — JSON estricto:
{
  "sport": "running",
  "duration_min": 45,
  "distance_km": 8.0,
  "intensity": "intensa",
  "rpe": null,
  "notes": "",
  "date_hint": "today"
}

Si el texto NO es sobre cardio, devolvé:
{"sport": null, "duration_min": null, "distance_km": null, "intensity": null,
 "rpe": null, "notes": "no_cardio_detected", "date_hint": "today"}
"""


@dataclass
class CardioParseResult:
    sport: str | None = None
    duration_min: float | None = None
    distance_km: float | None = None
    intensity: str | None = None
    rpe: int | None = None
    notes: str = ""
    resolved_date: date | None = None
    date_hint: str = "today"
    raw_response: str = ""

    @property
    def is_cardio(self) -> bool:
        return self.sport is not None


def parse(text: str, *, today: date | None = None) -> CardioParseResult:
    if not text or not text.strip():
        return CardioParseResult(resolved_date=today or date.today())

    today = today or date.today()
    raw = _call_sonnet(text)
    data = _extract_json(raw)

    sport = data.get("sport")
    if not sport:
        return CardioParseResult(resolved_date=today, raw_response=raw,
                                  notes="no_cardio_detected")

    date_hint = str(data.get("date_hint") or "today")
    resolved = _resolve_date(date_hint, today)

    return CardioParseResult(
        sport=str(sport).strip().lower(),
        duration_min=_to_float(data.get("duration_min")),
        distance_km=_to_float(data.get("distance_km")),
        intensity=str(data.get("intensity")).strip().lower() if data.get("intensity") else None,
        rpe=_to_int(data.get("rpe")),
        notes=str(data.get("notes") or "").strip(),
        resolved_date=resolved,
        date_hint=date_hint,
        raw_response=raw,
    )


# ── Internals (compartido idéntico con workout_parser) ─────────────────────

def _call_sonnet(text: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY no seteado")
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=_SONNET, max_tokens=500,
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
    return {}


def _to_int(v) -> int | None:
    if v is None: return None
    try: return int(v)
    except (TypeError, ValueError): return None


def _to_float(v) -> float | None:
    if v is None: return None
    try: return float(v)
    except (TypeError, ValueError): return None


_WEEKDAYS = {
    "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
    "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6,
}


def _resolve_date(hint: str, today: date) -> date:
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
            delta = (today.weekday() - target) % 7
            if delta == 0: delta = 7
            return today - timedelta(days=delta)
    return today
