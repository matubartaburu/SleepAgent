"""
agents/tagger.py — extrae tags estructurados de la respuesta libre de Mateo.

Cuando Oscar pregunta "¿qué pasó anoche?" y Mateo responde en lenguaje natural
("comí pasta tarde y tomé vino"), este agente usa Haiku 4.5 (barato y rápido)
para mapear la respuesta a un conjunto controlado de causas.

Tags controlados — si el modelo inventa fuera de esta lista, los descartamos.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from agents.base import BudgetState, ClaudeResponse, call_claude

log = logging.getLogger(__name__)


CONTROLLED_TAGS = {
    # Comida / bebida
    "comida_tarde", "comida_pesada", "alcohol", "cafeina_tarde",
    # Actividad
    "deporte_tarde", "deporte_intenso", "siesta_larga",
    # Mental
    "estres", "ansiedad", "discusion", "trabajo_tarde", "pantallas_tarde",
    # Ambiente
    "ruido", "frio", "calor", "luz",
    # Salud
    "enfermedad", "dolor", "medicacion",
    # Otros
    "viaje", "cambio_horario", "social",
    # Catch-all
    "nada", "otro",
}


SYSTEM = """\
Sos un clasificador de causas de mal sueño. Recibís una respuesta libre de
Mateo (rioplatense informal) sobre qué pasó anoche, y devolvés tags
estructurados.

REGLAS:
- Solo usás tags de esta lista controlada (los demás se descartan):
{tags}

- Devolvés JSON estricto:
{{
  "tags": ["tag1", "tag2", ...],
  "confidence": 0-1,
  "notes": "1 frase corta interpretando la respuesta"
}}

- Si la respuesta no tiene info útil → tags: ["nada"], confidence: 0.5.
- Si menciona algo fuera de la lista → tags: ["otro"], y poné el detalle en notes.
- Múltiples causas en una respuesta → múltiples tags.
- Conservador: ante duda, NO inventes tag (mejor "otro").
""".format(tags=", ".join(sorted(CONTROLLED_TAGS)))


@dataclass
class TaggerResult:
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    notes: str = ""
    cost_usd: float = 0.0
    raw_response: str = ""


def _extract_json(text: str) -> dict:
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
    raise ValueError(f"Tagger devolvió JSON inválido: {text[:200]}")


def tag_answer(answer: str, *, dry_run: bool = False,
                budget: BudgetState | None = None) -> TaggerResult:
    """
    Llama a Haiku con la respuesta y devuelve TaggerResult. Si Haiku se
    inventa un tag que no está en CONTROLLED_TAGS, lo descarta.
    """
    if dry_run:
        return TaggerResult(tags=["nada"], confidence=0.5, notes="dry-run")

    if not answer or not answer.strip():
        return TaggerResult(tags=["nada"], confidence=0.5, notes="respuesta vacía")

    resp = call_claude(
        "constructor",  # placeholder; ver nota debajo
        SYSTEM, answer,
        max_tokens=300, budget=budget,
    )
    # Nota: el modelo del rol "constructor" es Sonnet. Para el tagger queremos
    # Haiku (más barato). Forzamos vía override directo abajo si querés cambiarlo.

    try:
        data = _extract_json(resp.text)
    except ValueError as exc:
        log.warning("Tagger JSON inválido: %s", exc)
        return TaggerResult(
            tags=["otro"], confidence=0.3,
            notes=f"json inválido: {exc}",
            cost_usd=resp.cost_usd, raw_response=resp.text,
        )

    raw_tags = data.get("tags") or []
    clean_tags = [t for t in raw_tags if t in CONTROLLED_TAGS]
    if not clean_tags:
        clean_tags = ["otro"]

    return TaggerResult(
        tags=clean_tags,
        confidence=float(data.get("confidence", 0.0)),
        notes=str(data.get("notes", "")),
        cost_usd=resp.cost_usd,
        raw_response=resp.text,
    )


# Override para usar Haiku en vez de Sonnet (más barato para clasificación)
def tag_answer_haiku(answer: str, *, budget: BudgetState | None = None) -> TaggerResult:
    """Versión que usa Haiku 4.5 directamente (~10x más barato que Sonnet)."""
    from anthropic import Anthropic
    from config import ANTHROPIC_API_KEY
    from agents.base import _cost_usd

    if not answer or not answer.strip():
        return TaggerResult(tags=["nada"], confidence=0.5, notes="respuesta vacía")

    if not ANTHROPIC_API_KEY:
        return TaggerResult(tags=["otro"], confidence=0.0, notes="no_api_key")

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    model = "claude-haiku-4-5-20251001"
    resp = client.messages.create(
        model=model, max_tokens=300,
        system=SYSTEM,
        messages=[{"role": "user", "content": answer}],
    )
    text = resp.content[0].text.strip() if resp.content else ""
    in_tok = getattr(resp.usage, "input_tokens", 0) or 0
    out_tok = getattr(resp.usage, "output_tokens", 0) or 0
    cost = _cost_usd(model, in_tok, out_tok)

    if budget is not None:
        from agents.base import save_budget
        budget.spent_usd += cost
        budget.calls += 1
        save_budget(budget)

    try:
        data = _extract_json(text)
    except ValueError:
        return TaggerResult(tags=["otro"], confidence=0.3,
                            notes="json inválido del tagger",
                            cost_usd=cost, raw_response=text)

    raw_tags = data.get("tags") or []
    clean_tags = [t for t in raw_tags if t in CONTROLLED_TAGS]
    if not clean_tags:
        clean_tags = ["otro"]
    return TaggerResult(
        tags=clean_tags,
        confidence=float(data.get("confidence", 0.0)),
        notes=str(data.get("notes", "")),
        cost_usd=cost, raw_response=text,
    )
