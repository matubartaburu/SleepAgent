---
title: Twilio WhatsApp sandbox session dies silently with error 63015
date: 2026-05-15
category: integration-issues
module: twilio_client
problem_type: integration_issue
component: service_object
symptoms:
  - "Twilio API returns 201 with status=queued, but message never arrives at WhatsApp"
  - "messages(sid).fetch() later shows status=failed with error_code=63015"
  - "No exception or alert at send time — silent delivery failure"
  - "Affects all outbound messages after sandbox opt-in window expires (~72h of recipient silence)"
root_cause: incomplete_setup
resolution_type: code_fix
severity: high
related_components: [background_job, assistant]
tags: [twilio, whatsapp, sandbox, delivery-confirmation, preflight, error-63015]
---

# Twilio WhatsApp sandbox session dies silently with error 63015

## Problem

El SDK de Twilio acepta el `POST /Messages` y devuelve `status=queued` con un SID válido, pero el mensaje nunca llega al WhatsApp del destinatario. La sesión del sandbox (que requiere opt-in inicial via `join <código>` desde el teléfono del usuario) expira a las ~72h sin tráfico entrante, y a partir de ahí todos los envíos quedan `failed` con `error_code=63015` de forma asíncrona. El código original no detectaba esto porque solo miraba el `status` inicial del POST.

## Symptoms

- `client.messages.create()` retorna inmediatamente con `status=queued`, HTTP 201, SID válido.
- Llamada posterior a `client.messages(sid).fetch()` muestra `status=failed`, `error_code=63015`, `error_message=None`.
- Cero excepciones al momento de enviar; el logger imprime "Twilio sent" como si todo OK.
- Mateo (el destinatario) nunca recibe el mensaje.
- El reporte diario de Oscar a las 08:30 falla silencioso día tras día hasta detectarlo.

## What Didn't Work

- **Confiar en `status=queued` como éxito**. Es solo el estado inicial: Twilio resuelve la entrega real de forma asíncrona y el status final aparece segundos después como `sent`, `delivered`, `failed` o `undelivered`. El POST inicial siempre devuelve `queued` mientras la auth sea válida.
- **Preflight escaneando los últimos 5 mensajes**. El primer intento del preflight buscaba CUALQUIER mensaje en los últimos 5 con `error_code` en `{63015, 63016, 63031}`. Resultado: falso positivo permanente. Una vez que un envío viejo falla, queda en la historia inmutable de Twilio; aunque después se haga el re-opt-in y los nuevos mensajes funcionen, el viejo error seguía gatillando la alerta.

## Solution

**Fix 1 — confirmar entrega post-envío** (`twilio_client.py`):

```python
SANDBOX_DEAD_CODES = {63015, 63016, 63031}

class TwilioDeliveryError(RuntimeError):
    """El mensaje quedó en estado terminal de falla (no llega al usuario)."""

    def __init__(self, sid, status, error_code, error_message):
        sandbox_hint = (
            " — sandbox session expirada o sin opt-in. "
            "Mandá 'join <codigo>' al sandbox desde tu WhatsApp para reactivar."
            if error_code in SANDBOX_DEAD_CODES else ""
        )
        super().__init__(
            f"Twilio entrega fallida sid={sid} status={status} "
            f"code={error_code} msg={error_message!r}{sandbox_hint}"
        )


def _poll_delivery(sid, timeout_s=8.0, interval_s=1.0):
    """Espera hasta `timeout_s` por el estado terminal de un envío."""
    deadline = time.monotonic() + timeout_s
    snapshot = {"status": "unknown", "error_code": None, "error_message": None}
    while time.monotonic() < deadline:
        m = _client().messages(sid).fetch()
        snapshot = {"sid": m.sid, "status": m.status,
                    "error_code": m.error_code, "error_message": m.error_message}
        if m.status in {"sent", "delivered", "failed", "undelivered"}:
            return snapshot
        time.sleep(interval_s)
    return snapshot


def send_whatsapp_text(body, to=None, *, confirm_delivery=True, confirm_timeout_s=8.0):
    # ... build destination, validate config ...
    msg = _client().messages.create(from_=TWILIO_WHATSAPP_FROM, to=destination, body=body)
    if not confirm_delivery:
        return msg.sid
    snap = _poll_delivery(msg.sid, timeout_s=confirm_timeout_s)
    if snap.get("status") in {"failed", "undelivered"}:
        raise TwilioDeliveryError(
            sid=msg.sid, status=snap["status"],
            error_code=snap["error_code"], error_message=snap["error_message"],
        )
    return msg.sid
```

**Fix 2 — preflight mira SOLO el último mensaje** (`agents/preflight.py`):

```python
recent = client.messages.list(to=to, limit=1)
if recent:
    m = recent[0]
    if m.error_code and int(m.error_code) in SANDBOX_DEAD_CODES:
        return False, [
            f"twilio_sandbox_dead: último envío sid={m.sid} status={m.status} "
            f"code={m.error_code}. Mandá 'join <codigo>' al sandbox desde "
            f"tu WhatsApp para reactivar."
        ]
return True, []
```

No escanear los últimos N — un fallo viejo seguido de un envío exitoso reciente NO debe alarmar. El último mensaje refleja el estado actual del canal.

**Fix 3 — paso manual de recuperación** (lo único que el código no puede hacer solo):

Desde el WhatsApp de Mateo, mandar al número del sandbox de Twilio (`+1 415 523 8886`):

```
join <palabra-clave-del-sandbox>
```

La palabra clave está en https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn. Una vez recibida la confirmación "joined", la sesión queda viva ~72h.

## Why This Works

- **`status=queued` es asincronía, no éxito.** Twilio acepta el POST y devuelve un SID con el estado de la cola. La entrega real al endpoint del operador (Meta/WhatsApp) ocurre eventually. Estados terminales (`sent`, `delivered`, `failed`, `undelivered`) aparecen segundos después y solo entonces se sabe el resultado real.
- **Error 63015 es terminal por session-dead, no flap.** El sandbox de Twilio exige que el destinatario haya enviado `join <code>` desde su WhatsApp en las últimas ~72h. Una vez vencida la ventana, TODOS los outbounds quedan `failed/63015` hasta el próximo opt-in. No reintentar tiene sentido — la causa está fuera del control del código.
- **Solo el último mensaje refleja la salud actual del canal.** La historia de Twilio es inmutable; mirar los últimos N produce falsos positivos eternos. Mirar solo `limit=1` da una señal verdadera de "puede el canal entregar ahora".

## Prevention

- **Nunca asumas `queued` = éxito en APIs async**. Pollear hasta estado terminal o registrar webhooks de status callback. Aplica a Twilio, SES, SendGrid, Stripe payouts — cualquier endpoint que devuelva un SID/ID de tracking.
- **Codes de Twilio a vigilar específicamente**:
  - `63015`: delivery channel issue (sandbox dead, recipient blocked, etc.).
  - `63016`: outside 24h Meta window — requiere template message.
  - `63031`: recipient hasn't opted in to sandbox.
- **Test que simule el flow async completo** (`tests/test_twilio_client.py`):
  ```python
  def test_send_raises_on_failed_with_sandbox_dead_code():
      created = _fake_message(sid="SM2", status="queued")
      final = _fake_message(sid="SM2", status="failed", error_code=63015)
      fake_client.messages.create.return_value = created
      fake_client.messages.return_value.fetch.return_value = final
      with patch.object(twilio_client, "_client", return_value=fake_client):
          with pytest.raises(twilio_client.TwilioDeliveryError) as exc_info:
              twilio_client.send_whatsapp_text("hola", to="+59891000000",
                                                confirm_timeout_s=1.0)
      assert exc_info.value.error_code == 63015
      assert "sandbox" in str(exc_info.value).lower()
  ```
- **Preflight debe chequear estado del canal, no solo auth.** Un cliente que autentica OK puede tener el sandbox muerto. El último mensaje cuenta la verdad del canal.
- **Para producción real** (no sandbox), migrar a Twilio production con templates aprobados por Meta. La limitación de 72h del sandbox desaparece con templates aprobados.
- **Logging que loguee `error_code` + `status` final**, no solo el SID inicial. El `error_code` es el único campo accionable para diagnóstico.

## Related Issues

- No hay otros docs en `docs/solutions/` todavía — este es el primer doc del proyecto.
- Twilio docs sobre error codes: https://www.twilio.com/docs/api/errors/63015
- Tracking: 2026-05-15 — primera ocurrencia detectada y resuelta en la misma sesión.
