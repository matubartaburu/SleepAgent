"""
SleepAgent — servidor FastAPI (Oscar).

F1.0:
  · POST /sleep   webhook de Health Auto Export. Parsea y guarda en sleep_logs.
  · GET  /health  status basico.

Auth del webhook: header `X-Ingest-Secret` con el valor de INGEST_SECRET.
"""

import asyncio
import logging
from datetime import date, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Header, HTTPException, Request

from config import AGENT_NAME, INGEST_SECRET, REPORT_HOUR, REPORT_MINUTE, TZ
from db import upsert_sleep_logs
from report import send_daily_report, send_weekly_report, send_monthly_report
from security.log_filters import install_production_filters
from security.production_guards import (
    fail_if_missing_critical_secrets,
    fastapi_docs_kwargs,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
# En production: instala filters que redactan phone numbers, secrets,
# y bodies completos en los logs.
install_production_filters()
# En production: fail-fast si faltan secrets críticos o tienen valores
# de ejemplo/inseguros.
fail_if_missing_critical_secrets()

logger = logging.getLogger(__name__)


app = FastAPI(
    title=f"SleepAgent — {AGENT_NAME}",
    description="Agente personal de sueño con datos de Apple Watch",
    version="0.1.0",
    # En production: oculta /docs, /redoc, /openapi.json
    **fastapi_docs_kwargs(),
)


# ── Parser de Health Auto Export ────────────────────────────────────────────

# Formato de timestamps que manda HAE: "2026-05-01 02:13:06 -0300"
_HAE_DATETIME_FMT = "%Y-%m-%d %H:%M:%S %z"


def _parse_dt(s: str) -> datetime:
    return datetime.strptime(s, _HAE_DATETIME_FMT)


def _to_pg_ts(s: str | None) -> str | None:
    """Convierte el formato HAE a ISO 8601 que Postgres acepta como TIMESTAMPTZ."""
    if not s:
        return None
    try:
        return _parse_dt(s).isoformat()
    except (ValueError, TypeError):
        return None


def _hours_to_min(hours) -> int | None:
    if hours is None:
        return None
    try:
        return int(round(float(hours) * 60))
    except (TypeError, ValueError):
        return None


def _num(x) -> float | None:
    if x is None:
        return None
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return None


def _index_by_date(metric: dict | None) -> dict[date, dict]:
    """Indexa los items de una metric por la fecha del campo `date`."""
    out: dict[date, dict] = {}
    if not metric:
        return out
    for item in metric.get("data") or []:
        try:
            out[_parse_dt(item["date"]).date()] = item
        except (KeyError, ValueError, TypeError):
            continue
    return out


def parse_health_auto_export(payload: dict) -> list[dict]:
    """
    Convierte el payload de Health Auto Export en filas listas para upsert
    en `sleep_logs`. Una fila por `night_date`.

    Reglas:
      - `night_date` = el campo `date` del item de `sleep_analysis`
        (Apple lo asocia al día del despertar).
      - Si Apple manda VARIAS sleep_analysis items para la misma night_date
        (típico cuando hay un despertar nocturno y luego volver a dormir),
        las COMBINAMOS:
          · sleep_start = el más temprano de todos
          · sleep_end   = el más tardío
          · total/rem/core/deep/awake = suma de todos
          · in_bed_minutes = max(inBedEnd) - min(inBedStart)
      - Las duraciones del JSON vienen en horas con decimales -> a minutos.
      - HRV / resting HR / heart rate / respiratory rate los joineamos por
        fecha; si no hay match, queda NULL.
    """
    metrics = (payload.get("data") or {}).get("metrics") or []
    by_name = {m.get("name"): m for m in metrics if isinstance(m, dict)}

    sleep = by_name.get("sleep_analysis")
    if not sleep or not sleep.get("data"):
        logger.info("Payload sin sleep_analysis, no hay nada para guardar")
        return []

    hrv_idx = _index_by_date(by_name.get("heart_rate_variability"))
    rhr_idx = _index_by_date(by_name.get("resting_heart_rate"))
    hr_idx  = _index_by_date(by_name.get("heart_rate"))
    rr_idx  = _index_by_date(by_name.get("respiratory_rate"))

    # ── Paso 1: agrupar sleep_analysis items por night_date ────────────────
    sessions_by_night: dict[date, list[dict]] = {}
    for s in sleep["data"]:
        try:
            night = _parse_dt(s["date"]).date()
        except (KeyError, ValueError, TypeError):
            logger.warning("sleep_analysis sin `date` valido: %r", s)
            continue
        sessions_by_night.setdefault(night, []).append(s)

    # ── Paso 2: combinar las sesiones de cada noche en una fila ────────────
    rows: list[dict] = []
    for night, sessions in sessions_by_night.items():
        combined = _combine_sleep_sessions(sessions)
        hrv = hrv_idx.get(night) or {}
        rhr = rhr_idx.get(night) or {}
        hr  = hr_idx.get(night)  or {}
        rr  = rr_idx.get(night)  or {}

        rows.append({
            "night_date":            night.isoformat(),
            "source":                "webhook",
            "sleep_start":           combined["sleep_start"],
            "sleep_end":             combined["sleep_end"],
            "in_bed_start":          combined["in_bed_start"],
            "in_bed_end":            combined["in_bed_end"],
            "total_sleep_minutes":   combined["total_sleep_minutes"],
            "in_bed_minutes":        combined["in_bed_minutes"],
            "rem_minutes":           combined["rem_minutes"],
            "core_minutes":          combined["core_minutes"],
            "deep_minutes":          combined["deep_minutes"],
            "awake_minutes":         combined["awake_minutes"],
            "hrv_sdnn_ms":           _num(hrv.get("qty")),
            "resting_hr_bpm":        _num(rhr.get("qty")),
            "avg_hr_bpm":            _num(hr.get("Avg")),
            "min_hr_bpm":            _num(hr.get("Min")),
            "max_hr_bpm":            _num(hr.get("Max")),
            "respiratory_rate_brpm": _num(rr.get("qty")),
            "raw_payload": {
                "sessions":         sessions,
                "n_sessions":       len(sessions),
                "hrv":              hrv or None,
                "resting_hr":       rhr or None,
                "heart_rate":       hr  or None,
                "respiratory_rate": rr  or None,
            },
        })

    return rows


def _combine_sleep_sessions(sessions: list[dict]) -> dict:
    """
    Combina varios sleep_analysis items (de Apple) en uno solo.

    Usado cuando vos te despertás durante la noche y Apple registra 2+
    sesiones separadas con la misma night_date.
    """
    # Convertimos timestamps a datetimes para poder comparar.
    def _parse(s_field):
        return _parse_dt(s_field) if s_field else None

    sleep_starts = [_parse(s.get("sleepStart")) for s in sessions]
    sleep_ends   = [_parse(s.get("sleepEnd"))   for s in sessions]
    ib_starts    = [_parse(s.get("inBedStart")) for s in sessions]
    ib_ends      = [_parse(s.get("inBedEnd"))   for s in sessions]

    # Min/max ignorando None.
    earliest_sleep_start = min((t for t in sleep_starts if t), default=None)
    latest_sleep_end     = max((t for t in sleep_ends if t),   default=None)
    earliest_ib_start    = min((t for t in ib_starts if t),    default=None)
    latest_ib_end        = max((t for t in ib_ends if t),      default=None)

    # Sumamos las duraciones (no la diferencia entre extremos, que incluiría
    # los minutos despierto entre sesiones).
    def _sum_hours(field):
        total = 0.0
        any_value = False
        for s in sessions:
            v = s.get(field)
            if v is None:
                continue
            try:
                total += float(v)
                any_value = True
            except (TypeError, ValueError):
                continue
        return total if any_value else None

    total_sleep_h = _sum_hours("totalSleep")
    rem_h         = _sum_hours("rem")
    core_h        = _sum_hours("core")
    deep_h        = _sum_hours("deep")
    awake_h       = _sum_hours("awake")

    in_bed_min = None
    if earliest_ib_start and latest_ib_end:
        try:
            in_bed_min = int(round(
                (latest_ib_end - earliest_ib_start).total_seconds() / 60
            ))
        except (TypeError, ValueError):
            pass

    return {
        "sleep_start":         earliest_sleep_start.isoformat() if earliest_sleep_start else None,
        "sleep_end":           latest_sleep_end.isoformat()     if latest_sleep_end     else None,
        "in_bed_start":        earliest_ib_start.isoformat()    if earliest_ib_start    else None,
        "in_bed_end":          latest_ib_end.isoformat()        if latest_ib_end        else None,
        "total_sleep_minutes": _hours_to_min(total_sleep_h),
        "in_bed_minutes":      in_bed_min,
        "rem_minutes":         _hours_to_min(rem_h),
        "core_minutes":        _hours_to_min(core_h),
        "deep_minutes":        _hours_to_min(deep_h),
        "awake_minutes":       _hours_to_min(awake_h),
    }


# ── Routes ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "agent": AGENT_NAME, "phase": "F1.0"}


@app.post("/sleep")
async def ingest_sleep(
    request: Request,
    x_ingest_secret: str | None = Header(default=None),
):
    if not INGEST_SECRET or x_ingest_secret != INGEST_SECRET:
        logger.warning("Intento no autorizado a /sleep")
        raise HTTPException(status_code=401, detail="unauthorized")

    payload = await request.json()
    rows = parse_health_auto_export(payload)

    # Workouts (Apple Watch) en el mismo payload — opcional, no rompe si falla.
    workout_summary = await asyncio.to_thread(_ingest_apple_workouts, payload)
    logger.info("Payload recibido | nights=%d apple_workouts=%s",
                len(rows), workout_summary)

    if not rows:
        return {"status": "ok", "nights": 0, "night_dates": [],
                "apple_workouts": workout_summary}

    # ── Filtro defensivo: bloquear "downgrade" de filas existentes ──────
    # Si una noche ya tiene sleep>6h en DB y este POST viene con <70% de eso,
    # asumimos que es una siesta o data parcial — NO sobrescribir.
    rows, rejected = _filter_suspicious_overrides(rows)
    if rejected:
        logger.warning("Rechazadas %d filas sospechosas (downgrade): %s",
                       len(rejected), [r["night_date"] for r in rejected])

    if not rows:
        return {"status": "ok", "nights": 0, "night_dates": [],
                "rejected": [r["night_date"] for r in rejected],
                "apple_workouts": workout_summary}

    try:
        saved = upsert_sleep_logs(rows)
    except Exception as exc:
        logger.exception("Error guardando sleep_logs: %s", exc)
        raise HTTPException(status_code=500, detail="db_error")

    return {
        "status": "ok",
        "nights": len(saved),
        "night_dates": [r["night_date"] for r in saved],
        "rejected": [r["night_date"] for r in rejected],
        "apple_workouts": workout_summary,
    }


# ── Defensa anti-downgrade ───────────────────────────────────────────────

# Si la fila existente tiene >= 6h de sueño, no permitimos que un POST
# nuevo la sobrescriba con menos del 70% del valor. Apple Health a veces
# reporta siestas o fragmentos con night_date del día principal.
_DOWNGRADE_MIN_EXISTING_MINUTES = 360   # 6h
_DOWNGRADE_RATIO_THRESHOLD = 0.70


def _filter_suspicious_overrides(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Separa filas "sospechosas" (que reducirían >30% el sueño existente) de
    filas legítimas. Devuelve (legítimas, rechazadas).
    """
    from db import _client as _supabase

    legit: list[dict] = []
    rejected: list[dict] = []
    for r in rows:
        new_total = r.get("total_sleep_minutes")
        # Si la nueva fila no tiene total_sleep, no es candidata a downgrade
        if new_total is None:
            legit.append(r)
            continue
        try:
            existing = (
                _supabase()
                .table("sleep_logs")
                .select("total_sleep_minutes")
                .eq("night_date", r["night_date"])
                .eq("source", r["source"])
                .limit(1)
                .execute()
            ).data
        except Exception as exc:
            logger.warning("No pude verificar downgrade para %s: %s",
                           r["night_date"], exc)
            legit.append(r)
            continue
        if not existing:
            legit.append(r)
            continue
        old_total = existing[0].get("total_sleep_minutes") or 0
        # Solo aplica la defensa si la fila existente tiene un sueño "completo"
        if old_total < _DOWNGRADE_MIN_EXISTING_MINUTES:
            legit.append(r)
            continue
        ratio = new_total / max(old_total, 1)
        if ratio < _DOWNGRADE_RATIO_THRESHOLD:
            logger.warning(
                "Downgrade rechazado | night=%s existing=%dmin new=%dmin (ratio=%.2f)",
                r["night_date"], old_total, new_total, ratio,
            )
            rejected.append(r)
        else:
            legit.append(r)
    return legit, rejected


def _ingest_apple_workouts(payload: dict) -> dict:
    """
    Procesa workouts del Apple Watch en el mismo payload. Tolerante: si
    Notion falla o no hay workouts, no rompe el ingest de sleep.
    """
    try:
        from hae_workouts_parser import parse_workouts
        from agents.workout.workout_logger import log_apple_cardio_batch
        workouts = parse_workouts(payload)
        if not workouts:
            return {"parsed": 0, "logged": 0}
        result = log_apple_cardio_batch(workouts)
        return {
            "parsed": len(workouts),
            "logged": result.get("logged", 0),
            "skipped": result.get("skipped", 0),
        }
    except Exception as exc:
        logger.warning("Apple workouts ingestion falló: %s", exc)
        return {"parsed": 0, "logged": 0, "error": str(exc)[:80]}


# ── Scheduler del reporte diario ─────────────────────────────────────────────

_scheduler: AsyncIOScheduler | None = None


def _run_daily_report():
    """Wrapper sync: APScheduler lo invoca, send_daily_report ya es sync."""
    try:
        result = send_daily_report()
        logger.info(
            "Reporte diario | %s",
            result.get("reason") or f"sent sid={result.get('sid')}",
        )
    except Exception:
        logger.exception("Error en el reporte diario")


def _run_preflight():
    """Preflight 5 min antes del reporte. Alerta si algo no está listo."""
    try:
        from agents.preflight import preflight_check, send_preflight_alert
        result = preflight_check()
        if not result.all_ok():
            send_preflight_alert(result)
            logger.warning("Preflight FAIL | issues=%s", result.issues)
        else:
            logger.info("Preflight OK | last_night=%s", result.last_night_date)
    except Exception:
        logger.exception("Error en preflight")


def _run_weekly_report():
    try:
        result = send_weekly_report()
        logger.info("Reporte semanal | %s",
                    result.get("reason") or f"sent sid={result.get('sid')}")
    except Exception:
        logger.exception("Error en reporte semanal")


def _run_monthly_report():
    try:
        result = send_monthly_report()
        logger.info("Reporte mensual | %s",
                    result.get("reason") or f"sent sid={result.get('sid')}")
    except Exception:
        logger.exception("Error en reporte mensual")


@app.on_event("startup")
async def _start_scheduler():
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone=TZ)

    # Preflight 5 min antes del reporte (avisa si HAE / Twilio / Anthropic fallan)
    preflight_minute = (REPORT_MINUTE - 5) % 60
    preflight_hour = REPORT_HOUR - (1 if REPORT_MINUTE < 5 else 0)
    _scheduler.add_job(
        _run_preflight,
        CronTrigger(hour=preflight_hour, minute=preflight_minute),
        id="preflight",
        replace_existing=True,
        misfire_grace_time=4 * 3600,
        coalesce=True,
    )

    _scheduler.add_job(
        _run_daily_report,
        CronTrigger(hour=REPORT_HOUR, minute=REPORT_MINUTE),
        id="daily_report",
        replace_existing=True,
        # Si la Mac estuvo dormida y el cron se atraso, igual disparamos
        # cuando despierta — siempre y cuando sea dentro de las 4 horas
        # siguientes al horario programado.
        misfire_grace_time=4 * 3600,
        coalesce=True,
    )

    # Reporte semanal: domingos 09:30
    _scheduler.add_job(
        _run_weekly_report,
        CronTrigger(day_of_week="sun", hour=9, minute=30),
        id="weekly_report",
        replace_existing=True,
        misfire_grace_time=4 * 3600,
        coalesce=True,
    )

    # Reporte mensual: día 1 a las 10:30
    _scheduler.add_job(
        _run_monthly_report,
        CronTrigger(day=1, hour=10, minute=30),
        id="monthly_report",
        replace_existing=True,
        misfire_grace_time=12 * 3600,
        coalesce=True,
    )

    _scheduler.start()
    logger.info(
        "Scheduler arrancado | %s preflight %02d:%02d → daily %02d:%02d → weekly Sun 09:30 → monthly day-1 10:30",
        TZ, preflight_hour, preflight_minute, REPORT_HOUR, REPORT_MINUTE,
    )


@app.on_event("shutdown")
async def _stop_scheduler():
    if _scheduler:
        _scheduler.shutdown(wait=False)


# ── Endpoint para disparar el reporte a demanda ──────────────────────────────

@app.post("/report/test")
async def report_test(x_ingest_secret: str | None = Header(default=None)):
    if not INGEST_SECRET or x_ingest_secret != INGEST_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    result = await asyncio.to_thread(send_daily_report)
    return result


@app.post("/report/test-weekly")
async def report_test_weekly(x_ingest_secret: str | None = Header(default=None)):
    if not INGEST_SECRET or x_ingest_secret != INGEST_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    result = await asyncio.to_thread(send_weekly_report)
    return result


@app.post("/report/test-monthly")
async def report_test_monthly(x_ingest_secret: str | None = Header(default=None)):
    if not INGEST_SECRET or x_ingest_secret != INGEST_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    result = await asyncio.to_thread(send_monthly_report)
    return result


# ── Webhook inbound de WhatsApp (Twilio) ────────────────────────────────────
# Twilio sandbox manda POSTs form-urlencoded cuando Mateo escribe al número.
# Campos principales: From (whatsapp:+...), Body, MessageSid.
# Validamos firma con TwilioRequestValidator si TWILIO_AUTH_TOKEN está set.

@app.post("/whatsapp/inbound")
async def whatsapp_inbound(request: Request):
    """
    Webhook inbound de Twilio.

    Twilio timeout = 15s. Audios largos + Whisper + Sonnet + Notion pueden
    pasar de eso. Si nos tardamos, Twilio REINTENTA y procesamos en duplicado.
    Por eso:
    1. Validamos firma + sender al toque (rápido).
    2. Si está OK, lanzamos el procesamiento como TAREA EN BACKGROUND
       y respondemos 200 inmediato.
    3. La respuesta a Mateo llega como mensaje outbound cuando el procesamiento
       termina, NO como respuesta al webhook.
    """
    from twilio.request_validator import RequestValidator
    from config import TWILIO_AUTH_TOKEN, MY_PHONE

    form = await request.form()
    params = {k: form[k] for k in form.keys()}
    signature = request.headers.get("X-Twilio-Signature", "")

    # Reconstrucción del URL público para validar firma Twilio.
    # Fly (y cualquier reverse proxy) termina TLS al borde y forwarda HTTP
    # interno al app. `request.url` ve `http://...` pero Twilio firmó
    # `https://...`. Usamos X-Forwarded-Proto / Host para reconstruir el URL
    # que Twilio firmó. Fallback a request.url si los headers no están.
    fwd_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    fwd_host  = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    path = request.url.path
    query = request.url.query
    public_url = f"{fwd_proto}://{fwd_host}{path}"
    if query:
        public_url += f"?{query}"

    # Validación de firma — Twilio firma el request con TWILIO_AUTH_TOKEN.
    # Si no podemos validar (token vacío o firma mal), rechazamos.
    if TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        # Intentamos primero con el URL público reconstruido (caso normal en Fly).
        # Si falla, intentamos con el URL crudo (caso local sin proxy).
        valid = (validator.validate(public_url, params, signature)
                 or validator.validate(str(request.url), params, signature))
        if not valid:
            logger.warning("Inbound WhatsApp con firma inválida | url_intentado=%s",
                            public_url)
            raise HTTPException(status_code=403, detail="invalid_signature")

    sender = params.get("From", "")
    body = (params.get("Body", "") or "").strip()
    message_sid = params.get("MessageSid", "")
    num_media = int(params.get("NumMedia", "0") or 0)
    media_url = params.get("MediaUrl0", "") if num_media > 0 else ""
    media_ct = params.get("MediaContentType0", "") if num_media > 0 else ""
    logger.info("Inbound WhatsApp | from=%s body=%r media=%d ct=%s",
                sender, body[:80], num_media, media_ct)

    # Solo aceptamos respuestas de Mateo (MY_PHONE).
    expected = MY_PHONE if MY_PHONE.startswith("whatsapp:") else f"whatsapp:{MY_PHONE}"
    if sender != expected:
        logger.warning("Inbound de remitente desconocido: %s", sender)
        return {"status": "ignored", "reason": "unknown_sender"}

    # Dispatch en background, respondemos 200 inmediato a Twilio.
    asyncio.create_task(_process_inbound_async(
        body=body, message_sid=message_sid,
        media_url=media_url, media_ct=media_ct,
    ))
    return {"status": "accepted", "processing": "background"}


async def _process_inbound_async(*, body: str, message_sid: str,
                                  media_url: str, media_ct: str) -> None:
    """
    Procesa el inbound (transcripción + ruteo + respuesta) en background.
    La respuesta a Mateo llega como mensaje OUTBOUND al final.

    Esta función NO debe levantar excepciones que se propaguen — todo
    error termina con un log y, si hay forma, un mensaje al user explicando.
    """
    from twilio_client import send_whatsapp_text as _send

    try:
        # ── 1) Si hay audio adjunto, transcribir ────────────────────────
        if media_url and media_ct.startswith("audio"):
            try:
                from agents.workout.audio_ingester import transcribe_from_twilio_url
                transcription = await asyncio.to_thread(transcribe_from_twilio_url, media_url)
                body = (transcription.text or "").strip()
                logger.info("Audio transcrito (%.1fs, %d chars, cost ~$%.4f)",
                            transcription.duration_seconds, len(body),
                            transcription.cost_usd)
            except Exception:
                logger.exception("Whisper falló transcribiendo el audio")
                await asyncio.to_thread(_send,
                    "No pude transcribir el audio. Probá de nuevo o escribime el mensaje.")
                return

        if not body:
            logger.info("Inbound sin body útil, ignoro")
            return

        # ── 2) ¿Hay nota abierta de Oscar? → tagger ─────────────────────
        from db import get_open_note, update_note_answer
        from agents.tagger import tag_answer_haiku
        note = get_open_note()
        if note:
            tagger_result = await asyncio.to_thread(tag_answer_haiku, body)
            update_note_answer(
                note["id"],
                answer=body,
                tags=tagger_result.tags,
                tagger_raw={
                    "confidence": tagger_result.confidence,
                    "notes": tagger_result.notes,
                },
            )
            logger.info("tagged: note=%s tags=%s", note["id"], tagger_result.tags)
            return

        # ── 3) Workout orchestrator ─────────────────────────────────────
        try:
            from agents.workout.orchestrator import handle_message as workout_handle
            workout_result = await asyncio.to_thread(workout_handle, body,
                                                       voice_note_sid=message_sid)
            if workout_result.handled_by_workout and workout_result.reply_text:
                await asyncio.to_thread(_send, workout_result.reply_text)
                logger.info("workout_handled: intent=%s", workout_result.intent)
                return
        except Exception:
            logger.exception("Workout orchestrator falló; sigo con answerer")

        # ── 4) Fallback: answerer conversacional de sleep ───────────────
        try:
            from agents.answerer import answer_question
            result = await asyncio.to_thread(answer_question, body)
            await asyncio.to_thread(_send, result.text)
            logger.info("answered: %d chars %d nights", result.chars, result.n_nights_used)
        except Exception:
            logger.exception("Falló answerer; respondo fallback")
            await asyncio.to_thread(_send,
                "Algo salió mal procesando tu mensaje. Probá de nuevo en un rato.")
    except Exception:
        logger.exception("Error inesperado procesando inbound")
