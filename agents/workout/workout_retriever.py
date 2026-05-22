"""
agents/workout/workout_retriever.py — consultas sobre el historial de
entrenamientos en Notion.

Devuelve respuestas en lenguaje natural en rioplatense informal para
mandarlas por WhatsApp.

Queries soportadas:
- last_by_muscle(group)     "última espalda"
- last_by_exercise(name)    "última sentadilla"
- last_running()            "cuánto corrí la última vez"
- recent_running(n=4)       "como vengo en running"
- workouts_for_date(date)   contexto cross-domain para reportes de sueño
- next_day_suggestion()     "qué me toca hoy" (usa Training Plan)
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import notion_store as nc

log = logging.getLogger(__name__)


# ── Workouts: ejercicios por día (sub-DBs) ──────────────────────────────────

def exercises_for_muscles(muscles: list[str]) -> dict[str, Any]:
    """
    Dado un set de músculos (ej: ['pecho', 'hombros']), busca el día del
    plan que matchea y devuelve TODOS los ejercicios registrados en su sub-DB.

    Usado para responder "hoy entreno pecho y hombros" → trae info del Día N.
    """
    match = nc.find_day_for_muscles(muscles)
    if not match:
        return {"found": False, "reason": "no_day_match", "muscles": muscles}
    day_num, day_ds_id = match
    return _exercises_in_day_subdb(day_num, day_ds_id, muscles=muscles)


def exercises_for_day(day_number: int) -> dict[str, Any]:
    """Trae los ejercicios de un Día específico (1-5)."""
    ds_id = nc.day_subdb_id(day_number)
    if not ds_id:
        return {"found": False, "reason": "no_subdb", "day": day_number}
    return _exercises_in_day_subdb(day_number, ds_id)


def _exercises_in_day_subdb(day_num: int, ds_id: str,
                             muscles: list[str] | None = None) -> dict[str, Any]:
    rows = nc.query_db(
        ds_id,
        sorts=[{"property": "last_updated", "direction": "descending"}],
        page_size=50,
    )
    exercises = []
    most_recent_date = None
    for r in rows:
        p = nc.read_page_props(r)
        exercises.append({
            "exercise": p.get("exercise"),
            "sets": p.get("sets"),
            "reps": p.get("reps"),
            "weight_kg": p.get("weight_kg"),
            "rir": p.get("rir"),
            "last_updated": p.get("last_updated"),
            "notes": p.get("notes"),
        })
        if p.get("last_updated") and (not most_recent_date or p["last_updated"] > most_recent_date):
            most_recent_date = p["last_updated"]

    return {
        # `found` = "encontré el día" (no importa si tiene ejercicios o no).
        # El caller distingue "día sin ejercicios" via len(exercises).
        "found": True,
        "day_num": day_num,
        "muscles_requested": muscles or [],
        "exercises": exercises,
        "last_session_date": most_recent_date,
        "days_ago": _days_ago(most_recent_date),
    }


def last_session_by_muscle(muscle_group: str) -> dict[str, Any]:
    """
    Compat: busca el día del plan que toque ese muscle_group y trae sus
    ejercicios. Mantiene la misma shape para no romper callers.
    """
    return exercises_for_muscles([muscle_group])


def last_session_by_exercise(exercise_name: str) -> dict[str, Any]:
    """
    Busca un ejercicio específico en TODOS los sub-DBs por día.
    Devuelve el primero que encuentre (debería haber solo uno por nombre).
    """
    for day_num, ds_id in nc.all_day_subdbs().items():
        rows = nc.query_db(ds_id, filter_={
            "property": "exercise", "title": {"equals": exercise_name.lower()},
        }, page_size=1)
        if rows:
            p = nc.read_page_props(rows[0])
            return {
                "found": True,
                "exercise": p.get("exercise"),
                "day_num": day_num,
                "sets": p.get("sets"),
                "reps": p.get("reps"),
                "weight_kg": p.get("weight_kg"),
                "rir": p.get("rir"),
                "date": p.get("last_updated"),
                "days_ago": _days_ago(p.get("last_updated")),
            }
    return {"found": False, "reason": "no_data"}


# ── Cardio / running ────────────────────────────────────────────────────────

def last_running() -> dict[str, Any]:
    """Última corrida (Apple o manual)."""
    db_id = nc.cardio_db_id()
    if not db_id:
        return {"found": False, "reason": "no_db"}

    rows = nc.query_db(db_id, filter_={
        "property": "sport", "title": {"equals": "running"},
    }, sorts=[{"property": "date", "direction": "descending"}], page_size=1)
    if not rows:
        return {"found": False, "reason": "no_data"}
    p = nc.read_page_props(rows[0])
    return {
        "found": True,
        "date": p.get("date"),
        "duration_min": p.get("duration_min"),
        "distance_km": p.get("distance_km"),
        "pace_min_per_km": p.get("pace_min_per_km"),
        "avg_hr": p.get("avg_hr"),
        "intensity": p.get("intensity"),
        "days_ago": _days_ago(p.get("date")),
    }


def recent_running(n: int = 4) -> dict[str, Any]:
    """Últimas N corridas para ver tendencia."""
    db_id = nc.cardio_db_id()
    if not db_id:
        return {"found": False, "reason": "no_db", "sessions": []}

    rows = nc.query_db(db_id, filter_={
        "property": "sport", "title": {"equals": "running"},
    }, sorts=[{"property": "date", "direction": "descending"}], page_size=n)
    sessions = [nc.read_page_props(r) for r in rows]
    return {
        "found": bool(sessions),
        "sessions": sessions,
        "n": len(sessions),
    }


# ── Cross-domain helpers (usadas por report.py) ─────────────────────────────

def workouts_for_date(target_date: date) -> dict[str, Any]:
    """
    Todos los workouts + cardio de una fecha específica. Usado por el daily
    report de sueño para mencionar "ayer entrenaste X".
    """
    workouts = _workouts_for_date(target_date)
    cardio = _cardio_for_date(target_date)
    return {
        "date": target_date.isoformat(),
        "workouts": workouts,
        "cardio": cardio,
        "any_activity": bool(workouts or cardio),
    }


def workouts_for_range(start: date, end: date) -> dict[str, Any]:
    """Workouts + cardio en un rango — para weekly/monthly reports."""
    cardio_db = nc.cardio_db_id()
    workouts: list[dict] = []
    cardio: list[dict] = []

    # Workouts: agregamos de TODOS los sub-DBs por día
    for day_num, ds_id in nc.all_day_subdbs().items():
        rows = nc.query_db(ds_id, filter_={
            "and": [
                {"property": "last_updated", "date": {"on_or_after": start.isoformat()}},
                {"property": "last_updated", "date": {"on_or_before": end.isoformat()}},
            ],
        }, sorts=[{"property": "last_updated", "direction": "descending"}], page_size=100)
        for r in rows:
            p = nc.read_page_props(r)
            p["day_num"] = day_num
            workouts.append(p)

    if cardio_db:
        rows = nc.query_db(cardio_db, filter_={
            "and": [
                {"property": "date", "date": {"on_or_after": start.isoformat()}},
                {"property": "date", "date": {"on_or_before": end.isoformat()}},
            ],
        }, sorts=[{"property": "date", "direction": "descending"}], page_size=100)
        cardio = [nc.read_page_props(r) for r in rows]
    return {"workouts": workouts, "cardio": cardio,
            "start": start.isoformat(), "end": end.isoformat()}


def _workouts_for_date(target_date: date) -> list[dict]:
    """
    Trae ejercicios actualizados en target_date desde TODOS los sub-DBs por día.
    Útil para cross-domain con sueño (ej: "ayer entrenaste pierna").
    """
    out: list[dict] = []
    iso = target_date.isoformat()
    for day_num, ds_id in nc.all_day_subdbs().items():
        rows = nc.query_db(ds_id, filter_={
            "property": "last_updated", "date": {"equals": iso},
        }, page_size=50)
        for r in rows:
            p = nc.read_page_props(r)
            p["day_num"] = day_num
            out.append(p)
    return out


def _cardio_for_date(target_date: date) -> list[dict]:
    db_id = nc.cardio_db_id()
    if not db_id:
        return []
    rows = nc.query_db(db_id, filter_={
        "property": "date", "date": {"equals": target_date.isoformat()},
    }, page_size=20)
    return [nc.read_page_props(r) for r in rows]


# ── Training plan ───────────────────────────────────────────────────────────

def next_day_suggestion() -> dict[str, Any]:
    """
    Calcula qué día del plan correspondería entrenar hoy basándose en el
    último día logueado en los sub-DBs.
    """
    plan_db = nc.plan_db_id()
    if not plan_db:
        return {"found": False, "reason": "no_plan_db"}

    rows = nc.query_db(plan_db, filter_={
        "property": "active", "checkbox": {"equals": True},
    }, sorts=[{"property": "day_label", "direction": "ascending"}], page_size=10)
    plan_days = [nc.read_page_props(r) for r in rows]
    if not plan_days:
        return {"found": False, "reason": "no_plan"}

    # Buscar el sub-DB con la fecha de último update más reciente.
    import re
    last_day_num = None
    last_date = None
    for day_num, ds_id in nc.all_day_subdbs().items():
        recent = nc.query_db(ds_id, sorts=[
            {"property": "last_updated", "direction": "descending"}
        ], page_size=1)
        if recent:
            p = nc.read_page_props(recent[0])
            d = p.get("last_updated")
            if d and (not last_date or d > last_date):
                last_date = d
                last_day_num = day_num

    next_idx = 0
    if last_day_num:
        # Encontrar el índice del Día N en plan_days
        for i, d in enumerate(plan_days):
            m = re.search(r"\d+", d.get("day_label") or "")
            if m and int(m.group(0)) == last_day_num:
                next_idx = (i + 1) % len(plan_days)
                break

    next_day = plan_days[next_idx]
    return {
        "found": True,
        "next_day_label": next_day.get("day_label"),
        "muscle_groups": next_day.get("muscle_groups") or [],
        "suggested_exercises": next_day.get("suggested_exercises") or "",
        "cardio": next_day.get("cardio") or False,
        "last_trained_day": f"Día {last_day_num}" if last_day_num else None,
        "last_trained_date": last_date,
        "all_days": plan_days,
    }


# ── Misc ────────────────────────────────────────────────────────────────────

def _days_ago(iso_date: str | None) -> int | None:
    if not iso_date:
        return None
    try:
        d = date.fromisoformat(iso_date[:10])
        return (date.today() - d).days
    except (ValueError, TypeError):
        return None
