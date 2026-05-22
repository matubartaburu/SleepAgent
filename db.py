"""
db.py — cliente Supabase y operaciones sobre `sleep_logs`.

F1.0: solo upsert idempotente.
F1.5: agregamos lecturas (`get_last_night`, `get_last_n_nights`) cuando armemos
el reporte.

La tabla `sleep_logs` tiene UNIQUE(night_date, source), así que un upsert con
`on_conflict="night_date,source"` es idempotente por construcción: dos POSTs
del mismo día actualizan la fila en vez de duplicarla.
"""

import logging
from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_SERVICE_KEY

logger = logging.getLogger(__name__)

# Init lazy: el cliente se crea la primera vez que se usa, no al importar.
# Así `import db` no rompe si todavía no hay .env (útil en setup / tests).
_supabase: Client | None = None


def _client() -> Client:
    global _supabase
    if _supabase is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise RuntimeError(
                "Faltan SUPABASE_URL / SUPABASE_SERVICE_KEY en .env"
            )
        _supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _supabase


def upsert_sleep_logs(rows: list[dict]) -> list[dict]:
    """
    Upsert batch sobre `sleep_logs`. Cada row debe traer al menos
    `night_date` (DATE / ISO string) y `source` ('webhook' | 'backfill').
    El resto de las columnas son opcionales y deben matchear la DDL.
    Devuelve la lista de filas guardadas (con `id` poblado).

    Idempotencia: la tabla tiene UNIQUE(night_date, source), así que los
    duplicados se actualizan en vez de fallar.
    """
    if not rows:
        return []

    for r in rows:
        if "night_date" not in r or "source" not in r:
            raise ValueError(f"row sin night_date/source: {r!r}")

    result = (
        _client().table("sleep_logs")
        .upsert(rows, on_conflict="night_date,source")
        .execute()
    )
    saved = result.data or []
    logger.info(
        "sleep_logs upsert | rows=%d | nights=%s",
        len(saved),
        [r.get("night_date") for r in saved],
    )
    return saved


# ─────────────────────────────────────────────────────────────────────────────
# sleep_notes — preguntas de Oscar + respuestas de Mateo + tags.
# Requiere migración aplicada (ver supabase_setup.sql).
# ─────────────────────────────────────────────────────────────────────────────

def insert_sleep_note(*, night_date: str, question: str,
                       anomalies: list[str] | None = None) -> dict | None:
    """
    Crea (o reemplaza vía upsert) la nota abierta de una noche con la pregunta
    de Oscar. La answer y tags se llenan después cuando llegue el inbound.
    Devuelve la fila guardada o None si la tabla aún no existe (migración
    no aplicada). No levanta — log de warning y sigue.
    """
    row = {
        "night_date": night_date,
        "question": question,
        "anomalies": anomalies or [],
    }
    try:
        result = (
            _client().table("sleep_notes")
            .upsert(row, on_conflict="night_date")
            .execute()
        )
        saved = (result.data or [None])[0]
        logger.info("sleep_notes upsert | night_date=%s", night_date)
        return saved
    except Exception as exc:
        logger.warning("insert_sleep_note falló (¿migración aplicada?): %s", exc)
        return None


def get_open_note() -> dict | None:
    """
    Devuelve la nota más reciente que tiene pregunta pero NO answer todavía.
    Sirve para matchear el inbound de WhatsApp con la pregunta abierta.
    """
    try:
        res = (
            _client().table("sleep_notes")
            .select("*")
            .is_("answer", "null")
            .order("asked_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.warning("get_open_note falló: %s", exc)
        return None


def update_note_answer(note_id: int, *, answer: str,
                        tags: list[str], tagger_raw: dict | None = None) -> dict | None:
    """Llena answer + tags + answered_at. Devuelve la fila actualizada."""
    try:
        from datetime import datetime, timezone
        result = (
            _client().table("sleep_notes")
            .update({
                "answer": answer,
                "tags": tags,
                "tagger_raw": tagger_raw or {},
                "answered_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", note_id)
            .execute()
        )
        saved = (result.data or [None])[0]
        logger.info("sleep_notes answered | id=%s tags=%s", note_id, tags)
        return saved
    except Exception as exc:
        logger.warning("update_note_answer falló: %s", exc)
        return None


def get_notes_for_range(start_iso: str, end_iso: str) -> list[dict]:
    """Lee notas (con o sin answer) en el rango de fechas."""
    try:
        res = (
            _client().table("sleep_notes")
            .select("night_date, question, answer, tags, anomalies")
            .gte("night_date", start_iso)
            .lte("night_date", end_iso)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.warning("get_notes_for_range falló: %s", exc)
        return []


def get_last_n_nights(n: int = 60) -> list[dict]:
    """
    Trae las últimas N noches en orden cronológico descendente.
    Usado por el answerer conversacional cuando Mateo hace una pregunta libre.
    """
    res = (
        _client().table("sleep_logs")
        .select(
            "night_date, total_sleep_minutes, in_bed_minutes, "
            "rem_minutes, core_minutes, deep_minutes, awake_minutes, "
            "hrv_sdnn_ms, resting_hr_bpm, avg_hr_bpm, min_hr_bpm, max_hr_bpm, "
            "respiratory_rate_brpm, sleep_start, sleep_end"
        )
        .order("night_date", desc=True)
        .limit(n)
        .execute()
    )
    return res.data or []
