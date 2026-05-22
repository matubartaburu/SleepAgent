"""
security/production_guards.py — guardas que se aplican solo cuando
OSCAR_ENV=production.

Responsabilidades:
- Esconder /docs y /redoc de FastAPI (información de la API pública = malo).
- Fail-fast si faltan secrets críticos en producción.
- Verificar que INGEST_SECRET no sea un valor de ejemplo.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def is_production() -> bool:
    return os.getenv("OSCAR_ENV", "").lower() == "production"


def fail_if_missing_critical_secrets() -> None:
    """
    En production, no arrancamos si faltan secrets críticos. Mejor un fail
    ruidoso temprano que un sistema corriendo a medias.
    """
    if not is_production():
        return

    critical = [
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "INGEST_SECRET",
        "ANTHROPIC_API_KEY",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_WHATSAPP_FROM",
        "MY_PHONE",
    ]
    missing = [k for k in critical if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            f"OSCAR_ENV=production pero faltan vars críticas: {missing}. "
            "Setealas con `fly secrets set ...`."
        )

    # Guardrail contra valores de ejemplo / dev.
    insecure = []
    secret = os.getenv("INGEST_SECRET", "")
    if len(secret) < 20:
        insecure.append("INGEST_SECRET es demasiado corto (mín 20 chars)")
    if secret.lower() in {"test", "test-secret", "secret", "changeme", "password"}:
        insecure.append(f"INGEST_SECRET tiene valor inseguro ({secret!r})")
    if insecure:
        raise RuntimeError(
            "OSCAR_ENV=production con configuración insegura: " + "; ".join(insecure)
        )

    log.info("Production secrets check passed (%d critical vars set)", len(critical))


def fastapi_docs_kwargs() -> dict:
    """
    En production, deshabilita /docs, /redoc, y /openapi.json para no
    exponer la superficie de la API.
    """
    if is_production():
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {}
