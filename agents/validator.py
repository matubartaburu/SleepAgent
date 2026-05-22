"""
agents/validator.py — LLM-as-judge para reportes de Oscar.

Toma un texto generado + los datos crudos + el contrato, y decide si sale o no.
Usa Opus 4.7 para asimetría con el generador (Sonnet 4.6).

API pública:
- validate_report(text, raw_data, baseline) -> ValidatorVerdict
- ValidatorVerdict.approved : bool
- ValidatorVerdict.issues   : list[str]
- ValidatorVerdict.fact_check: list[dict]
- ValidatorVerdict.score    : int (0-10)
- ValidatorVerdict.cost_usd : float
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from agents.base import (
    BudgetState,
    ClaudeResponse,
    call_claude,
    handoff,
    load_contract,
)

log = logging.getLogger(__name__)


SYSTEM = """\
Sos el validador de Oscar (agente personal de sueño de Mateo). Tu único trabajo
es decidir si un reporte generado puede salir por WhatsApp o no.

Sos un crítico estricto pero justo. Tenés sesgo a RECHAZAR cuando dudás:
es preferible regenerar a mandar algo malo.

Vas a recibir:
1. El CONTRATO con las reglas que el mensaje debe cumplir.
2. Los DATOS CRUDOS de la noche analizada y el baseline 7d.
3. El MENSAJE GENERADO por el reporter.

Tu respuesta DEBE ser un JSON válido, sin texto extra antes o después.
Schema:
{
  "approved": bool,
  "score": int (0-10, 10 = excelente, 7 = aceptable, <7 = rechazar),
  "issues": [string, ...],   // lista de violaciones concretas al contrato
  "fact_check": [
    {"claim": "...", "supported": bool, "evidence": "..."}
  ],
  "suggested_fix_hint": "una pista para el reporter si rechazaste (corta)"
}

Reglas para vos:
- Si encontrás CUALQUIER violación dura del contrato → approved=false.
- Si encontrás >=2 issues blandas → approved=false.
- Sé específico en `issues`: cita la palabra/frase ofensora.
- En `fact_check` chequeá cada cifra del mensaje contra los datos crudos.
"""


@dataclass
class ValidatorVerdict:
    approved: bool
    score: int
    issues: list[str] = field(default_factory=list)
    fact_check: list[dict] = field(default_factory=list)
    suggested_fix_hint: str = ""
    cost_usd: float = 0.0
    raw_response: str = ""


def _extract_json(text: str) -> dict:
    """Claude a veces envuelve el JSON en ```json ... ``` o agrega texto."""
    # Probar parsing directo
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Buscar bloque ```json ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Buscar el primer { ... } balanceado
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"No pude extraer JSON de la respuesta del validator: {text[:200]}")


def validate_report(
    text: str,
    raw_data: dict,
    baseline: dict | None = None,
    *,
    contract: str | None = None,
    dry_run: bool = False,
    budget: BudgetState | None = None,
) -> ValidatorVerdict:
    """
    Args:
        text: mensaje generado por el reporter.
        raw_data: dict de la noche evaluada (cols de sleep_logs).
        baseline: dict con promedios 7d (sleep_min, hrv, rhr, rr, deep_min, rem_min).
        contract: si None, lee .agents/validation.contract.md.
        dry_run: si True, devuelve un verdict "aprobado" placeholder sin llamar API.
    """
    if dry_run:
        return ValidatorVerdict(
            approved=True, score=10,
            issues=["<dry-run, no se validó>"],
        )

    contract_md = contract or load_contract()
    if not contract_md:
        log.warning("Validator corriendo sin contrato (validation.contract.md vacío)")

    user_msg = (
        f"=== CONTRATO ===\n{contract_md}\n\n"
        f"=== DATOS CRUDOS (anoche) ===\n{json.dumps(raw_data, indent=2, default=str)}\n\n"
        f"=== BASELINE 7D ===\n{json.dumps(baseline or {}, indent=2, default=str)}\n\n"
        f"=== MENSAJE GENERADO ===\n{text}\n\n"
        f"Devolvé el JSON con tu veredicto."
    )

    resp = call_claude("validator", SYSTEM, user_msg,
                       max_tokens=1024, dry_run=False, budget=budget)
    try:
        data = _extract_json(resp.text)
    except ValueError as exc:
        log.error("Validator devolvió JSON inválido: %s", exc)
        return ValidatorVerdict(
            approved=False, score=0,
            issues=[f"validator_invalid_json: {exc}"],
            cost_usd=resp.cost_usd, raw_response=resp.text,
        )

    verdict = ValidatorVerdict(
        approved=bool(data.get("approved")),
        score=int(data.get("score", 0)),
        issues=list(data.get("issues", [])),
        fact_check=list(data.get("fact_check", [])),
        suggested_fix_hint=str(data.get("suggested_fix_hint", "")),
        cost_usd=resp.cost_usd,
        raw_response=resp.text,
    )

    handoff(
        from_agent="validator",
        to_agent="orchestrator" if not verdict.approved else "twilio",
        feature_id=None,
        event="validation",
        summary=(
            f"approved={verdict.approved} score={verdict.score} "
            f"issues={len(verdict.issues)} cost=${verdict.cost_usd:.4f}"
        ),
        extra={
            "issues": verdict.issues,
            "fact_check_failures": [fc for fc in verdict.fact_check if not fc.get("supported")],
        },
    )
    return verdict


# ── CLI: validar el último reporte de smoke test ────────────────────────────

def _cli_validate_last_report() -> int:
    """Modo conveniente: agarra la última noche de DB, genera con report.py y valida."""
    from report import _build_user_message, SYSTEM_PROMPT, SONNET_MODEL
    from db import _client as _supabase

    res = (
        _supabase()
        .table("sleep_logs")
        .select(
            "night_date, total_sleep_minutes, in_bed_minutes, "
            "rem_minutes, core_minutes, deep_minutes, awake_minutes, "
            "hrv_sdnn_ms, resting_hr_bpm, avg_hr_bpm, min_hr_bpm, max_hr_bpm, "
            "respiratory_rate_brpm"
        )
        .order("night_date", desc=True)
        .limit(7)
        .execute()
    )
    rows = res.data or []
    if not rows:
        print("Sin filas en sleep_logs")
        return 1

    last_night = rows[0]

    def avg(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    baseline = {
        "sleep_min": avg("total_sleep_minutes"),
        "hrv":       avg("hrv_sdnn_ms"),
        "rhr":       avg("resting_hr_bpm"),
        "rr":        avg("respiratory_rate_brpm"),
        "deep_min":  avg("deep_minutes"),
        "rem_min":   avg("rem_minutes"),
    }

    # Generamos con Sonnet (reporter)
    from anthropic import Anthropic
    from config import ANTHROPIC_API_KEY
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    user_msg = _build_user_message({
        "last_night": last_night, "baseline": baseline, "n_baseline": len(rows)
    })
    gen = client.messages.create(
        model=SONNET_MODEL, max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    generated = gen.content[0].text.strip()
    print("\n=== MENSAJE GENERADO ===\n" + generated + "\n")

    verdict = validate_report(generated, last_night, baseline)
    print("=== VEREDICTO ===")
    print(f"approved: {verdict.approved}")
    print(f"score:    {verdict.score}/10")
    print(f"cost:     ${verdict.cost_usd:.4f}")
    if verdict.issues:
        print("issues:")
        for i in verdict.issues:
            print(f"  - {i}")
    return 0 if verdict.approved else 1


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    if "--validate-last-report" in sys.argv:
        sys.exit(_cli_validate_last_report())
    print("Uso: python -m agents.validator --validate-last-report")
    sys.exit(2)
