"""
agents/preflight.py — preflight check NO-LLM antes del reporte diario.

Verifica que las piezas externas estén listas para que Oscar pueda generar
y mandar el reporte. Si algo falla, alerta a Mateo por WhatsApp en vez del
silencio del flujo normal.

Diseñado para correr en el scheduler ~5 min antes del reporte real.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date

from agents.base import handoff

log = logging.getLogger(__name__)


@dataclass
class PreflightResult:
    data_ok: bool = False
    twilio_ok: bool = False
    anthropic_ok: bool = False
    last_night_date: str | None = None
    issues: list[str] = field(default_factory=list)

    def all_ok(self) -> bool:
        return self.data_ok and self.twilio_ok and self.anthropic_ok


def _check_data() -> tuple[bool, str | None, list[str]]:
    try:
        from db import _client as _supabase
        res = (
            _supabase()
            .table("sleep_logs")
            .select("night_date")
            .order("night_date", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return False, None, ["sleep_logs vacía"]
        last = rows[0]["night_date"]
        today = date.today().isoformat()
        if last != today:
            return False, last, [f"última fila es de {last}, hoy es {today}"]
        return True, last, []
    except Exception as exc:
        return False, None, [f"db_error: {exc}"]


def _check_twilio() -> tuple[bool, list[str]]:
    """
    No alcanza con verificar que la API responde. La trampa típica es que
    la sesión del sandbox WhatsApp expiró: la API acepta el POST, devuelve
    SID `queued`, pero después marca `failed` con error 63015. Por eso
    miramos los últimos N mensajes salientes a MY_PHONE: si alguno terminó
    en failed/undelivered con código sandbox-dead, alertamos.
    """
    try:
        from twilio_client import _client, SANDBOX_DEAD_CODES
        from config import MY_PHONE
        client = _client()
        # Validamos auth tocando cuenta
        client.api.v2010.accounts.list(limit=1)
    except Exception as exc:
        return False, [f"twilio_auth_error: {exc}"]

    # Miramos SOLO el mensaje más reciente. Si fue sandbox-dead, alerta.
    # Si fue OK (sent/delivered/queued), asumimos que el canal está sano.
    # No scaneamos los últimos N porque mensajes viejos que fallaron
    # antes de un re-opt-in seguirían marcando false-positive eternamente.
    try:
        if not MY_PHONE:
            return True, []
        to = MY_PHONE if MY_PHONE.startswith("whatsapp:") else f"whatsapp:{MY_PHONE}"
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
    except Exception as exc:
        return True, [f"twilio_history_check_skipped: {exc}"]


def _check_anthropic() -> tuple[bool, list[str]]:
    try:
        from config import ANTHROPIC_API_KEY
        from anthropic import Anthropic
        if not ANTHROPIC_API_KEY:
            return False, ["ANTHROPIC_API_KEY vacía"]
        # No hacemos llamada para ahorrar tokens: solo verificamos init
        Anthropic(api_key=ANTHROPIC_API_KEY)
        return True, []
    except Exception as exc:
        return False, [f"anthropic_error: {exc}"]


def preflight_check() -> PreflightResult:
    res = PreflightResult()

    res.data_ok, res.last_night_date, issues = _check_data()
    res.issues += issues

    res.twilio_ok, issues = _check_twilio()
    res.issues += issues

    res.anthropic_ok, issues = _check_anthropic()
    res.issues += issues

    log.info("Preflight | data=%s twilio=%s anthropic=%s | last_night=%s | issues=%s",
             res.data_ok, res.twilio_ok, res.anthropic_ok,
             res.last_night_date, res.issues)
    handoff(from_agent="preflight", to_agent="orchestrator",
            feature_id=None, event="preflight",
            summary=f"all_ok={res.all_ok()} issues={len(res.issues)}",
            extra=asdict(res))
    return res


def send_preflight_alert(result: PreflightResult) -> str | None:
    """
    Si algo está mal, manda un WhatsApp corto a Mateo explicando qué pasa.
    Devuelve el SID o None si no había nada que alertar.
    """
    if result.all_ok():
        return None
    try:
        from twilio_client import send_whatsapp_text
    except Exception as exc:
        log.error("No puedo importar twilio para mandar alerta: %s", exc)
        return None

    lines = ["Oscar preflight 8:25 — algo falló:"]
    if not result.data_ok:
        if result.last_night_date:
            lines.append(f"- Sin data de anoche (última: {result.last_night_date}). HAE no mandó o Apple Health no finalizó.")
        else:
            lines.append("- DB vacía o no se pudo consultar.")
    if not result.twilio_ok:
        lines.append("- Twilio respondió raro (revisá sandbox / token).")
    if not result.anthropic_ok:
        lines.append("- Anthropic no responde (revisá key).")
    lines.append("\nRevisalo y corré /report/test cuando esté ok.")
    body = "\n".join(lines)

    try:
        sid = send_whatsapp_text(body=body)
        log.info("Alerta de preflight enviada | sid=%s", sid)
        return sid
    except Exception as exc:
        log.error("Falló envío de alerta de preflight: %s", exc)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    res = preflight_check()
    print(asdict(res))
    if not res.all_ok():
        send_preflight_alert(res)
