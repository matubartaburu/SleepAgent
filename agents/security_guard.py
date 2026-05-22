"""
agents/security_guard.py — security audit local previo a deploy.

Diseñado como un agente determinístico (no llama a LLMs por defecto, para
ser barato y reproducible). Chequea reglas concretas sobre la salud de
seguridad del proyecto:

1. .env nunca en git (staged/committed).
2. Secrets no aparecen en código en plain text.
3. .gitignore protege archivos sensibles.
4. Pre-commit hook instalado.
5. Endpoints autenticados / con firma.
6. Production guards activos cuando OSCAR_ENV=production.
7. Logging sanitization no truncado.

Devuelve un GuardReport con findings. Severity: critical | high | medium | low.

CLI:
    python -m agents.security_guard
    python -m agents.security_guard --fail-on-critical
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent


# ── Resultado ──────────────────────────────────────────────────────────────

@dataclass
class Finding:
    rule_id: str
    severity: str             # critical | high | medium | low
    title: str
    detail: str = ""
    file: str = ""
    suggestion: str = ""


@dataclass
class GuardReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def critical(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "critical"]

    @property
    def has_critical(self) -> bool:
        return bool(self.critical)

    def to_dict(self) -> dict:
        return {
            "total": len(self.findings),
            "by_severity": {
                sev: len([f for f in self.findings if f.severity == sev])
                for sev in ("critical", "high", "medium", "low")
            },
            "findings": [
                {
                    "rule": f.rule_id, "severity": f.severity,
                    "title": f.title, "detail": f.detail,
                    "file": f.file, "suggestion": f.suggestion,
                } for f in self.findings
            ],
        }


# ── Rules ──────────────────────────────────────────────────────────────────

_SECRET_PREFIXES = [
    ("sk-ant-",      "Anthropic key"),
    ("sk-proj-",     "OpenAI key"),
    ("ntn_",         "Notion token v2"),
    ("secret_",      "Notion token legacy"),
    ("FlyV1 ",       "Fly.io token"),
]
_HEX_SECRET = re.compile(r"\bAC[a-f0-9]{32}\b")  # Twilio SID


def _check_env_not_in_git() -> Finding | None:
    """Crítico: .env no debe estar tracked en git."""
    try:
        result = subprocess.run(
            ["git", "ls-files", ".env"], cwd=ROOT, capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            return Finding(
                rule_id="S001",
                severity="critical",
                title=".env está tracked en git",
                detail=f"git ls-files devolvió: {result.stdout.strip()}",
                suggestion="git rm --cached .env && git commit. Rotá TODOS los secrets que estaban dentro.",
            )
    except Exception as exc:
        return Finding(
            rule_id="S001",
            severity="medium",
            title="No pude verificar si .env está en git",
            detail=str(exc),
        )
    return None


def _check_gitignore_has_env() -> Finding | None:
    """High: .gitignore debe excluir .env."""
    gi = ROOT / ".gitignore"
    if not gi.exists():
        return Finding(rule_id="S002", severity="high",
                       title=".gitignore no existe",
                       suggestion="Crear .gitignore con al menos: .env, __pycache__/, .venv/")
    content = gi.read_text()
    if ".env" not in content:
        return Finding(rule_id="S002", severity="high",
                       title=".gitignore no menciona .env",
                       file=".gitignore",
                       suggestion="Agregá una línea: .env")
    return None


def _check_secrets_in_code() -> list[Finding]:
    """Critical: buscar prefijos de secretos en archivos versionados."""
    findings: list[Finding] = []
    # Solo escaneamos archivos de código y configs, no .venv ni .git ni data/
    excluded_dirs = {".venv", ".git", "__pycache__", "node_modules", "data", ".agents"}
    extensions = {".py", ".md", ".yml", ".yaml", ".toml", ".json", ".sh", ".cfg", ".ini"}

    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in excluded_dirs for part in path.parts):
            continue
        if path.suffix not in extensions:
            continue
        if path.name in {".env.example"}:
            continue
        # Skip nuestro propio security_guard.py (tiene los patterns)
        if path.name == "security_guard.py":
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for prefix, label in _SECRET_PREFIXES:
            # Buscamos el prefijo + al menos 20 chars de "secret"
            pattern = re.escape(prefix) + r"[a-zA-Z0-9_\-]{20,}"
            if re.search(pattern, content):
                findings.append(Finding(
                    rule_id="S003",
                    severity="critical",
                    title=f"Posible {label} hardcoded en código",
                    file=str(path.relative_to(ROOT)),
                    suggestion=f"Movelo a .env y leelo con os.getenv. Rotá el secret porque ya está en git history.",
                ))
        if _HEX_SECRET.search(content):
            findings.append(Finding(
                rule_id="S003",
                severity="critical",
                title="Posible Twilio Account SID hardcoded",
                file=str(path.relative_to(ROOT)),
                suggestion="Movelo a .env como TWILIO_ACCOUNT_SID.",
            ))
    return findings


def _check_pre_commit_hook() -> Finding | None:
    """Medium: pre-commit hook debería estar instalado."""
    hook = ROOT / ".git" / "hooks" / "pre-commit"
    if not hook.exists():
        return Finding(
            rule_id="S004",
            severity="medium",
            title="pre-commit hook no instalado",
            suggestion="Correr: bash scripts/install-hooks.sh",
        )
    return None


def _check_endpoint_auth() -> list[Finding]:
    """High: todos los endpoints sensibles deben requerir auth."""
    findings: list[Finding] = []
    main_py = ROOT / "main.py"
    if not main_py.exists():
        return [Finding(rule_id="S005", severity="critical",
                        title="main.py no existe")]
    content = main_py.read_text()
    # Decorators de FastAPI POST + nombre del path
    decorator_pattern = re.compile(r'@app\.(post|put|delete|patch)\("([^"]+)"\)', re.MULTILINE)
    for m in decorator_pattern.finditer(content):
        method, path = m.group(1), m.group(2)
        # /health y /whatsapp/inbound tienen su propia auth (firma Twilio)
        if path in {"/health", "/whatsapp/inbound"}:
            continue
        # Buscamos en las 15 líneas siguientes si hay un chequeo de INGEST_SECRET
        start = m.end()
        snippet = content[start:start + 800]
        if "INGEST_SECRET" not in snippet and "x_ingest_secret" not in snippet:
            findings.append(Finding(
                rule_id="S005",
                severity="high",
                title=f"Endpoint {method.upper()} {path} parece no requerir auth",
                file="main.py",
                detail="No detecté chequeo de INGEST_SECRET en las primeras líneas del handler.",
                suggestion="Agregá: if not INGEST_SECRET or x_ingest_secret != INGEST_SECRET: raise HTTPException(401)",
            ))
    return findings


def _check_production_guards_wired() -> Finding | None:
    """Medium: main.py debe importar y llamar production guards."""
    main_py = ROOT / "main.py"
    if not main_py.exists():
        return None
    content = main_py.read_text()
    if "install_production_filters" not in content:
        return Finding(
            rule_id="S006", severity="medium",
            title="Log sanitization no se está instalando",
            file="main.py",
            suggestion="Importá y llamá install_production_filters() al inicio de main.py.",
        )
    if "fail_if_missing_critical_secrets" not in content:
        return Finding(
            rule_id="S006", severity="medium",
            title="Production guards no están enchufados",
            file="main.py",
            suggestion="Llamá fail_if_missing_critical_secrets() al startup.",
        )
    return None


def _check_dockerignore_protects_env() -> Finding | None:
    """High: .dockerignore debe excluir .env (para que no entre a la imagen)."""
    di = ROOT / ".dockerignore"
    if not di.exists():
        return Finding(rule_id="S007", severity="high",
                       title=".dockerignore no existe",
                       suggestion="Crear .dockerignore que excluya .env, .venv, etc.")
    content = di.read_text()
    if ".env" not in content:
        return Finding(rule_id="S007", severity="high",
                       title=".dockerignore no excluye .env",
                       file=".dockerignore",
                       suggestion="Agregá `.env` y `.env.*` a .dockerignore.")
    return None


# ── Runner ────────────────────────────────────────────────────────────────

def run_audit() -> GuardReport:
    report = GuardReport()
    checks = [
        _check_env_not_in_git,
        _check_gitignore_has_env,
        _check_pre_commit_hook,
        _check_production_guards_wired,
        _check_dockerignore_protects_env,
    ]
    for check in checks:
        result = check()
        if result:
            report.findings.append(result)

    # Multi-finding checks
    report.findings.extend(_check_secrets_in_code())
    report.findings.extend(_check_endpoint_auth())

    return report


def _format_human(report: GuardReport) -> str:
    if not report.findings:
        return "✓ Security audit limpio (0 findings)."
    lines = [f"Security audit: {len(report.findings)} findings"]
    by_sev = report.to_dict()["by_severity"]
    lines.append("  " + " | ".join(f"{s}={n}" for s, n in by_sev.items() if n))
    for f in report.findings:
        marker = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}.get(f.severity, "·")
        lines.append(f"\n{marker} [{f.rule_id}] {f.title}")
        if f.file:
            lines.append(f"   file: {f.file}")
        if f.detail:
            lines.append(f"   detail: {f.detail}")
        if f.suggestion:
            lines.append(f"   fix: {f.suggestion}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(prog="security_guard")
    parser.add_argument("--json", action="store_true", help="Output JSON en vez de texto")
    parser.add_argument("--fail-on-critical", action="store_true",
                        help="Exit code != 0 si hay findings critical")
    parser.add_argument("--fail-on-high", action="store_true",
                        help="Exit code != 0 si hay findings critical o high")
    args = parser.parse_args()

    report = run_audit()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(_format_human(report))

    if args.fail_on_critical and report.has_critical:
        return 1
    if args.fail_on_high and any(f.severity in {"critical", "high"} for f in report.findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
