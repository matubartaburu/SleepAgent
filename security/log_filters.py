"""
security/log_filters.py — filters de logging que redactan info sensible
cuando OSCAR_ENV=production.

API pública:
- install_production_filters(): instala los filters en el root logger.
- redact(text): redacta texto manualmente (útil en mensajes específicos).

Diseño:
- Patrones regex sobre el `msg` y los `args` de cada LogRecord.
- Lista controlada de patrones — fácil de auditar.
- En dev (OSCAR_ENV != production) los filters quedan inactivos.
"""

from __future__ import annotations

import logging
import os
import re

# ── Patrones a redactar ──────────────────────────────────────────────────

# Tokens/secrets/keys. Conservador: match cualquier secuencia larga
# que arranque con prefijos conocidos.
_SECRET_PATTERNS = [
    # Anthropic
    (re.compile(r"\bsk-ant-[a-zA-Z0-9_\-]{20,}"),  "sk-ant-***REDACTED***"),
    # OpenAI
    (re.compile(r"\bsk-(?:proj-)?[a-zA-Z0-9_\-]{20,}"), "sk-***REDACTED***"),
    # Notion (legacy y v2)
    (re.compile(r"\b(?:secret|ntn)_[a-zA-Z0-9]{20,}"), "ntn_***REDACTED***"),
    # Twilio Account SID/Auth Token
    (re.compile(r"\bAC[a-f0-9]{32}\b"),            "AC***REDACTED***"),
    # Fly tokens
    (re.compile(r"\bFlyV1\s+[a-zA-Z0-9_\-]{20,}"), "FlyV1 ***REDACTED***"),
    # Supabase service keys (jwt)
    (re.compile(r"\beyJ[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{20,}"),
     "eyJ***REDACTED.JWT***"),
]

# Números de teléfono E.164 — preservamos prefijo país + redactamos suffix.
# +598 9 1483458 → +598 9*****58
_PHONE_PATTERN = re.compile(r"(\+\d{1,3})(\d{2,3})(\d+)(\d{2})")

# Mensajes que son específicamente bodies de reportes (los marcamos con tag).
# Cualquier log que tenga "preview=" o "body=" con texto largo lo truncamos.
_LONG_BODY_PATTERN = re.compile(r"((?:preview|body)\s*=\s*)['\"]?([^'\"]{30,})['\"]?")


def _redact_phone(match: re.Match) -> str:
    country = match.group(1)
    area = match.group(2)
    middle_len = len(match.group(3))
    last = match.group(4)
    return f"{country}{area}{'*' * middle_len}{last}"


def redact(text: str) -> str:
    """
    Redacta texto aplicando todos los patrones. Devuelve string seguro
    para loguear o exponer al usuario.

    Idempotente: aplicarlo dos veces no cambia el resultado.
    """
    if not text:
        return text
    out = text
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    out = _PHONE_PATTERN.sub(_redact_phone, out)
    # Truncar bodies largos a 40 chars + indicador.
    out = _LONG_BODY_PATTERN.sub(
        lambda m: f"{m.group(1)}'{m.group(2)[:40]}...({len(m.group(2))}chars)'",
        out,
    )
    return out


class ProductionRedactionFilter(logging.Filter):
    """
    Filter de logging que redacta el mensaje + args de cada LogRecord
    antes de que llegue a los handlers.

    No bloquea ningún log; solo lo limpia.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        try:
            # Redactamos el mensaje formateado completo
            msg = record.getMessage()
            redacted = redact(msg)
            if redacted != msg:
                # Reemplazamos msg + args para que no se vuelva a formatear con args sucios
                record.msg = redacted
                record.args = ()
        except Exception:
            # Nunca bloqueamos un log por un fallo del filter
            pass
        return True


_INSTALLED = False


def install_production_filters() -> bool:
    """
    Instala el filter en el root logger SI OSCAR_ENV=production.
    Devuelve True si quedó instalado, False si no aplica (dev).
    """
    global _INSTALLED
    if _INSTALLED:
        return True
    if os.getenv("OSCAR_ENV", "").lower() != "production":
        return False

    f = ProductionRedactionFilter()
    root = logging.getLogger()
    root.addFilter(f)
    # Algunos handlers ya enganchados se filtran a nivel logger,
    # pero también colgamos el filter en cada handler por las dudas.
    for handler in root.handlers:
        handler.addFilter(f)
    _INSTALLED = True
    logging.getLogger(__name__).info("Production log redaction filters installed")
    return True
