# .agents/ — Shared state del sistema multi-agente

Estado compartido que TODOS los agentes leen y/o escriben. Versionado en git.

## Archivos

| Archivo | Quién lo lee | Quién lo escribe | Para qué |
|---|---|---|---|
| `features.json` | todos | orchestrator | Lista de features pendientes/en progreso/hechas |
| `validation.contract.md` | validator (sobre todo), workers | humano | Reglas que el output tiene que cumplir |
| `handoffs.jsonl` | orchestrator, humano | cada agente al terminar | Log append-only de qué hizo cada agente |
| `budget.json` | orchestrator | orchestrator | Tokens gastados acumulados (circuit breaker) |

## Convenciones

- **`handoffs.jsonl`** es append-only. Una línea = un handoff. JSON por línea.
- **`features.json`** se modifica solo desde `orchestrator.update_feature_status()`.
- **`validation.contract.md`** lo edita el humano. Los agentes lo leen, nunca lo escriben.

## Cómo correr

```bash
# Ver qué features hay pendientes
.venv/bin/python -m agents.orchestrator --list

# Correr el loop sobre una feature en dry-run (no toca código, solo planea)
.venv/bin/python -m agents.orchestrator --feature F-PREFLIGHT-001 --dry-run

# Correr de verdad (con budget cap en USD)
.venv/bin/python -m agents.orchestrator --feature F-PREFLIGHT-001 --budget 2.00

# Solo validar el último reporte sin construir nada
.venv/bin/python -m agents.validator --validate-last-report
```

## Filosofía

- **Cada agente es una función** que llama a Claude con un system prompt específico y un contexto acotado.
- **El shared state vive en disco** (estos archivos), no en memoria. Cualquier agente puede leerlo en cualquier momento.
- **Loop con max iterations + budget cap** para no quemar tokens.
- **Humano es circuit breaker**: el orchestrator pausa y pide aprobación en momentos críticos.
