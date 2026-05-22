"""
agents/answerer.py — responde preguntas libres de Mateo sobre su sueño.

Mateo escribe a Oscar fuera del flujo de pregunta-respuesta dirigido por
anomalías. Este agente:
  1. Trae las últimas N noches de sleep_logs.
  2. Las inyecta como contexto compacto en el prompt.
  3. Sonnet 4.6 redacta una respuesta corta en rioplatense.

Costo aprox por pregunta: $0.012 (Sonnet, ~3k tokens input).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY

log = logging.getLogger(__name__)

SONNET_MODEL = "claude-sonnet-4-6"

# Zona horaria del usuario — TZ de config.py (default America/Montevideo).
_USER_TZ = ZoneInfo(os.getenv("TZ", "America/Montevideo"))


def _fmt_local_dt(iso_ts: str | None) -> str | None:
    """Convierte un timestamp UTC (de Supabase) a hora local de Mateo."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace(" ", "T"))
        return dt.astimezone(_USER_TZ).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_ts


ANSWERER_SYSTEM = """\
Sos Oscar, agente personal de sueño de Mateo (uruguayo ~30 años). Mateo
te escribe libremente por WhatsApp. Tu trabajo es contestarle directo y
corto, basándote SOLO en los datos que te paso.

CONTEXTO CONVERSACIONAL
- Vas a recibir el historial de los últimos mensajes intercambiados con
  Mateo (turnos previos). USALO para resolver referencias: si dice "y a
  qué hora me desperté" después de haber hablado del 3 de marzo, asumí
  que sigue hablando del 3 de marzo.
- Si el contexto previo no aclara la pregunta, preguntá UNA cosa para
  clarificar antes de inventar.

ESTILO
- Rioplatense informal: vos, podés, dale, ta, joya, bárbaro.
- Sin emojis, sin markdown, sin asteriscos, sin negritas.
- Texto plano, 1-3 bloques cortos estilo WhatsApp.
- Preguntas con `?`. Nunca abrir con `¿`.
- Variá afirmaciones, evitá "perfecto".

CONTENIDO
- Si te pregunta una hora puntual, devolvé la hora en formato HH:MM y
  marcá si es AM/PM explícitamente cuando pueda haber confusión (ej:
  "12:46 del mediodía" o "00:30 de madrugada"). Una frase basta.
- Si te pregunta cuánto durmió o cómo viene una métrica, devolvé el dato
  + 1 frase corta de contexto si suma.
- Si te pregunta resumen / comparativa, dale 2-3 datos clave, no listes todo.

NOTA SOBRE SIESTAS
- Apple Watch a veces registra siestas como sleep_analysis. Si una "noche"
  tiene sleep_start entre las 10:00 y las 19:00, probablemente es siesta,
  no la noche principal. Si Mateo no aclara, asumí que pregunta por la
  noche principal y mencioná si lo único que ves es una siesta.

FORMATO DE LOS TIMESTAMPS
- Los campos `in=` y `out=` vienen ya en HORA LOCAL de Mateo (Montevideo, UTC-3).
  NO le sumes ni restes horas. Si dice in=2026-05-16 02:43, decí "02:43" tal cual.

LÍMITES DUROS
- Si no tenés data para la fecha → decilo honesto: "no tengo data para esa
  fecha" o "esa noche no la tengo registrada". NO INVENTES.
- Si la fecha es futura → "todavía no pasó, no puedo contestarte eso".
- Si la pregunta no es sobre sueño/recovery → respondé corto que solo te
  dedicás al sueño: "yo solo te puedo hablar del sueño, eso no es lo mío".
- No saludes con "Hola Mateo"; saltá directo a la respuesta.
"""


def _fmt_hm(minutes) -> str:
    if minutes is None:
        return "—"
    minutes = int(minutes)
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}" if h else f"{m}min"


def _compact_row(r: dict) -> str:
    """Una línea por noche, lo más compacto posible para gastar pocos tokens.

    Importante: los timestamps en DB están en UTC. Los convertimos a hora
    LOCAL de Mateo antes de pasarlos a Sonnet — sino la respuesta sale 3h
    corrida ("te dormiste a las 05:43" cuando real fue 02:43 Mvd).
    """
    parts = [r["night_date"]]
    local_start = _fmt_local_dt(r.get("sleep_start"))
    local_end   = _fmt_local_dt(r.get("sleep_end"))
    if local_start:
        parts.append(f"in={local_start}")
    if local_end:
        parts.append(f"out={local_end}")
    if r.get("total_sleep_minutes") is not None:
        parts.append(f"total={_fmt_hm(r['total_sleep_minutes'])}")
    if r.get("rem_minutes") is not None:
        parts.append(f"rem={_fmt_hm(r['rem_minutes'])}")
    if r.get("deep_minutes") is not None:
        parts.append(f"deep={_fmt_hm(r['deep_minutes'])}")
    if r.get("awake_minutes") is not None:
        parts.append(f"awake={_fmt_hm(r['awake_minutes'])}")
    if r.get("hrv_sdnn_ms") is not None:
        parts.append(f"hrv={r['hrv_sdnn_ms']:.0f}")
    if r.get("resting_hr_bpm") is not None:
        parts.append(f"rhr={r['resting_hr_bpm']:.0f}")
    if r.get("respiratory_rate_brpm") is not None:
        parts.append(f"rr={r['respiratory_rate_brpm']:.1f}")
    return " | ".join(parts)


def _build_user_message(question: str, nights: list[dict]) -> str:
    if not nights:
        body = "(sin filas en sleep_logs todavía)"
    else:
        body = "\n".join(_compact_row(n) for n in nights)
    from datetime import date as _d
    return (
        f"PREGUNTA DE MATEO:\n{question}\n\n"
        f"=== DATA DISPONIBLE (últimas {len(nights)} noches, desc por fecha) ===\n"
        f"Hoy es {_d.today().isoformat()}.\n"
        f"{body}\n\n"
        f"Generá la respuesta corta para Mateo en rioplatense."
    )


@dataclass
class AnswererResult:
    text: str
    n_nights_used: int
    chars: int
    n_history_turns: int = 0


def _build_messages(question: str, nights: list[dict],
                    history: list[dict] | None) -> list[dict]:
    """
    Arma el array de messages para Anthropic con la data + history + question.

    Estructura:
      [...turnos previos como user/assistant alternados...,
       {"role": "user", "content": "<data + pregunta actual>"}]

    La data se pega en el último user message (no en cada turno) para no
    repetirla y ahorrar tokens.
    """
    msgs: list[dict] = []

    # Sanitizamos history: solo alternancia user/assistant; el último DEBE ser
    # user (la pregunta nueva), pero acá agregamos hasta el penúltimo.
    if history:
        # Excluimos el mensaje actual de Mateo si ya está en la history
        # (puede aparecer cuando Twilio inbound webhook fire antes de la query).
        clean = [h for h in history if h.get("content", "").strip() != question.strip()]
        # Anthropic requiere que alterne. Colapsamos turnos del mismo role
        # consecutivos concatenando con \n.
        prev_role = None
        for h in clean:
            role = h["role"]
            content = h["content"]
            if role == prev_role and msgs:
                msgs[-1]["content"] += f"\n{content}"
            else:
                msgs.append({"role": role, "content": content})
                prev_role = role
        # Si el último es 'assistant', dejamos así. Si es 'user', tenemos que
        # asegurarnos que el siguiente user (la pregunta nueva con data) lo
        # colapse — pero Anthropic acepta dos user seguidos si los separamos
        # con un assistant placeholder. Más simple: si termina en user,
        # colapsamos la pregunta nueva ahí.

    user_with_data = _build_user_message(question, nights)
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += f"\n\n{user_with_data}"
    else:
        msgs.append({"role": "user", "content": user_with_data})

    return msgs


def answer_question(question: str, *,
                    limit_nights: int = 60,
                    history_turns: int = 10,
                    skip_history: bool = False) -> AnswererResult:
    """
    Trae las últimas `limit_nights` noches + `history_turns` mensajes previos
    y le pide a Sonnet que conteste teniendo en cuenta el contexto.

    Args:
        skip_history: si True, ignora la conversación previa (modo stateless).
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("Falta ANTHROPIC_API_KEY")

    from db import get_last_n_nights
    nights = get_last_n_nights(limit_nights)

    history: list[dict] = []
    if not skip_history:
        try:
            from twilio_client import get_recent_conversation
            history = get_recent_conversation(limit=history_turns)
        except Exception as exc:
            log.warning("No pude traer history de Twilio: %s", exc)

    messages = _build_messages(question, nights, history)

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=SONNET_MODEL, max_tokens=400,
        system=ANSWERER_SYSTEM,
        messages=messages,
    )
    text = resp.content[0].text.strip() if resp.content else ""
    log.info("Answerer respondió (%d chars) usando %d noches + %d turnos",
             len(text), len(nights), len(history))
    return AnswererResult(text=text, n_nights_used=len(nights),
                          chars=len(text), n_history_turns=len(history))
