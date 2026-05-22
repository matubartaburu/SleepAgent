"""
twilio_client.py — envio de mensajes de WhatsApp via Twilio.

Twilio sandbox no aplica la ventana de 24h de Meta cuando el destinatario
hizo el opt-in (mandando 'join xxx' al numero sandbox de Twilio). Ideal
para uso personal de Oscar — Mateo opt-in una vez y listo.

Para produccion (sin opt-in, multiples destinatarios) hay que migrar a
Twilio production con templates aprobados por Meta.

Códigos de error Twilio que vigilamos especialmente:
- 63015: channel could not deliver (sandbox session muerta o cliente bloqueó).
- 63016: outside 24h window — necesitarías template message.
- 63031: el destinatario no hizo opt-in al sandbox.
Cuando aparecen, queremos surfacearlos: el mensaje no llegó aunque la API
devolvió 201 inicialmente.
"""

import logging
import time

from twilio.rest import Client

from config import (
    MY_PHONE,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_WHATSAPP_FROM,
)

logger = logging.getLogger(__name__)

_twilio: Client | None = None


# Códigos que indican que el mensaje NO va a llegar (no es flap transitorio).
SANDBOX_DEAD_CODES = {63015, 63016, 63031}


class TwilioDeliveryError(RuntimeError):
    """El mensaje quedó en estado terminal de falla (no llega al usuario)."""

    def __init__(self, sid: str, status: str, error_code: int | None, error_message: str | None):
        self.sid = sid
        self.status = status
        self.error_code = error_code
        self.error_message = error_message
        sandbox_hint = (
            " — sandbox session expirada o sin opt-in. "
            "Mandá 'join <codigo>' al sandbox desde tu WhatsApp para reactivar."
            if error_code in SANDBOX_DEAD_CODES else ""
        )
        super().__init__(
            f"Twilio entrega fallida sid={sid} status={status} "
            f"code={error_code} msg={error_message!r}{sandbox_hint}"
        )


def _client() -> Client:
    global _twilio
    if _twilio is None:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
            raise RuntimeError(
                "Faltan TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN en .env"
            )
        _twilio = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio


def _poll_delivery(sid: str, timeout_s: float = 8.0, interval_s: float = 1.0) -> dict:
    """
    Espera hasta `timeout_s` para que el status pase de 'queued/sending' a un
    estado terminal ('sent', 'delivered', 'failed', 'undelivered'). Devuelve el
    último snapshot leído. No levanta excepciones por timeout — eso lo decide
    el caller mirando el dict.
    """
    deadline = time.monotonic() + timeout_s
    snapshot = {"status": "unknown", "error_code": None, "error_message": None}
    while time.monotonic() < deadline:
        m = _client().messages(sid).fetch()
        snapshot = {
            "sid": m.sid,
            "status": m.status,
            "error_code": m.error_code,
            "error_message": m.error_message,
        }
        if m.status in {"sent", "delivered", "failed", "undelivered"}:
            return snapshot
        time.sleep(interval_s)
    return snapshot


def get_recent_conversation(limit: int = 10) -> list[dict]:
    """
    Trae los últimos `limit` mensajes intercambiados con MY_PHONE, en orden
    cronológico (más viejo primero, así pasa derecho a Anthropic messages[]).
    Cada item: {role: 'user'|'assistant', content: str, ts: str, sid: str}.

    Se usa para dar memoria al answerer entre turnos: sin esto, cada pregunta
    de Mateo se interpreta sin contexto del intercambio previo.
    """
    if not MY_PHONE:
        return []
    to = MY_PHONE if MY_PHONE.startswith("whatsapp:") else f"whatsapp:{MY_PHONE}"
    client = _client()

    try:
        # Twilio messages.list devuelve por default desc por date_sent. Pedimos
        # más de lo necesario porque vienen mezclados in/out a/desde MY_PHONE.
        sent = client.messages.list(to=to, limit=limit)
        received = client.messages.list(from_=to, limit=limit)
    except Exception as exc:
        logger.warning("get_recent_conversation falló: %s", exc)
        return []

    items: list[dict] = []
    for m in sent:
        # 'outbound-api' = mensajes que mandamos nosotros (asistente).
        if m.body and m.status not in {"failed", "undelivered"}:
            items.append({
                "role": "assistant",
                "content": m.body,
                "ts": str(m.date_sent or m.date_created or ""),
                "sid": m.sid,
            })
    for m in received:
        # 'inbound' = mensajes que escribió Mateo.
        if m.body:
            items.append({
                "role": "user",
                "content": m.body,
                "ts": str(m.date_sent or m.date_created or ""),
                "sid": m.sid,
            })

    # Ordenamos cronológicamente y nos quedamos con los últimos `limit`.
    items.sort(key=lambda x: x["ts"])
    return items[-limit:]


def send_whatsapp_text(
    body: str,
    to: str | None = None,
    *,
    confirm_delivery: bool = True,
    confirm_timeout_s: float = 8.0,
) -> str:
    """
    Envia un mensaje a `to` (numero E.164 con `+`, ej '+598XXXXXXXX'). Si no
    se especifica `to`, usa MY_PHONE de .env. Devuelve el SID del mensaje.

    Si `confirm_delivery=True` (default), hace polling al status hasta que
    Twilio confirme. Si terminó en 'failed' o 'undelivered', levanta
    TwilioDeliveryError con el código exacto. Esto evita el bug de que
    `status=queued` daba ilusión de éxito mientras el mensaje moría.
    """
    if not TWILIO_WHATSAPP_FROM:
        raise RuntimeError("Falta TWILIO_WHATSAPP_FROM en .env")

    destination = to or MY_PHONE
    if not destination:
        raise RuntimeError("Falta destinatario (parametro `to` o MY_PHONE)")

    if not destination.startswith("whatsapp:"):
        destination = f"whatsapp:{destination}"

    msg = _client().messages.create(
        from_=TWILIO_WHATSAPP_FROM,
        to=destination,
        body=body,
    )
    logger.info("Twilio queued | sid=%s | status=%s | to=%s", msg.sid, msg.status, destination)

    if not confirm_delivery:
        return msg.sid

    snap = _poll_delivery(msg.sid, timeout_s=confirm_timeout_s)
    logger.info(
        "Twilio delivery | sid=%s | status=%s | code=%s | msg=%s",
        snap.get("sid"), snap.get("status"), snap.get("error_code"), snap.get("error_message"),
    )
    if snap.get("status") in {"failed", "undelivered"}:
        raise TwilioDeliveryError(
            sid=msg.sid,
            status=snap.get("status"),
            error_code=snap.get("error_code"),
            error_message=snap.get("error_message"),
        )
    return msg.sid
