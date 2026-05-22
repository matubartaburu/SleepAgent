"""
dev_force_report.py — smoke test: genera y manda el reporte usando la noche
mas reciente en sleep_logs, sin importar que sea de hoy.

Solo para verificar que Anthropic + Twilio andan end-to-end. NO usar en
producción; el flujo normal (regla "sin data de anoche, silencio") sigue vivo
en report.py.

Uso:
    .venv/bin/python dev_force_report.py
"""

import logging
import sys

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY
from db import _client as _supabase
from report import SONNET_MODEL, SYSTEM_PROMPT, _build_user_message
from twilio_client import send_whatsapp_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("force_report")


def force_report() -> dict:
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
        return {"sent": False, "reason": "no_rows_in_db"}

    last_night = rows[0]
    log.info("Usando night_date=%s (forzado, no es hoy)", last_night["night_date"])

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
    ctx = {"last_night": last_night, "baseline": baseline, "n_baseline": len(rows)}
    user_msg = _build_user_message(ctx)

    if not ANTHROPIC_API_KEY:
        return {"sent": False, "reason": "missing_anthropic_key"}

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text.strip()
    log.info("Reporte generado (%d chars)", len(text))

    # Prefijo claro para que sepas que es smoke test, no el reporte real
    body = f"[smoke test — noche {last_night['night_date']}]\n\n{text}"
    try:
        sid = send_whatsapp_text(body=body)
    except Exception as exc:
        log.error("Falló el envío Twilio: %s", exc)
        return {"sent": False, "reason": "twilio_delivery_failed", "error": str(exc)}
    log.info("Twilio SID confirmado: %s", sid)
    return {
        "sent": True,
        "sid": sid,
        "night_used": last_night["night_date"],
        "chars": len(body),
    }


if __name__ == "__main__":
    try:
        result = force_report()
        print(result)
        sys.exit(0 if result.get("sent") else 1)
    except Exception as exc:
        log.exception("Fallo el force_report: %s", exc)
        sys.exit(2)
