"""
agents/constructor.py — agente que implementa código.

Toma una `spec` (feature de features.json) y propone cambios concretos:
- Una lista de operaciones de edición/creación de archivos.
- Justificación corta.

Para MANTENER BAJO el riesgo y los tokens, el constructor:
- NO ejecuta nada por su cuenta.
- Devuelve un plan estructurado (JSON con operaciones).
- El orchestrator decide si aplicarlo o no.
- El humano puede revisar el plan antes de aprobar.

Las operaciones soportadas:
- {"op": "create_file", "path": "...", "content": "..."}
- {"op": "edit_file",   "path": "...", "find": "...", "replace": "..."}
- {"op": "append_file", "path": "...", "content": "..."}
- {"op": "note",        "text": "..."}     # nota humano-only, no se aplica
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from agents.base import (
    BudgetState,
    call_claude,
    handoff,
    load_contract,
)

log = logging.getLogger(__name__)


SYSTEM = """\
Sos el constructor de Oscar (un agente personal de sueño en FastAPI + Python).

Recibís una FEATURE SPEC y devolvés un PLAN DE IMPLEMENTACIÓN como JSON.

REGLAS DURAS:
- NO ejecutás nada, solo proponés.
- Cambios incrementales, mínimos, conservadores.
- No tocás archivos que la spec no menciona salvo si es estrictamente necesario.
- Respetás el estilo del codebase (mirá main.py / report.py / db.py de referencia).
- Si la spec es ambigua, pedís clarificación en una "note", no inventes.

Schema del output (JSON estricto, sin texto extra antes ni después):
{
  "summary": "1-2 frases de qué vas a hacer",
  "operations": [
    {"op": "create_file", "path": "agents/preflight.py", "content": "..."},
    {"op": "edit_file",   "path": "report.py", "find": "...", "replace": "..."},
    {"op": "append_file", "path": "main.py", "content": "..."},
    {"op": "note", "text": "Hace falta agregar una env var nueva"}
  ],
  "tests_to_write": [
    "Caso X: ...",
    "Caso Y: ..."
  ],
  "risks": ["...", "..."]
}

Si la feature es grande, partila: devolvé SOLO el primer subset (max 3 operations).
El orchestrator te volverá a llamar con el progreso.
"""


@dataclass
class ConstructorPlan:
    summary: str
    operations: list[dict] = field(default_factory=list)
    tests_to_write: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
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
    raise ValueError(f"Constructor devolvió JSON inválido: {text[:200]}")


def build(
    feature: dict,
    *,
    previous_feedback: str | None = None,
    dry_run: bool = False,
    budget: BudgetState | None = None,
) -> ConstructorPlan:
    """
    Genera un plan de implementación para la feature.

    Args:
        feature: dict de features.json
        previous_feedback: si es retry, qué dijo el validator/tests
        dry_run: no llama a la API
        budget: estado de presupuesto
    """
    if dry_run:
        return ConstructorPlan(
            summary=f"[dry-run] Plan placeholder para {feature['id']}",
            operations=[{"op": "note", "text": "dry-run, no se planeó nada real"}],
        )

    contract = load_contract()
    parts = [
        f"=== FEATURE ===\n{json.dumps(feature, indent=2)}",
        f"\n=== VALIDATION CONTRACT ===\n{contract}" if contract else "",
    ]
    if previous_feedback:
        parts.append(f"\n=== FEEDBACK DE INTENTO ANTERIOR ===\n{previous_feedback}")
    parts.append("\n\nDevolvé el JSON con tu plan.")
    user_msg = "\n".join(p for p in parts if p)

    resp = call_claude("constructor", SYSTEM, user_msg,
                       max_tokens=4096, budget=budget)
    try:
        data = _extract_json(resp.text)
    except ValueError as exc:
        log.error("Plan inválido: %s", exc)
        return ConstructorPlan(
            summary=f"INVALID_PLAN: {exc}",
            cost_usd=resp.cost_usd,
            raw_response=resp.text,
        )

    plan = ConstructorPlan(
        summary=str(data.get("summary", "")),
        operations=list(data.get("operations", [])),
        tests_to_write=list(data.get("tests_to_write", [])),
        risks=list(data.get("risks", [])),
        cost_usd=resp.cost_usd,
        raw_response=resp.text,
    )

    handoff(
        from_agent="constructor",
        to_agent="orchestrator",
        feature_id=feature.get("id"),
        event="plan_generated",
        summary=plan.summary,
        extra={
            "n_operations": len(plan.operations),
            "n_tests":      len(plan.tests_to_write),
            "n_risks":      len(plan.risks),
            "cost_usd":     plan.cost_usd,
        },
    )
    return plan
