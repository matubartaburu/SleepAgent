"""
agents/base.py — utilidades compartidas: cliente Claude, registro de handoffs,
budget tracking, lectura del shared state.

Diseño:
- Un solo Anthropic client cacheado.
- call_claude(...) devuelve texto + uso de tokens, registra en budget.json.
- handoff(...) hace append en .agents/handoffs.jsonl.
- load_contract() / load_features() leen el shared state.

Pensado para mantener bajo el gasto de tokens:
- Modelos por rol (Sonnet para generación, Opus solo para crítica).
- Budget cap por feature.
- Dry-run mode que NO llama a Claude.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / ".agents"
FEATURES_FILE = AGENTS_DIR / "features.json"
HANDOFFS_FILE = AGENTS_DIR / "handoffs.jsonl"
CONTRACT_FILE = AGENTS_DIR / "validation.contract.md"
BUDGET_FILE = AGENTS_DIR / "budget.json"

# Modelo por rol. Cambiar acá si querés barato/caro.
MODELS = {
    "constructor":  "claude-sonnet-4-6",
    "test_agent":   "claude-sonnet-4-6",
    "validator":    "claude-opus-4-7",
    "orchestrator": "claude-opus-4-7",
    "reporter":     "claude-sonnet-4-6",
}

# Precios USD por millón de tokens (aprox, ajustar si cambian).
# Sirven SOLO para tracking de budget local, no son la fuente de verdad.
PRICING_PER_MTOK = {
    "claude-sonnet-4-6":         {"input": 3.0,  "output": 15.0},
    "claude-opus-4-7":           {"input": 15.0, "output": 75.0},
    "claude-haiku-4-5-20251001": {"input": 1.0,  "output": 5.0},
}


# ── Anthropic client (lazy + singleton) ────────────────────────────────────

_client: Anthropic | None = None


def _anthropic() -> Anthropic:
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# ── Budget tracking ────────────────────────────────────────────────────────

@dataclass
class BudgetState:
    spent_usd: float = 0.0
    cap_usd: float = 5.0
    calls: int = 0

    def remaining(self) -> float:
        return self.cap_usd - self.spent_usd

    def exceeded(self) -> bool:
        return self.spent_usd >= self.cap_usd


def load_budget(cap_usd: float | None = None) -> BudgetState:
    if BUDGET_FILE.exists():
        data = json.loads(BUDGET_FILE.read_text())
        state = BudgetState(**data)
    else:
        state = BudgetState()
    if cap_usd is not None:
        state.cap_usd = cap_usd
    return state


def save_budget(state: BudgetState) -> None:
    BUDGET_FILE.write_text(json.dumps({
        "spent_usd": round(state.spent_usd, 6),
        "cap_usd":   state.cap_usd,
        "calls":     state.calls,
    }, indent=2))


def reset_budget(cap_usd: float = 5.0) -> None:
    save_budget(BudgetState(spent_usd=0.0, cap_usd=cap_usd, calls=0))


def _cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price = PRICING_PER_MTOK.get(model)
    if not price:
        return 0.0
    return (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000


# ── Llamada a Claude ───────────────────────────────────────────────────────

@dataclass
class ClaudeResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    dry_run: bool = False


def call_claude(
    role: str,
    system: str,
    user: str,
    *,
    max_tokens: int = 1024,
    dry_run: bool = False,
    budget: BudgetState | None = None,
) -> ClaudeResponse:
    """
    Llama a Claude con el modelo asociado al rol. Registra costo en budget.

    Args:
        role: clave en MODELS (constructor, validator, etc.)
        system: system prompt
        user: user message
        max_tokens: cap del output
        dry_run: si True, no llama a la API, devuelve un placeholder
        budget: si pasás un BudgetState y se excede el cap, levanta RuntimeError
    """
    model = MODELS.get(role)
    if not model:
        raise ValueError(f"Rol desconocido: {role}. Valores: {list(MODELS)}")

    if dry_run:
        log.info("[dry-run] role=%s model=%s would call Claude with %d chars system + %d chars user",
                 role, model, len(system), len(user))
        return ClaudeResponse(text="<dry-run, no se llamó a Claude>",
                              model=model, dry_run=True)

    if budget and budget.exceeded():
        raise RuntimeError(
            f"Budget excedido (gastado=${budget.spent_usd:.2f}, cap=${budget.cap_usd:.2f}). "
            "Pausá el orchestrator y revisá."
        )

    resp = _anthropic().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = resp.content[0].text.strip() if resp.content else ""
    in_tok = getattr(resp.usage, "input_tokens", 0) or 0
    out_tok = getattr(resp.usage, "output_tokens", 0) or 0
    cost = _cost_usd(model, in_tok, out_tok)

    if budget is not None:
        budget.spent_usd += cost
        budget.calls += 1
        save_budget(budget)

    log.info(
        "call_claude | role=%s model=%s in=%d out=%d cost=$%.4f",
        role, model, in_tok, out_tok, cost,
    )
    return ClaudeResponse(
        text=text,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost,
        model=model,
    )


# ── Shared state I/O ───────────────────────────────────────────────────────

def load_contract() -> str:
    if not CONTRACT_FILE.exists():
        return ""
    return CONTRACT_FILE.read_text()


def load_features() -> dict:
    if not FEATURES_FILE.exists():
        return {"features": []}
    return json.loads(FEATURES_FILE.read_text())


def save_features(data: dict) -> None:
    FEATURES_FILE.write_text(json.dumps(data, indent=2))


def get_feature(feature_id: str) -> dict | None:
    for f in load_features().get("features", []):
        if f["id"] == feature_id:
            return f
    return None


def set_feature_status(feature_id: str, status: str) -> None:
    data = load_features()
    for f in data["features"]:
        if f["id"] == feature_id:
            f["status"] = status
            break
    save_features(data)


def handoff(
    *,
    from_agent: str,
    to_agent: str,
    feature_id: str | None,
    event: str,
    summary: str,
    files_touched: list[str] | None = None,
    next_step: str = "",
    extra: dict | None = None,
) -> None:
    """Append-only log de qué pasó. Para auditar y para que el orchestrator decida."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "from": from_agent,
        "to": to_agent,
        "feature_id": feature_id,
        "event": event,
        "summary": summary,
        "files_touched": files_touched or [],
        "next": next_step,
    }
    if extra:
        entry["extra"] = extra
    AGENTS_DIR.mkdir(exist_ok=True)
    with HANDOFFS_FILE.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
