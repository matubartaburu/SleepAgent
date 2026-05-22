"""
agents/test_agent.py — agente que escribe tests a partir de una spec.

Recibe la spec de una feature + el plan del constructor, y devuelve tests
(unitarios + integración) que cubran los acceptance_criteria.

Devuelve un JSON con archivos de test propuestos. NO ejecuta nada.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from agents.base import BudgetState, call_claude, handoff

log = logging.getLogger(__name__)


SYSTEM = """\
Sos el agente de tests de Oscar.

Recibís:
- Una FEATURE SPEC con `acceptance_criteria`.
- Un PLAN del constructor con las operaciones propuestas.

Devolvés tests en pytest, organizados por archivo. Cubrís TODOS los
acceptance_criteria, no inventes pruebas que no estén en la spec.

REGLAS:
- pytest. Imports al tope. Sin clases (usa funciones `test_*`).
- Mockeás servicios externos (Anthropic, Twilio, Supabase) con `unittest.mock` o `pytest-mock`.
- Tests integración (que tocan red/DB) llevan `@pytest.mark.integration`.
- Cada test es CORTO (<25 líneas). Si necesita más, partilo.
- Fixtures compartidos van en `tests/conftest.py`.

Schema del output (JSON estricto):
{
  "summary": "qué cubrís",
  "files": [
    {"path": "tests/test_preflight.py", "content": "..."},
    {"path": "tests/conftest.py", "content": "..."}
  ],
  "coverage": [
    {"criterion": "Existe función preflight_check()...", "test": "test_preflight_returns_dict"}
  ]
}
"""


@dataclass
class TestPlan:
    summary: str
    files: list[dict] = field(default_factory=list)
    coverage: list[dict] = field(default_factory=list)
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
    raise ValueError(f"TestAgent devolvió JSON inválido: {text[:200]}")


def write_tests(
    feature: dict,
    constructor_plan: dict | None = None,
    *,
    dry_run: bool = False,
    budget: BudgetState | None = None,
) -> TestPlan:
    if dry_run:
        return TestPlan(
            summary=f"[dry-run] Tests placeholder para {feature['id']}",
        )

    user_parts = [f"=== FEATURE ===\n{json.dumps(feature, indent=2)}"]
    if constructor_plan:
        user_parts.append(f"\n=== PLAN DEL CONSTRUCTOR ===\n{json.dumps(constructor_plan, indent=2)}")
    user_parts.append("\nDevolvé el JSON con los tests.")
    user_msg = "\n".join(user_parts)

    resp = call_claude("test_agent", SYSTEM, user_msg,
                       max_tokens=4096, budget=budget)
    try:
        data = _extract_json(resp.text)
    except ValueError as exc:
        log.error("TestPlan inválido: %s", exc)
        return TestPlan(
            summary=f"INVALID_TESTS: {exc}",
            cost_usd=resp.cost_usd, raw_response=resp.text,
        )

    plan = TestPlan(
        summary=str(data.get("summary", "")),
        files=list(data.get("files", [])),
        coverage=list(data.get("coverage", [])),
        cost_usd=resp.cost_usd,
        raw_response=resp.text,
    )
    handoff(
        from_agent="test_agent",
        to_agent="orchestrator",
        feature_id=feature.get("id"),
        event="tests_generated",
        summary=plan.summary,
        extra={
            "n_files":    len(plan.files),
            "n_criteria": len(plan.coverage),
            "cost_usd":   plan.cost_usd,
        },
    )
    return plan
