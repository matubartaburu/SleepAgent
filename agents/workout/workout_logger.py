"""
agents/workout/workout_logger.py — persiste workouts y cardio sessions
en Notion.

Decisiones de diseño:
- Una fila por ejercicio (no por sesión). Agrupa con session_id (uuid).
- En cardio: dedup por (sport, date, ±5min) para no duplicar Apple vs manual.
- Llama al muscle_classifier para resolver el grupo y aprender alias nuevos.
- Tolera fallas: si Notion no responde, loguea pero no rompe el flujo.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import notion_store as nc
from agents.workout.muscle_classifier import classify, learn_alias
from agents.workout.workout_parser import ParsedExercise, WorkoutParseResult
from agents.workout.cardio_parser import CardioParseResult

log = logging.getLogger(__name__)


# ── Workouts (musculación) ──────────────────────────────────────────────────

def log_workout_session(
    result: WorkoutParseResult,
    *,
    voice_note_sid: str | None = None,
    day_label: str | None = None,
) -> dict[str, Any]:
    """
    Loguea una sesión de musculación en los sub-DBs por día.

    Para cada ejercicio:
    1. Detecta su muscle_group.
    2. Busca en el Training Plan qué Día tiene ese muscle_group.
    3. Upsert por exercise dentro del sub-DB de ese día: si ya existe,
       actualiza con el último valor. Si no, crea.

    Si no hay match con ningún día, el ejercicio queda sin loguear (warning
    en logs) — el caller puede preguntarle al user.
    """
    if not result.has_exercises:
        return {"logged": 0, "exercises": [], "reason": "no_exercises"}

    session_id = str(uuid.uuid4())[:8]
    date_iso = result.resolved_date.isoformat()
    logged: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for ex in result.exercises:
        classification = classify(ex.exercise)
        canonical = classification.canonical_name or ex.exercise

        # Aprende alias nuevo (LLM) si vale la pena
        if classification.is_new_alias and classification.muscle_groups:
            learn_alias(ex.exercise, canonical, classification.muscle_groups)

        # Buscar día del plan que coincide con los músculos del ejercicio.
        match = nc.find_day_for_muscles(classification.muscle_groups)
        if not match:
            log.warning("Sin día matching para %r (muscles=%s)",
                        canonical, classification.muscle_groups)
            skipped.append({
                "exercise": canonical,
                "muscle_groups": classification.muscle_groups,
                "reason": "no_day_match",
            })
            continue
        day_num, day_ds_id = match

        # Upsert: buscar si ya existe el ejercicio en este día
        existing_page_id, last_values = _find_existing_exercise(day_ds_id, canonical)

        if existing_page_id:
            # PARTIAL UPDATE: solo actualizamos campos con valor nuevo NO-None.
            # Esto preserva los valores anteriores en correcciones tipo
            # "press banca fueron 4 reps no 5" — solo cambia reps, deja peso/sets.
            update_props: dict = {"last_updated": nc.prop_date(date_iso)}
            changed_fields: list[str] = []

            if ex.sets is not None:
                update_props["sets"] = nc.prop_number(ex.sets)
                if last_values and last_values.get("sets") != ex.sets:
                    changed_fields.append(f"sets {last_values.get('sets')}→{ex.sets}")
            if ex.reps is not None:
                update_props["reps"] = nc.prop_number(ex.reps)
                if last_values and last_values.get("reps") != ex.reps:
                    changed_fields.append(f"reps {last_values.get('reps')}→{ex.reps}")
            if ex.weight_kg is not None:
                update_props["weight_kg"] = nc.prop_number(ex.weight_kg)
                if last_values and last_values.get("weight_kg") != ex.weight_kg:
                    changed_fields.append(f"peso {last_values.get('weight_kg')}→{ex.weight_kg}kg")
            if ex.rir is not None:
                update_props["rir"] = nc.prop_number(ex.rir)
            if ex.notes:
                update_props["notes"] = nc.prop_text(ex.notes)

            page = nc.update_page(existing_page_id, update_props)
            action = "updated"
        else:
            # CREATE: incluimos todos los campos (los None quedan null en Notion).
            properties = {
                "exercise":     nc.prop_title(canonical),
                "sets":         nc.prop_number(ex.sets),
                "reps":         nc.prop_number(ex.reps),
                "weight_kg":    nc.prop_number(ex.weight_kg),
                "rir":          nc.prop_number(ex.rir),
                "notes":        nc.prop_text(ex.notes or result.session_notes),
                "last_updated": nc.prop_date(date_iso),
            }
            page = nc.create_page_in_db(day_ds_id, properties)
            action = "created"
            changed_fields = []

        if page:
            # Para reportar al user: si fue update, los valores actuales son
            # los previos + los cambios. Si fue create, lo nuevo.
            final_values = {
                "sets": ex.sets if ex.sets is not None else (last_values or {}).get("sets"),
                "reps": ex.reps if ex.reps is not None else (last_values or {}).get("reps"),
                "weight_kg": ex.weight_kg if ex.weight_kg is not None else (last_values or {}).get("weight_kg"),
                "rir": ex.rir if ex.rir is not None else (last_values or {}).get("rir"),
                "notes": ex.notes or (last_values or {}).get("notes") or "",
            }
            logged.append({
                "exercise": canonical,
                "day_num": day_num,
                "muscle_groups": classification.muscle_groups,
                "sets": final_values["sets"],
                "reps": final_values["reps"],
                "weight_kg": final_values["weight_kg"],
                "rir": final_values["rir"],
                "notes": final_values["notes"],
                "previous": last_values,
                "changed_fields": changed_fields,
                "action": action,
                "page_id": page.get("id"),
            })
            log.info("Workout %s: %s → Día %d sets=%s reps=%s wt=%s changes=%s",
                     action, canonical, day_num, final_values["sets"],
                     final_values["reps"], final_values["weight_kg"], changed_fields)

    # ── Sincronizar el body de las pages de Día tocadas con el contenido
    #    actualizado del sub-DB. Best-effort, no rompe si Notion falla.
    days_touched = {e["day_num"] for e in logged if e.get("day_num")}
    for day_num in days_touched:
        try:
            _sync_day_page_body(day_num)
        except Exception as exc:
            log.warning("No pude sync body de Día %d: %s", day_num, exc)

    return {
        "logged": len(logged),
        "skipped": len(skipped),
        "exercises": logged,
        "skipped_exercises": skipped,
        "session_id": session_id,
        "resolved_date": date_iso,
    }


# ── Sync del body de la page del Día ────────────────────────────────────

_EXERCISES_HEADER_TEXT = "📋 Ejercicios"


def _sync_day_page_body(day_num: int) -> None:
    """
    Re-escribe los blocks de "ejercicios" en el body de la page Día N del
    Training Plan, usando el contenido actual del sub-DB del día.

    Idempotente. Solo modifica los blocks después del heading "📋 Ejercicios"
    — preserva cualquier contenido que el usuario haya agregado arriba.
    """
    # 1) Encontrar la page del Día en el Training Plan
    plan_id = nc.plan_db_id()
    if not plan_id:
        return
    plan_rows = nc.query_db(plan_id, page_size=20)
    page_id = None
    import re
    for row in plan_rows:
        p = nc.read_page_props(row)
        m = re.search(r"\d+", p.get("day_label") or "")
        if m and int(m.group(0)) == day_num:
            page_id = row["id"]
            break
    if not page_id:
        log.warning("No encontré page del Día %d en el plan", day_num)
        return

    # 2) Traer los ejercicios actuales del sub-DB
    ds_id = nc.day_subdb_id(day_num)
    if not ds_id:
        return
    rows = nc.query_db(ds_id,
        sorts=[{"property": "last_updated", "direction": "ascending"}],
        page_size=100,
    )

    # 3) Listar blocks actuales del page, encontrar el heading "📋 Ejercicios"
    children = nc._sdk().blocks.children.list(block_id=page_id, page_size=100)
    blocks = children.get("results", [])

    header_block_id = None
    blocks_to_delete = []
    found_header = False
    for b in blocks:
        if not found_header:
            if b.get("type") == "heading_2":
                text = "".join(t.get("plain_text", "") for t in
                               (b.get("heading_2") or {}).get("rich_text", []))
                if _EXERCISES_HEADER_TEXT in text:
                    header_block_id = b["id"]
                    found_header = True
        else:
            blocks_to_delete.append(b["id"])

    # 4) Si no existe el header, crearlo (al final de la page)
    if not header_block_id:
        try:
            nc._sdk().blocks.children.append(block_id=page_id, children=[{
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text",
                                    "text": {"content": _EXERCISES_HEADER_TEXT}}],
                },
            }])
        except Exception as exc:
            log.warning("No pude crear header en Día %d: %s", day_num, exc)
            return

    # 5) Borrar blocks viejos después del header
    for block_id in blocks_to_delete:
        try:
            nc._sdk().blocks.delete(block_id=block_id)
        except Exception as exc:
            log.debug("No pude borrar block %s: %s", block_id[:8], exc)

    # 6) Agregar blocks nuevos con los ejercicios actuales
    new_blocks = []
    for r in rows:
        p = nc.read_page_props(r)
        text = _format_exercise_line(p)
        if not text:
            continue
        new_blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": text}}],
            },
        })
    if new_blocks:
        try:
            nc._sdk().blocks.children.append(block_id=page_id, children=new_blocks)
            log.info("Sync body Día %d: %d ejercicios escritos", day_num, len(new_blocks))
        except Exception as exc:
            log.warning("No pude escribir ejercicios en Día %d: %s", day_num, exc)


def _format_exercise_line(p: dict) -> str:
    """Formatea un ejercicio como línea de texto para el body de la page.

    Formato: 'ejercicio  sets×reps  peso kg  rirN  — nota'
    La nota va al final separada por em-dash si existe.
    """
    exercise = p.get("exercise") or ""
    if not exercise:
        return ""
    parts = [exercise]
    sets = p.get("sets")
    reps = p.get("reps")
    if sets and reps:
        parts.append(f"{int(sets)}x{int(reps)}")
    elif sets:
        parts.append(f"{int(sets)} series")
    elif reps:
        parts.append(f"{int(reps)} reps")
    weight = p.get("weight_kg")
    if weight is not None:
        if weight == int(weight):
            parts.append(f"{int(weight)} kg")
        else:
            parts.append(f"{weight} kg")
    rir = p.get("rir")
    if rir is not None:
        parts.append(f"rir{int(rir)}")
    line = " ".join(parts)
    notes = (p.get("notes") or "").strip()
    if notes:
        line += f"  — {notes}"
    return line


def _find_existing_exercise(day_ds_id: str, canonical_name: str) -> tuple[str | None, dict | None]:
    """
    Busca si un ejercicio ya existe en el sub-DB del día.
    Devuelve (page_id_existente, dict_con_valores_anteriores) o (None, None).
    """
    rows = nc.query_db(day_ds_id, filter_={
        "property": "exercise", "title": {"equals": canonical_name},
    }, page_size=1)
    if not rows:
        return None, None
    row = rows[0]
    p = nc.read_page_props(row)
    return row["id"], {
        "sets": p.get("sets"),
        "reps": p.get("reps"),
        "weight_kg": p.get("weight_kg"),
        "rir": p.get("rir"),
        "last_updated": p.get("last_updated"),
    }


# ── Cardio ──────────────────────────────────────────────────────────────────

def log_cardio_session(
    result: CardioParseResult,
    *,
    voice_note_sid: str | None = None,
) -> dict[str, Any]:
    """Loguea una sesión de cardio manual. Si ya hay una de Apple en ±15min, mergea."""
    if not result.is_cardio:
        return {"logged": 0, "reason": "not_cardio"}

    db_id = nc.cardio_db_id()
    if not db_id:
        return {"logged": 0, "reason": "no_db"}

    # Dedup contra Apple Health: si hay sesión del mismo sport hoy, mergeamos.
    existing_apple = _find_apple_match(db_id, result.sport, result.resolved_date)
    if existing_apple:
        log.info("Mergeando con sesión Apple existente (page=%s)", existing_apple["_id"][:8])
        properties = _props_for_merge_with_manual(result, existing_apple)
        page = nc.update_page(existing_apple["_id"], properties)
        return {"logged": 1, "merged_with_apple": True, "page_id": existing_apple["_id"]}

    properties = _props_for_new_cardio(result, source="manual", voice_note_sid=voice_note_sid)
    page = nc.create_page_in_db(db_id, properties)
    if not page:
        return {"logged": 0, "reason": "notion_failed"}
    return {"logged": 1, "page_id": page.get("id"), "source": "manual"}


def log_apple_cardio_batch(workouts: list[dict]) -> dict[str, Any]:
    """
    Bulk insert de workouts del Apple Watch (vía HAE).
    Dedup por apple_workout_uuid para idempotencia.
    """
    db_id = nc.cardio_db_id()
    if not db_id:
        return {"logged": 0, "reason": "no_db"}

    inserted = 0
    skipped = 0
    for w in workouts:
        uuid_val = w.get("apple_workout_uuid")
        if uuid_val and _apple_uuid_exists(db_id, uuid_val):
            skipped += 1
            continue
        properties = _props_for_apple_workout(w)
        page = nc.create_page_in_db(db_id, properties)
        if page:
            inserted += 1
    return {"logged": inserted, "skipped": skipped, "source": "apple_health"}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _find_apple_match(db_id: str, sport: str, target_date: date | None) -> dict | None:
    """Busca una sesión de apple_health del mismo sport y misma fecha."""
    if not target_date:
        return None
    rows = nc.query_db(db_id, filter_={
        "and": [
            {"property": "sport", "title": {"equals": sport}},
            {"property": "source", "select": {"equals": "apple_health"}},
            {"property": "date", "date": {"equals": target_date.isoformat()}},
        ],
    }, page_size=1)
    if not rows:
        return None
    return nc.read_page_props(rows[0])


def _apple_uuid_exists(db_id: str, apple_uuid: str) -> bool:
    rows = nc.query_db(db_id, filter_={
        "property": "apple_workout_uuid", "rich_text": {"equals": apple_uuid},
    }, page_size=1)
    return bool(rows)


def _props_for_new_cardio(result: CardioParseResult, *, source: str,
                           voice_note_sid: str | None = None) -> dict:
    return {
        "sport":        nc.prop_title(result.sport or "otro"),
        "date":         nc.prop_date(result.resolved_date.isoformat() if result.resolved_date else None),
        "duration_min": nc.prop_number(result.duration_min),
        "distance_km":  nc.prop_number(result.distance_km),
        "intensity":    nc.prop_select(result.intensity),
        "rpe":          nc.prop_number(result.rpe),
        "notes":        nc.prop_text(result.notes),
        "source":       nc.prop_select(source),
    }


def _props_for_merge_with_manual(result: CardioParseResult, existing: dict) -> dict:
    """Cuando Mateo loguea manual una sesión que Apple ya tenía, mergeamos
    los campos manuales (RPE subjetivo, notas) sin pisar los del watch."""
    merged_notes = "; ".join(filter(None, [existing.get("notes"), result.notes]))
    return {
        "rpe":          nc.prop_number(result.rpe or existing.get("rpe")),
        "intensity":    nc.prop_select(result.intensity or existing.get("intensity")),
        "notes":        nc.prop_text(merged_notes or ""),
    }


def _props_for_apple_workout(w: dict) -> dict:
    """Maps a workout dict from HAE parser to Notion properties."""
    return {
        "sport":              nc.prop_title(w.get("sport") or "otro"),
        "date":               nc.prop_date(w.get("date")),
        "duration_min":       nc.prop_number(w.get("duration_min")),
        "distance_km":        nc.prop_number(w.get("distance_km")),
        "avg_hr":             nc.prop_number(w.get("avg_hr")),
        "max_hr":             nc.prop_number(w.get("max_hr")),
        "pace_min_per_km":    nc.prop_number(w.get("pace_min_per_km")),
        "calories":           nc.prop_number(w.get("calories")),
        "intensity":          nc.prop_select(_derive_intensity(w)),
        "source":             nc.prop_select("apple_health"),
        "apple_workout_uuid": nc.prop_text(w.get("apple_workout_uuid") or ""),
        "notes":              nc.prop_text(w.get("notes") or ""),
    }


def _derive_intensity(w: dict) -> str | None:
    """Deriva intensity de avg_hr si está disponible (zonas aproximadas)."""
    hr = w.get("avg_hr")
    if hr is None:
        return None
    # Aproximación simplificada (zonas relativas a max ~190 para adulto ~30)
    if hr < 120:   return "suave"
    if hr < 150:   return "moderada"
    if hr < 175:   return "intensa"
    return "intervalos"


# ── Training plan ───────────────────────────────────────────────────────────

def upsert_training_day(day_label: str, muscle_groups: list[str],
                         suggested_exercises: str = "", cardio: bool = False,
                         notes: str = "") -> bool:
    """Crea o actualiza un día del plan. Idempotente por day_label."""
    db_id = nc.plan_db_id()
    if not db_id:
        return False

    existing = nc.query_db(db_id, filter_={
        "property": "day_label", "title": {"equals": day_label},
    }, page_size=1)

    properties = {
        "day_label":           nc.prop_title(day_label),
        "muscle_groups":       nc.prop_multi_select(muscle_groups),
        "suggested_exercises": nc.prop_text(suggested_exercises),
        "cardio":              nc.prop_checkbox(cardio),
        "notes":               nc.prop_text(notes),
        "active":              nc.prop_checkbox(True),
    }
    if existing:
        return bool(nc.update_page(existing[0]["id"], properties))
    return bool(nc.create_page_in_db(db_id, properties))
