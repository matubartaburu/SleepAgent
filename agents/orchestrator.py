"""
agents/orchestrator.py — el cerebro del loop multi-agente.

Flujo por feature:
  1. Lee feature de features.json.
  2. Llama al constructor → recibe plan.
  3. Llama al test_agent → recibe tests.
  4. Aplica operaciones del plan (con confirmación del humano si --interactive).
  5. Corre tests + lint + typecheck.
  6. Si algo falla → vuelve al constructor con feedback (max N iteraciones).
  7. Si pasa → marca feature done, archiva handoff final.

El orchestrator NO escribe código él mismo (lo hace el constructor). Su
trabajo es **decidir y re-scopear**.

CLI:
  python -m agents.orchestrator --list
  python -m agents.orchestrator --feature F-PREFLIGHT-001 --dry-run
  python -m agents.orchestrator --feature F-PREFLIGHT-001 --budget 2.00 --max-iters 3
  python -m agents.orchestrator --feature F-PREFLIGHT-001 --apply        # aplica plan al disco
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from agents.base import (
    AGENTS_DIR,
    BudgetState,
    get_feature,
    handoff,
    load_budget,
    load_features,
    reset_budget,
    save_budget,
    set_feature_status,
)
from agents.constructor import build as run_constructor
from agents.test_agent import write_tests as run_test_agent

log = logging.getLogger("orchestrator")

ROOT = Path(__file__).resolve().parent.parent


# ── Aplicar operaciones del plan al disco ──────────────────────────────────

def _apply_operations(operations: list[dict], *, dry: bool) -> list[str]:
    """
    Aplica operaciones al filesystem. Devuelve lista de archivos modificados.
    Si dry=True, solo loguea.
    """
    touched: list[str] = []
    for op in operations:
        kind = op.get("op")
        path = op.get("path", "")
        target = (ROOT / path).resolve() if path else None

        # Salvaguarda: no salir del repo.
        if target and ROOT not in target.parents and target != ROOT:
            log.warning("SKIP op fuera del repo: %s", target)
            continue

        if kind == "create_file":
            log.info("[%s] create_file %s (%d chars)",
                     "dry" if dry else "apply", path, len(op.get("content", "")))
            if not dry:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    log.warning("create_file: %s ya existe, salteo", path)
                    continue
                target.write_text(op.get("content", ""))
                touched.append(path)

        elif kind == "append_file":
            log.info("[%s] append_file %s (+%d chars)",
                     "dry" if dry else "apply", path, len(op.get("content", "")))
            if not dry:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a") as fh:
                    fh.write(op.get("content", ""))
                touched.append(path)

        elif kind == "edit_file":
            find = op.get("find", "")
            repl = op.get("replace", "")
            log.info("[%s] edit_file %s (find %d chars → replace %d chars)",
                     "dry" if dry else "apply", path, len(find), len(repl))
            if not dry:
                if not target.exists():
                    log.warning("edit_file: %s no existe, salteo", path)
                    continue
                content = target.read_text()
                if find not in content:
                    log.warning("edit_file: find no encontrado en %s", path)
                    continue
                target.write_text(content.replace(find, repl, 1))
                touched.append(path)

        elif kind == "note":
            log.info("NOTE: %s", op.get("text", ""))

        else:
            log.warning("Operación desconocida: %s", kind)
    return touched


# ── Correr suite ───────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    ok: bool
    output: str

    def short(self) -> str:
        return f"{'✅' if self.ok else '❌'} {self.name}"


def _run(cmd: list[str], *, name: str, timeout: int = 120) -> CheckResult:
    log.info("Corriendo %s: %s", name, " ".join(cmd))
    try:
        out = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout
        )
        ok = out.returncode == 0
        combined = (out.stdout or "") + (out.stderr or "")
        return CheckResult(name=name, ok=ok, output=combined[-4000:])
    except FileNotFoundError as exc:
        return CheckResult(name=name, ok=False, output=f"comando no encontrado: {exc}")
    except subprocess.TimeoutExpired:
        return CheckResult(name=name, ok=False, output=f"timeout {timeout}s")


def run_checks() -> list[CheckResult]:
    return [
        _run([".venv/bin/pytest", "-q", "-x", "--no-header"], name="pytest"),
        # ruff y mypy son opcionales: si no están instalados, no rompe nada
        _run([".venv/bin/ruff", "check", "."], name="ruff", timeout=30),
    ]


# ── Loop principal ─────────────────────────────────────────────────────────

def run_loop(
    feature_id: str,
    *,
    max_iters: int = 3,
    budget_usd: float = 2.0,
    dry_run: bool = False,
    apply_changes: bool = False,
) -> int:
    feature = get_feature(feature_id)
    if not feature:
        log.error("Feature %s no existe en features.json", feature_id)
        return 1

    log.info("=== Iniciando loop | feature=%s | dry_run=%s apply=%s budget=$%.2f ===",
             feature_id, dry_run, apply_changes, budget_usd)

    budget = load_budget(cap_usd=budget_usd)
    set_feature_status(feature_id, "in_progress")
    feedback = None

    for i in range(1, max_iters + 1):
        log.info("--- Iteración %d/%d (gastado=$%.4f / $%.2f) ---",
                 i, max_iters, budget.spent_usd, budget.cap_usd)

        plan = run_constructor(feature, previous_feedback=feedback,
                               dry_run=dry_run, budget=budget)
        log.info("Constructor: %s", plan.summary)
        for op in plan.operations:
            log.info("  op=%s path=%s", op.get("op"), op.get("path", ""))

        if budget.exceeded():
            log.error("Budget excedido tras constructor. Aborto.")
            handoff(from_agent="orchestrator", to_agent="human",
                    feature_id=feature_id, event="budget_exceeded",
                    summary=f"Spent ${budget.spent_usd:.4f} / cap ${budget.cap_usd:.2f}")
            return 2

        tests = run_test_agent(feature, asdict(plan), dry_run=dry_run, budget=budget)
        log.info("TestAgent: %s (%d archivos, %d criterios)",
                 tests.summary, len(tests.files), len(tests.coverage))

        if apply_changes and not dry_run:
            touched_code = _apply_operations(plan.operations, dry=False)
            touched_tests = _apply_operations(
                [{"op": "create_file", "path": f["path"], "content": f["content"]}
                 for f in tests.files],
                dry=False,
            )
            log.info("Aplicado: code=%s tests=%s", touched_code, touched_tests)

            checks = run_checks()
            for c in checks:
                log.info(c.short())
            if all(c.ok for c in checks):
                set_feature_status(feature_id, "done")
                handoff(from_agent="orchestrator", to_agent="human",
                        feature_id=feature_id, event="feature_done",
                        summary=f"Iteración {i} pasó todos los checks.",
                        files_touched=touched_code + touched_tests)
                log.info("=== Feature %s LISTA en iteración %d ===", feature_id, i)
                return 0
            else:
                feedback = "\n\n".join(
                    f"--- {c.name} ---\n{c.output}" for c in checks if not c.ok
                )
                log.warning("Checks fallaron, vuelvo al constructor con feedback.")
        else:
            log.info("--apply NO seteado: ni se escribió a disco ni se corrieron checks.")
            log.info("Plan completo guardado en stdout. Revisalo y corré con --apply si te convence.")
            return 0

    log.error("=== No convergió en %d iteraciones (feature=%s) ===", max_iters, feature_id)
    handoff(from_agent="orchestrator", to_agent="human",
            feature_id=feature_id, event="no_convergence",
            summary=f"Max iteraciones alcanzadas ({max_iters}).")
    return 3


# ── CLI ────────────────────────────────────────────────────────────────────

def _cli_list() -> None:
    data = load_features()
    for f in data.get("features", []):
        print(f"  [{f['status']:>11}] {f['id']:<25} {f['title']}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="orchestrator")
    parser.add_argument("--list", action="store_true", help="Lista features y sale")
    parser.add_argument("--feature", help="ID de feature a procesar")
    parser.add_argument("--max-iters", type=int, default=3)
    parser.add_argument("--budget", type=float, default=2.0, help="Cap en USD")
    parser.add_argument("--dry-run", action="store_true",
                        help="No llama a Claude, no toca disco. Solo muestra estructura.")
    parser.add_argument("--apply", action="store_true",
                        help="Aplica las operaciones al disco y corre checks.")
    parser.add_argument("--reset-budget", action="store_true",
                        help="Resetea el budget acumulado a 0.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    if args.reset_budget:
        reset_budget(cap_usd=args.budget)
        print(f"Budget reseteado, cap=${args.budget:.2f}")
        return 0

    if args.list or not args.feature:
        _cli_list()
        return 0

    return run_loop(
        args.feature,
        max_iters=args.max_iters,
        budget_usd=args.budget,
        dry_run=args.dry_run,
        apply_changes=args.apply,
    )


if __name__ == "__main__":
    sys.exit(main())
