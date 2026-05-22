"""
Sistema multi-agente de Oscar.

Cada agente es una función que llama a Claude con un system prompt específico
y un contexto acotado. El shared state vive en .agents/ (features.json,
handoffs.jsonl, validation.contract.md).

Modelos por rol:
- constructor:  Sonnet 4.6 (escribe código, balance velocidad/calidad)
- test_agent:   Sonnet 4.6
- validator:    Opus 4.7  (más estricto, asimetría con el generador)
- orchestrator: Opus 4.7  (decide y re-scopea)
- reporter:     Sonnet 4.6 (el que ya usa report.py)
"""
