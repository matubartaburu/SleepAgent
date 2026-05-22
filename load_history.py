"""
load_history.py — backfill puntual desde un export manual de Health Auto Export.

Mismo parser que el webhook, pero sin HTTP. Pensado para cargar de un saque un
rango largo (ej: ultimos 3 meses) que exportes a mano desde la app.

Idempotente: las noches que ya esten en la DB se actualizan, no se duplican.

Uso:
    .venv/bin/python load_history.py data/historico_3m.json
"""

import json
import os
import sys

from db import upsert_sleep_logs
from main import parse_health_auto_export


def main(path: str) -> None:
    path = os.path.abspath(path)
    if not os.path.exists(path):
        print(f"No existe: {path}")
        sys.exit(1)

    size_mb = os.path.getsize(path) / 1024 / 1024
    print(f"Leyendo {path} ({size_mb:.1f} MB)...")
    with open(path) as f:
        payload = json.load(f)

    rows = parse_health_auto_export(payload)
    if not rows:
        print("Payload sin sleep_analysis, no hay noches para guardar.")
        return

    print(f"Parser produjo {len(rows)} noches. Subiendo a Supabase...")
    saved = upsert_sleep_logs(rows)

    nights = sorted(r["night_date"] for r in saved)
    print(f"OK: {len(saved)} noches guardadas/actualizadas.")
    print(f"Rango: {nights[0]} -> {nights[-1]}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python load_history.py <ruta-al-json>")
        sys.exit(1)
    main(sys.argv[1])
