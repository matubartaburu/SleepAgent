"""
config.py — carga variables de entorno y valida las críticas.

F1.0 (base): SUPABASE_URL, SUPABASE_SERVICE_KEY, INGEST_SECRET.
F1.5 (reporte): ANTHROPIC_API_KEY, TWILIO_*, MY_PHONE.
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Identidad del agente
AGENT_NAME = "Oscar"

# ── Supabase (F1.0) ─────────────────────────────────────────────────────────
SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# ── Webhook auth (F1.0) ─────────────────────────────────────────────────────
INGEST_SECRET = os.getenv("INGEST_SECRET", "")

# ── Anthropic (F1.5) ────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── Twilio (F1.5 — sandbox para uso personal) ───────────────────────────────
TWILIO_ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM  = os.getenv("TWILIO_WHATSAPP_FROM", "")

# ── Destinatario WhatsApp ───────────────────────────────────────────────────
MY_PHONE              = os.getenv("MY_PHONE", "")

# ── Reporte (F1.5) ──────────────────────────────────────────────────────────
TZ            = os.getenv("TZ", "America/Montevideo")
REPORT_HOUR   = int(os.getenv("REPORT_HOUR", "8"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "30"))


# Validamos solo lo que usa F1.0. F1.5 se valida cuando se enchufe.
_required_f1 = {
    "SUPABASE_URL":         SUPABASE_URL,
    "SUPABASE_SERVICE_KEY": SUPABASE_SERVICE_KEY,
    "INGEST_SECRET":        INGEST_SECRET,
}
_required_f15 = {
    "ANTHROPIC_API_KEY":    ANTHROPIC_API_KEY,
    "TWILIO_ACCOUNT_SID":   TWILIO_ACCOUNT_SID,
    "TWILIO_AUTH_TOKEN":    TWILIO_AUTH_TOKEN,
    "TWILIO_WHATSAPP_FROM": TWILIO_WHATSAPP_FROM,
    "MY_PHONE":             MY_PHONE,
}
_missing_f1  = [k for k, v in _required_f1.items()  if not v]
_missing_f15 = [k for k, v in _required_f15.items() if not v]
if _missing_f1:
    logger.warning("Variables faltantes (F1.0): %s", ", ".join(_missing_f1))
if _missing_f15:
    logger.warning("Variables faltantes (F1.5): %s", ", ".join(_missing_f15))
