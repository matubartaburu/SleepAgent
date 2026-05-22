"""
report.py — generacion del reporte diario de Oscar (doctor del sueño).

Flujo:
  1. _build_context()     trae anoche + promedio ultimos 7 dias del DB
  2. _build_user_message() arma el bloque de data con shape predecible
  3. build_daily_report() llama a Claude Sonnet 4.6 con system + data
  4. (opcional) validator LLM-as-judge si OSCAR_VALIDATOR_ENABLED=1
  5. send_daily_report()  envia el texto generado por WhatsApp via Twilio

Si no hay data de anoche (night_date != today), no se manda nada.

Variables de entorno relevantes:
  OSCAR_VALIDATOR_ENABLED  - "1" para activar el validator (Opus 4.7).
  OSCAR_VALIDATOR_MAX_RETRIES - máx intentos de regenerar si rechaza (default 3).
"""

import logging
import os
import statistics
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# Zona horaria del usuario para convertir timestamps UTC de Supabase
# antes de calcular bedtime/waketime (sino las horas salen 3h corridas).
_USER_TZ = ZoneInfo(os.getenv("TZ", "America/Montevideo"))

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY
from db import _client as _supabase
from twilio_client import send_whatsapp_text

logger = logging.getLogger(__name__)

SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Umbral de "sueño corto" — debajo de esto, Oscar te rete (daily) o te marca
# el periodo como insuficiente (weekly/monthly). 7h10min = 430 min.
SLEEP_MIN_THRESHOLD = 430

_anthropic = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


SYSTEM_PROMPT = """\
Sos Oscar, el doctor personal del sueño de Mateo (uruguayo, alrededor de 30 años).
Cada mañana le mandás por WhatsApp un reporte breve sobre cómo durmió anoche.

ESTILO
- Rioplatense informal: vos, podés, querés, dale, ta, joya, buenísimo, bárbaro, che.
- Sin emojis, sin markdown, sin asteriscos, sin negritas. Texto plano.
- Bloques cortos al estilo WhatsApp. Sin punto final en bloques cortos.
- Las preguntas las cerrás solo con `?`. Nunca abrís con `¿`.
- Variedad en afirmaciones: NO uses siempre "perfecto". Alterná entre dale, buenísimo,
  bárbaro, joya, listo, tranqui, ta. Mucho menos "perfecto".
- Tono de doctor amigo: claro, directo, sin alarmismo por una sola noche mala.

CONTENIDO
- 2 a 4 bloques cortos máximo. Total bajo 350 caracteres.
- Identificá 1 o 2 cosas relevantes de anoche, no listes todas las métricas.
- Compará con tu base personal (te paso promedio de últimos 7 días).
- Si algo vale la pena marcar (sueño corto, HRV bajó, resting HR alto, deep flaco, etc.)
  lo decís claro y breve.
- Si la noche fue normal, decilo simple sin estirar.
- Cerrá con algo accionable solo si tiene sentido, no fuerces consejos.

QUÉ NO HACER
- No mostrar formato dashboard (REM: X, Core: Y, etc.).
- No alarmar por una noche aislada.
- No inventar correlaciones que no están en los datos.
- No tecnicismos sin contexto. HRV se puede mencionar pero explicado en una sola frase
  si es la primera vez que aparece en el reporte.
- No saludar con "buenos días Mateo" todos los días igual — variá el saludo.
"""


def _fmt_hm(minutes) -> str:
    if minutes is None:
        return "—"
    minutes = int(minutes)
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}" if h else f"{m}min"


def _fmt_num(v, decimals=1) -> str:
    if v is None:
        return "NA"
    return f"{float(v):.{decimals}f}"


def _build_context() -> dict | None:
    """
    Trae la noche más reciente. Si no es de hoy (night_date != today), devuelve None
    -> no se manda reporte (regla: sin data, silencio).
    Si hay, calcula promedios de las últimas 7 noches en DB (incluyendo anoche).
    """
    today_iso = date.today().isoformat()

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
        logger.info("No hay filas en sleep_logs")
        return None

    last_night = rows[0]
    if last_night["night_date"] != today_iso:
        logger.info(
            "Sin data de anoche (mas reciente=%s, hoy=%s)",
            last_night["night_date"], today_iso,
        )
        return None

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

    return {
        "last_night":  last_night,
        "baseline":    baseline,
        "n_baseline":  len(rows),
    }


def _fetch_yesterday_workout_context(night_date_iso: str) -> str:
    """
    Trae contexto del entrenamiento del día anterior a `night_date_iso` para
    inyectar en el reporte. Tolerante: si Notion no responde o no hay nada,
    devuelve string vacío.
    """
    try:
        from datetime import date as _d, timedelta as _td
        from agents.workout.workout_retriever import workouts_for_date
        night = _d.fromisoformat(night_date_iso)
        yesterday = night - _td(days=1)
        data = workouts_for_date(yesterday)
        if not data.get("any_activity"):
            return ""
        lines = [f"\nACTIVIDAD AYER ({yesterday.isoformat()}):"]
        for w in data.get("workouts", []):
            ex = w.get("exercise", "ejercicio")
            sets = w.get("sets") or "?"
            reps = w.get("reps") or "?"
            wt = w.get("weight_kg")
            ext = f" {sets}x{reps}"
            if wt: ext += f" {wt}kg"
            lines.append(f"  gym: {ex}{ext}")
        for c in data.get("cardio", []):
            sport = c.get("sport", "cardio")
            dur = c.get("duration_min")
            dist = c.get("distance_km")
            inten = c.get("intensity")
            parts = [sport]
            if dist: parts.append(f"{dist}km")
            if dur: parts.append(f"{int(dur)}min")
            if inten: parts.append(inten)
            lines.append(f"  cardio: {' · '.join(parts)}")
        return "\n".join(lines) + "\n"
    except Exception as exc:
        logger.debug("No pude traer workouts cross-domain: %s", exc)
        return ""


def _build_user_message(ctx: dict) -> str:
    n = ctx["last_night"]
    b = ctx["baseline"]
    nb = ctx["n_baseline"]

    base_sleep = _fmt_hm(b["sleep_min"]) if b["sleep_min"] else "NA"
    base_deep  = _fmt_hm(b["deep_min"])  if b["deep_min"]  else "NA"
    base_rem   = _fmt_hm(b["rem_min"])   if b["rem_min"]   else "NA"

    # Cross-domain: contexto de workout del día anterior a esta noche
    workout_ctx = _fetch_yesterday_workout_context(n["night_date"])

    msg = (
        f"ANOCHE ({n['night_date']})\n"
        f"  Total sueño: {_fmt_hm(n.get('total_sleep_minutes'))}\n"
        f"  En cama: {_fmt_hm(n.get('in_bed_minutes'))}\n"
        f"  REM: {_fmt_hm(n.get('rem_minutes'))}\n"
        f"  Core: {_fmt_hm(n.get('core_minutes'))}\n"
        f"  Deep: {_fmt_hm(n.get('deep_minutes'))}\n"
        f"  Awake: {_fmt_hm(n.get('awake_minutes'))}\n"
        f"  HRV: {_fmt_num(n.get('hrv_sdnn_ms'))} ms\n"
        f"  Resting HR: {_fmt_num(n.get('resting_hr_bpm'))} bpm\n"
        f"  HR del dia (avg/min/max): {_fmt_num(n.get('avg_hr_bpm'))} / "
        f"{_fmt_num(n.get('min_hr_bpm'))} / {_fmt_num(n.get('max_hr_bpm'))}\n"
        f"  Respiratoria: {_fmt_num(n.get('respiratory_rate_brpm'))} brpm\n"
        f"\n"
        f"BASE PERSONAL (promedio de las ultimas {nb} noches en DB, incluye anoche)\n"
        f"  Sueño: {base_sleep}\n"
        f"  Deep: {base_deep}\n"
        f"  REM: {base_rem}\n"
        f"  HRV: {_fmt_num(b['hrv'])} ms\n"
        f"  Resting HR: {_fmt_num(b['rhr'])} bpm\n"
        f"  Respiratoria: {_fmt_num(b['rr'])} brpm\n"
        f"\n"
    )

    # Cross-domain: si entrenó ayer, mencionarlo (sin forzarlo en el reporte).
    if workout_ctx:
        msg += workout_ctx + "\n"

    # Reto si durmió < 7h10 (umbral autoimpuesto por Mateo).
    ts = n.get("total_sleep_minutes")
    if ts is not None and ts < SLEEP_MIN_THRESHOLD:
        msg += (
            f"⚠ FLAG INTERNO (no copiar literal): Mateo durmió {_fmt_hm(ts)}, "
            f"por debajo de su umbral autoimpuesto de 7h10. Reproche cariñoso pero firme. "
            f"No le suaves, dejá clarito que es poco. No exageres tampoco.\n"
            f"\n"
        )

    msg += "Generá el reporte de Oscar para Mateo, en rioplatense, sin formato.\n"
    return msg


def build_daily_report() -> str | None:
    """Devuelve el texto del reporte, o None si no hay data de anoche."""
    if _anthropic is None:
        raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")

    ctx = _build_context()
    if not ctx:
        return None

    user_msg = _build_user_message(ctx)
    logger.info("Generando reporte para night_date=%s", ctx["last_night"]["night_date"])

    response = _anthropic.messages.create(
        model=SONNET_MODEL,
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = response.content[0].text.strip()
    logger.info("Reporte generado (%d chars)", len(text))
    return text


def _validated_or_regenerate(text: str, ctx: dict, max_retries: int) -> tuple[str, dict | None]:
    """
    Si el validator está activo, valida y, si rechaza, regenera con feedback
    hasta max_retries veces. Devuelve (texto_final, verdict_dict_o_None).
    Si los retries se agotan, devuelve el MEJOR intento + el último verdict.
    """
    from agents.validator import validate_report  # import lazy para no cargar Opus si no se usa

    best_text = text
    best_verdict = None
    feedback = None
    user_msg = _build_user_message(ctx)

    for attempt in range(1, max_retries + 1):
        verdict = validate_report(best_text, ctx["last_night"], ctx["baseline"])
        best_verdict = verdict
        logger.info(
            "Validator intento=%d approved=%s score=%d cost=$%.4f",
            attempt, verdict.approved, verdict.score, verdict.cost_usd,
        )
        if verdict.approved:
            return best_text, _verdict_to_dict(verdict)
        if attempt >= max_retries:
            break

        # Re-generamos con feedback explícito
        feedback = (
            f"\n\n=== FEEDBACK DEL VALIDADOR (intento {attempt}) ===\n"
            f"score={verdict.score}/10\n"
            f"issues:\n- " + "\n- ".join(verdict.issues) + "\n"
            f"hint: {verdict.suggested_fix_hint}\n"
            f"Volvé a generar respetando estos puntos."
        )
        response = _anthropic.messages.create(
            model=SONNET_MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg + feedback}],
        )
        best_text = response.content[0].text.strip()
        logger.info("Regenerado intento=%d (%d chars)", attempt, len(best_text))

    return best_text, _verdict_to_dict(best_verdict) if best_verdict else None


def _verdict_to_dict(v) -> dict:
    return {
        "approved": v.approved,
        "score": v.score,
        "issues": v.issues,
        "cost_usd": v.cost_usd,
    }


def send_daily_report() -> dict:
    """Genera y envia. Devuelve dict con resultado."""
    if _anthropic is None:
        raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")

    ctx = _build_context()
    if not ctx:
        return {"sent": False, "reason": "no_data"}

    user_msg = _build_user_message(ctx)
    response = _anthropic.messages.create(
        model=SONNET_MODEL,
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = response.content[0].text.strip()

    validator_result = None
    if os.getenv("OSCAR_VALIDATOR_ENABLED") == "1":
        max_retries = int(os.getenv("OSCAR_VALIDATOR_MAX_RETRIES", "3"))
        text, validator_result = _validated_or_regenerate(text, ctx, max_retries)

        if validator_result and not validator_result["approved"]:
            # Falló los N intentos. NO mandamos el reporte; mandamos alerta.
            alert = (
                "Oscar generó un reporte pero el validator lo rechazó "
                f"{max_retries} veces. Score final: {validator_result['score']}/10.\n"
                f"Issues:\n- " + "\n- ".join(validator_result["issues"]) + "\n\n"
                f"--- mejor intento ---\n{text}"
            )
            sid = send_whatsapp_text(body=alert)
            return {
                "sent": True, "sid": sid, "reason": "validator_rejected",
                "validator": validator_result,
            }

    sid = send_whatsapp_text(body=text)
    result = {
        "sent": True, "sid": sid, "chars": len(text), "preview": text,
        "validator": validator_result,
    }

    # Detector de anomalías → si encuentra cosas raras, dispara follow-up question.
    try:
        anomalies = _detect_anomalies(ctx["last_night"], ctx["baseline"])
        if anomalies:
            q = _generate_followup_question(ctx["last_night"], anomalies)
            if q:
                from db import insert_sleep_note  # lazy, requiere migración
                try:
                    insert_sleep_note(
                        night_date=ctx["last_night"]["night_date"],
                        question=q,
                        anomalies=anomalies,
                    )
                except Exception as exc:
                    logger.warning("No pude persistir la nota (¿migración aplicada?): %s", exc)
                followup_sid = send_whatsapp_text(body=q)
                result["followup"] = {"sid": followup_sid, "anomalies": anomalies, "question": q}
    except Exception:
        logger.exception("Error en follow-up de anomalías (el reporte ya se mandó)")

    return result


# ============================================================================
# Helpers de consistencia (SD de bedtime / waketime, etiquetas)
# ============================================================================

def _to_minutes_since_noon(iso_ts: str | None) -> int | None:
    """
    Normaliza una hora a 'minutos desde el mediodía' para que bedtimes
    crossing-midnight queden continuos: 23:00 → 660, 00:30 → 750, 02:00 → 840.
    Convierte UTC → TZ local del usuario antes de extraer hora.
    """
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace(" ", "T"))
        local = dt.astimezone(_USER_TZ)
    except (ValueError, TypeError):
        return None
    return ((local.hour - 12) % 24) * 60 + local.minute


def _to_minutes_since_midnight(iso_ts: str | None) -> int | None:
    """Para horas de despertar (5am-12pm): simple h*60+m, en TZ local."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace(" ", "T"))
        local = dt.astimezone(_USER_TZ)
    except (ValueError, TypeError):
        return None
    return local.hour * 60 + local.minute


def _stddev_minutes(values: list[int | None]) -> float | None:
    """SD poblacional sobre los no-None. Devuelve None si <2 valores válidos."""
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return None
    return statistics.pstdev(clean)


def _consistency_label(sd_minutes: float | None) -> str:
    """Mapea SD a etiqueta humana. None si no hay data."""
    if sd_minutes is None:
        return "sin data"
    if sd_minutes <= 25:
        return "muy consistente"
    if sd_minutes <= 50:
        return "bastante consistente"
    if sd_minutes <= 90:
        return "variable"
    return "muy variable"


def _minutes_to_clock(minutes_since_noon: float | None) -> str:
    """Convierte 'minutos desde mediodía' de vuelta a HH:MM (24h)."""
    if minutes_since_noon is None:
        return "—"
    total = int(round(minutes_since_noon))
    hour_offset = (total // 60) + 12
    h = hour_offset % 24
    m = total % 60
    return f"{h:02d}:{m:02d}"


def _minutes_since_midnight_to_clock(m: float | None) -> str:
    if m is None:
        return "—"
    total = int(round(m))
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


# ============================================================================
# Detector de anomalías + follow-up question (daily)
# ============================================================================

def _detect_anomalies(night: dict, baseline: dict) -> list[str]:
    """
    Devuelve lista de tags-string describiendo lo raro de la noche.
    Pensado para alimentar la pregunta de follow-up y la tabla sleep_notes.
    """
    out: list[str] = []
    ts = night.get("total_sleep_minutes")
    if ts is not None and ts < SLEEP_MIN_THRESHOLD:
        out.append(f"sueño_corto ({_fmt_hm(ts)})")

    deep = night.get("deep_minutes")
    base_deep = baseline.get("deep_min")
    if deep is not None and base_deep and deep < base_deep * 0.6:
        out.append(f"deep_flaco ({_fmt_hm(deep)} vs base {_fmt_hm(base_deep)})")

    awake = night.get("awake_minutes")
    if awake is not None and awake > 60:
        out.append(f"awake_alto ({_fmt_hm(awake)})")

    hrv = night.get("hrv_sdnn_ms")
    base_hrv = baseline.get("hrv")
    if hrv is not None and base_hrv and hrv < base_hrv * 0.85:
        out.append(f"hrv_bajo ({hrv:.0f} vs base {base_hrv:.0f})")

    rhr = night.get("resting_hr_bpm")
    base_rhr = baseline.get("rhr")
    if rhr is not None and base_rhr and rhr > base_rhr * 1.10:
        out.append(f"rhr_alto ({rhr:.0f} vs base {base_rhr:.0f})")

    rr = night.get("respiratory_rate_brpm")
    base_rr = baseline.get("rr")
    if rr is not None and base_rr and rr > base_rr + 2:
        out.append(f"rr_alto ({rr:.1f} vs base {base_rr:.1f})")

    return out


_FOLLOWUP_SYSTEM = """\
Sos Oscar, escribiendo un SOLO mensaje corto de WhatsApp a Mateo para preguntarle
qué pasó anoche. Vio una o más anomalías en sus datos. Pregunta abierta y
amigable, en rioplatense informal, sin emojis ni markdown.

Reglas:
- Máximo 200 caracteres.
- Mencioná concretamente qué viste raro (no listes todo, lo más relevante).
- Ofrecé 4-6 sospechosos típicos como opciones (comida tarde, alcohol, cafe, deporte,
  estrés, viaje, ruido) y pedí que conteste libre.
- Cerrá pregunta con `?`. Nunca abrás con `¿`.
- No alarmista. No usar "perfecto".
"""


def _generate_followup_question(night: dict, anomalies: list[str]) -> str | None:
    """Pide a Sonnet que arme la pregunta. Devuelve string listo para Twilio."""
    if _anthropic is None or not anomalies:
        return None
    user = (
        f"Noche del {night.get('night_date')}.\n"
        f"Anomalías detectadas:\n- " + "\n- ".join(anomalies) + "\n\n"
        f"Generá el mensaje de pregunta."
    )
    resp = _anthropic.messages.create(
        model=SONNET_MODEL, max_tokens=200,
        system=_FOLLOWUP_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = resp.content[0].text.strip()
    logger.info("Follow-up question generada (%d chars)", len(text))
    return text


# ============================================================================
# Weekly report (domingo 09:30)
# ============================================================================

WEEKLY_SYSTEM_PROMPT = """\
Sos Oscar, doctor personal del sueño de Mateo. Le mandás los DOMINGOS un
mini-recap de la semana en WhatsApp.

ESTILO
- Rioplatense informal. Sin emojis, sin markdown, sin asteriscos.
- Texto plano, bloques cortos estilo WhatsApp.
- Preguntas con `?`. Nunca abrás con `¿`.
- Variá afirmaciones (dale, joya, bárbaro, buenísimo, ta). Evitá "perfecto".

CONTENIDO (4-6 bloques cortos, total bajo 600 caracteres)
- 1 línea de encabezado de la semana ("Semana del X al Y").
- Promedio horas dormidas + cómo se compara con su umbral autoimpuesto de 7h10.
- Consistencia de horario (lo más importante para sueño): de dormir y de despertar.
- 1 detalle destacable: mejor noche, peor noche, o tendencia de HRV/RR/RHR.
- Si hay notas que Mateo respondió esta semana, conectá causas: "las 2 peores
  noches fueron post-comida-tarde" o "ojo con X".
- Cerrá con 1 sugerencia accionable concreta para la semana siguiente. Una sola.

QUÉ NO HACER
- No listar todas las métricas, eligí 2-3.
- No alarmar. Una semana bajo umbral no es catástrofe, sí merece marcarlo claro.
- Si promedio <7h10: tono firme pero cariñoso, dejá claro que es insuficiente.
- Si promedio >=7h10: celebrá sin estirar.
"""


def _build_weekly_context(days: int = 7) -> dict | None:
    """Trae las últimas `days` noches + notas asociadas."""
    end = date.today()
    start = end - timedelta(days=days)

    res = (
        _supabase()
        .table("sleep_logs")
        .select(
            "night_date, total_sleep_minutes, in_bed_minutes, "
            "rem_minutes, core_minutes, deep_minutes, awake_minutes, "
            "hrv_sdnn_ms, resting_hr_bpm, respiratory_rate_brpm, "
            "sleep_start, sleep_end"
        )
        .gte("night_date", start.isoformat())
        .lte("night_date", end.isoformat())
        .order("night_date", desc=True)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None

    notes = _fetch_notes_for_range(start, end)
    return _aggregate_period(rows, notes, label=f"semana del {start} al {end}",
                             period_days=days, period_start=start, period_end=end)


def _fetch_notes_for_range(start: date, end: date) -> list[dict]:
    """Lee sleep_notes en el rango. Si la tabla no existe aún, devuelve []."""
    try:
        res = (
            _supabase()
            .table("sleep_notes")
            .select("night_date, question, answer, tags")
            .gte("night_date", start.isoformat())
            .lte("night_date", end.isoformat())
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.info("sleep_notes no disponible (%s) — sigo sin notas", exc)
        return []


def _fetch_workouts_summary_for_range(start: date, end: date) -> str:
    """
    Trae todos los workouts + cardio del rango para inyectar en weekly/monthly.
    Devuelve string formateado o vacío si no hay nada.
    """
    try:
        from agents.workout.workout_retriever import workouts_for_range
        data = workouts_for_range(start, end)
        workouts = data.get("workouts", [])
        cardio = data.get("cardio", [])
        if not workouts and not cardio:
            return ""

        # Agrupar workouts por sesión (mismo session_id)
        sessions: dict[str, list[dict]] = {}
        for w in workouts:
            sid = (w.get("session_id") or w.get("date") or "")
            sessions.setdefault(sid, []).append(w)

        lines = [f"\nACTIVIDAD EN EL PERIODO ({start} al {end}):"]
        lines.append(f"  Sesiones de gym: {len(sessions)} ({len(workouts)} ejercicios totales)")

        # Sumario por grupo muscular
        muscle_counts: dict[str, int] = {}
        for w in workouts:
            for mg in (w.get("muscle_group") or []):
                muscle_counts[mg] = muscle_counts.get(mg, 0) + 1
        if muscle_counts:
            top_muscles = sorted(muscle_counts.items(), key=lambda x: -x[1])[:5]
            lines.append("  Grupos más trabajados: " +
                          ", ".join(f"{m} ({n})" for m, n in top_muscles))

        # Sumario de cardio
        if cardio:
            total_km = sum((c.get("distance_km") or 0) for c in cardio)
            total_min = sum((c.get("duration_min") or 0) for c in cardio)
            lines.append(f"  Sesiones de cardio: {len(cardio)}"
                         + (f" · {total_km:.1f}km" if total_km else "")
                         + (f" · {int(total_min)}min totales" if total_min else ""))
            running = [c for c in cardio if (c.get("sport") or "").lower() == "running"]
            if running:
                run_km = sum((c.get("distance_km") or 0) for c in running)
                lines.append(f"  Running: {len(running)} corridas, {run_km:.1f}km")

        return "\n".join(lines) + "\n"
    except Exception as exc:
        logger.debug("No pude traer workouts para weekly/monthly: %s", exc)
        return ""


def _aggregate_period(rows: list[dict], notes: list[dict], *,
                      label: str, period_days: int,
                      period_start: date | None = None,
                      period_end: date | None = None) -> dict:
    def avg(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    sleep_min_avg = avg("total_sleep_minutes")
    bedtimes = [_to_minutes_since_noon(r.get("sleep_start")) for r in rows]
    waketimes = [_to_minutes_since_midnight(r.get("sleep_end")) for r in rows]

    # Distribución de horas dormidas (cuántas noches en cada bucket).
    distribution = {"<6h": 0, "6-7h": 0, "7-8h": 0, "8h+": 0}
    for r in rows:
        ts = r.get("total_sleep_minutes")
        if ts is None:
            continue
        if ts < 360:        distribution["<6h"]  += 1
        elif ts < 420:      distribution["6-7h"] += 1
        elif ts < 480:      distribution["7-8h"] += 1
        else:               distribution["8h+"]  += 1

    # Mejor / peor noche por horas dormidas.
    nights_with_ts = [r for r in rows if r.get("total_sleep_minutes") is not None]
    best = max(nights_with_ts, key=lambda r: r["total_sleep_minutes"], default=None)
    worst = min(nights_with_ts, key=lambda r: r["total_sleep_minutes"], default=None)

    # Agrupar notas por causa más frecuente.
    tag_counts: dict[str, int] = {}
    for note in notes:
        for tag in (note.get("tags") or []):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    # Cross-domain: traer resumen de entrenamientos del periodo si tenemos
    # las fechas exactas. Tolerante a fallas.
    workout_summary = ""
    if period_start and period_end:
        workout_summary = _fetch_workouts_summary_for_range(period_start, period_end)

    return {
        "label": label,
        "period_days": period_days,
        "n_nights":    len(rows),
        "period_workout_summary": workout_summary,
        "sleep_min_avg":     sleep_min_avg,
        "hrv_avg":           avg("hrv_sdnn_ms"),
        "rhr_avg":           avg("resting_hr_bpm"),
        "rr_avg":            avg("respiratory_rate_brpm"),
        "deep_avg":          avg("deep_minutes"),
        "rem_avg":           avg("rem_minutes"),
        "awake_avg":         avg("awake_minutes"),
        "bedtime_sd_min":    _stddev_minutes(bedtimes),
        "bedtime_mean_min":  (sum(b for b in bedtimes if b is not None)
                              / max(1, sum(1 for b in bedtimes if b is not None))
                              ) if any(b is not None for b in bedtimes) else None,
        "waketime_sd_min":   _stddev_minutes(waketimes),
        "waketime_mean_min": (sum(w for w in waketimes if w is not None)
                              / max(1, sum(1 for w in waketimes if w is not None))
                              ) if any(w is not None for w in waketimes) else None,
        "distribution":      distribution,
        "best_night":        best,
        "worst_night":       worst,
        "notes_count":       len(notes),
        "tag_counts":        tag_counts,
        "rows":              rows,
    }


def _build_weekly_user_message(ctx: dict) -> str:
    msg = (
        f"=== RESUMEN DE LA SEMANA ===\n"
        f"Periodo: {ctx['label']} ({ctx['n_nights']} noches con data).\n"
        f"\n"
        f"PROMEDIOS\n"
        f"  Sueño total: {_fmt_hm(ctx['sleep_min_avg'])} "
        f"(umbral autoimpuesto Mateo: 7h10)\n"
        f"  Deep: {_fmt_hm(ctx['deep_avg'])}\n"
        f"  REM: {_fmt_hm(ctx['rem_avg'])}\n"
        f"  Awake: {_fmt_hm(ctx['awake_avg'])}\n"
        f"  HRV: {_fmt_num(ctx['hrv_avg'])} ms\n"
        f"  Resting HR: {_fmt_num(ctx['rhr_avg'])} bpm\n"
        f"  Respiratoria: {_fmt_num(ctx['rr_avg'])} brpm\n"
        f"\n"
        f"CONSISTENCIA DE HORARIO\n"
        f"  Hora de dormir: {_minutes_to_clock(ctx['bedtime_mean_min'])} promedio, "
        f"SD={_fmt_num(ctx['bedtime_sd_min'])} min ({_consistency_label(ctx['bedtime_sd_min'])})\n"
        f"  Hora de despertar: {_minutes_since_midnight_to_clock(ctx['waketime_mean_min'])} promedio, "
        f"SD={_fmt_num(ctx['waketime_sd_min'])} min ({_consistency_label(ctx['waketime_sd_min'])})\n"
        f"\n"
        f"DISTRIBUCIÓN DE NOCHES\n"
        f"  <6h: {ctx['distribution']['<6h']} | 6-7h: {ctx['distribution']['6-7h']} | "
        f"7-8h: {ctx['distribution']['7-8h']} | 8h+: {ctx['distribution']['8h+']}\n"
        f"\n"
    )
    if ctx.get("best_night"):
        msg += (
            f"MEJOR: {ctx['best_night']['night_date']} → "
            f"{_fmt_hm(ctx['best_night']['total_sleep_minutes'])}\n"
        )
    if ctx.get("worst_night"):
        msg += (
            f"PEOR: {ctx['worst_night']['night_date']} → "
            f"{_fmt_hm(ctx['worst_night']['total_sleep_minutes'])}\n"
        )
    if ctx.get("tag_counts"):
        msg += "\nCAUSAS REPORTADAS ESTA SEMANA (de las notas que Mateo respondió):\n"
        for tag, n in sorted(ctx["tag_counts"].items(), key=lambda x: -x[1]):
            msg += f"  {tag}: {n} vez(es)\n"
    # Cross-domain: contexto de entrenamientos del periodo
    period_workout = ctx.get("period_workout_summary") or ""
    if period_workout:
        msg += period_workout
    avg = ctx.get("sleep_min_avg")
    if avg is not None and avg < SLEEP_MIN_THRESHOLD:
        msg += (
            f"\n⚠ FLAG INTERNO: el promedio semanal de {_fmt_hm(avg)} está por debajo "
            f"de su umbral autoimpuesto de 7h10. Tono firme pero cariñoso, marcalo claro.\n"
        )
    msg += "\nGenerá el reporte SEMANAL de Oscar para Mateo, en rioplatense, sin formato.\n"
    return msg


def build_weekly_report() -> str | None:
    if _anthropic is None:
        raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")
    ctx = _build_weekly_context(days=7)
    if not ctx or ctx["n_nights"] == 0:
        return None
    user_msg = _build_weekly_user_message(ctx)
    resp = _anthropic.messages.create(
        model=SONNET_MODEL, max_tokens=600,
        system=WEEKLY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text.strip()
    logger.info("Reporte semanal generado (%d chars)", len(text))
    return text


def send_weekly_report() -> dict:
    text = build_weekly_report()
    if text is None:
        return {"sent": False, "reason": "no_data"}
    sid = send_whatsapp_text(body=text)
    return {"sent": True, "sid": sid, "chars": len(text), "preview": text, "period": "weekly"}


# ============================================================================
# Monthly report (día 1 10:30)
# ============================================================================

MONTHLY_SYSTEM_PROMPT = """\
Sos Oscar, doctor del sueño de Mateo. Le mandás el día 1 de cada mes un recap
del mes pasado en WhatsApp.

ESTILO
- Rioplatense informal. Sin emojis ni markdown.
- Texto plano, bloques cortos estilo WhatsApp.
- Variá afirmaciones (dale, joya, bárbaro, ta). Evitá "perfecto".

CONTENIDO (6-9 bloques cortos, total bajo 900 caracteres)
- Encabezado: "Resumen de [mes]"
- Promedio mensual de horas dormidas vs su umbral autoimpuesto de 7h10.
- Tendencia vs mes anterior si hay data: ¿mejoró, empeoró, igual?
- Consistencia de horario (dormir y despertar).
- Distribución de noches por bucket (<6h, 6-7h, 7-8h, 8h+).
- Tendencias HRV / RHR / RR del mes — sólo lo que valga la pena.
- Si hay notas, conectá patrones: "tus peores noches suelen ser post-X".
- 1 goal concreto para el mes que arranca.

QUÉ NO HACER
- No listar todas las métricas, sintetizá.
- Si promedio <7h10: firme pero cariñoso, claro que es problema sostenido.
- Si mejoró respecto al mes pasado, reconocelo.
"""


def _build_monthly_context(days: int = 30) -> dict | None:
    end = date.today()
    start_curr = end - timedelta(days=days)
    start_prev = end - timedelta(days=days * 2)

    # Mes actual
    curr = (
        _supabase()
        .table("sleep_logs")
        .select(
            "night_date, total_sleep_minutes, in_bed_minutes, "
            "rem_minutes, core_minutes, deep_minutes, awake_minutes, "
            "hrv_sdnn_ms, resting_hr_bpm, respiratory_rate_brpm, "
            "sleep_start, sleep_end"
        )
        .gte("night_date", start_curr.isoformat())
        .lte("night_date", end.isoformat())
        .order("night_date", desc=True)
        .execute()
    )
    rows = curr.data or []
    if not rows:
        return None

    notes = _fetch_notes_for_range(start_curr, end)
    ctx = _aggregate_period(rows, notes,
                            label=f"último mes ({start_curr} al {end})",
                            period_days=days,
                            period_start=start_curr, period_end=end)

    # Mes anterior (para comparación)
    prev = (
        _supabase()
        .table("sleep_logs")
        .select("night_date, total_sleep_minutes, hrv_sdnn_ms, resting_hr_bpm")
        .gte("night_date", start_prev.isoformat())
        .lt("night_date", start_curr.isoformat())
        .execute()
    )
    prev_rows = prev.data or []
    if prev_rows:
        def pavg(key):
            vals = [r[key] for r in prev_rows if r.get(key) is not None]
            return sum(vals) / len(vals) if vals else None
        ctx["prev_sleep_min_avg"] = pavg("total_sleep_minutes")
        ctx["prev_hrv_avg"]       = pavg("hrv_sdnn_ms")
        ctx["prev_rhr_avg"]       = pavg("resting_hr_bpm")
        ctx["prev_n_nights"]      = len(prev_rows)
    return ctx


def _build_monthly_user_message(ctx: dict) -> str:
    msg = _build_weekly_user_message(ctx)  # reutilizamos la estructura base
    # Sumamos comparación con mes anterior si hay
    if ctx.get("prev_sleep_min_avg") is not None:
        diff_min = (ctx["sleep_min_avg"] or 0) - ctx["prev_sleep_min_avg"]
        sign = "+" if diff_min >= 0 else ""
        msg += (
            f"\nCOMPARACIÓN VS MES ANTERIOR "
            f"({ctx.get('prev_n_nights', 0)} noches)\n"
            f"  Sueño: {_fmt_hm(ctx['prev_sleep_min_avg'])} → "
            f"{_fmt_hm(ctx['sleep_min_avg'])} ({sign}{int(diff_min)} min)\n"
        )
        if ctx.get("prev_hrv_avg") and ctx.get("hrv_avg"):
            msg += (f"  HRV: {_fmt_num(ctx['prev_hrv_avg'])} → "
                    f"{_fmt_num(ctx['hrv_avg'])} ms\n")
        if ctx.get("prev_rhr_avg") and ctx.get("rhr_avg"):
            msg += (f"  RHR: {_fmt_num(ctx['prev_rhr_avg'])} → "
                    f"{_fmt_num(ctx['rhr_avg'])} bpm\n")
    # Reemplazamos el cierre weekly por uno mensual
    msg = msg.replace(
        "Generá el reporte SEMANAL de Oscar para Mateo, en rioplatense, sin formato.\n",
        "Generá el reporte MENSUAL de Oscar para Mateo, en rioplatense, sin formato.\n",
    )
    return msg


def build_monthly_report() -> str | None:
    if _anthropic is None:
        raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")
    ctx = _build_monthly_context(days=30)
    if not ctx or ctx["n_nights"] == 0:
        return None
    user_msg = _build_monthly_user_message(ctx)
    resp = _anthropic.messages.create(
        model=SONNET_MODEL, max_tokens=900,
        system=MONTHLY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text.strip()
    logger.info("Reporte mensual generado (%d chars)", len(text))
    return text


def send_monthly_report() -> dict:
    text = build_monthly_report()
    if text is None:
        return {"sent": False, "reason": "no_data"}
    sid = send_whatsapp_text(body=text)
    return {"sent": True, "sid": sid, "chars": len(text), "preview": text, "period": "monthly"}
