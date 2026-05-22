"""
security/ — utilidades de seguridad: log filters, secret detection,
PII redaction.

Diseñado para que en OSCAR_ENV=production los logs no contengan:
- Números de teléfono completos
- Bodies completos de reportes (solo SID + char count)
- API keys / tokens / secrets
- Contenido textual de mensajes inbound (solo metadata)

En dev (OSCAR_ENV != "production") los logs van completos, como hoy.
"""
