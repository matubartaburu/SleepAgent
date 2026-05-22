"""
agents/workout/orchestrator.py — punto de entrada del módulo workout.

Recibe el texto de Mateo (transcrito o escrito), lo rutea al intent
correcto, ejecuta el sub-agent correspondiente y devuelve una respuesta
en lenguaje natural para WhatsApp.

Diseñado para ser llamado desde el endpoint /whatsapp/inbound de FastAPI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from agents.workout import (
    cardio_parser,
    plan_setup,
    workout_parser,
    workout_retriever,
    workout_router,
    workout_logger,
)
from agents.workout.workout_router import RouterResult

log = logging.getLogger(__name__)


@dataclass
class OrchestratorResult:
    intent: str
    reply_text: str
    actions: list[str] = field(default_factory=list)
    handled_by_workout: bool = True

    @property
    def empty_reply(self) -> bool:
        return not self.reply_text


# ── Public entry point ─────────────────────────────────────────────────────

def handle_message(text: str, *, voice_note_sid: str | None = None) -> OrchestratorResult:
    """
    Rutea el mensaje al intent correcto y ejecuta. Devuelve la respuesta
    para mandar por WhatsApp.

    Si el intent es 'sleep_question' o similar (no-workout), devuelve
    handled_by_workout=False para que el caller delegue al answerer
    de sleep.
    """
    if not text or not text.strip():
        return OrchestratorResult(intent="other", reply_text="",
                                   handled_by_workout=False)

    routed = workout_router.route(text)
    intent = routed.intent
    log.info("workout_orchestrator: intent=%s confidence=%.2f", intent, routed.confidence)

    if intent == "log_workout":
        return _handle_log_workout(text, voice_note_sid)
    if intent == "correction":
        # Una corrección es funcionalmente un log nuevo: el upsert del
        # logger actualiza idempotentemente el ejercicio existente. El
        # parser de Sonnet entiende texto conversacional como
        # "press banca fueron 4 reps no 5".
        return _handle_log_workout(text, voice_note_sid, is_correction=True)
    if intent == "log_cardio":
        return _handle_log_cardio(text, voice_note_sid)
    if intent == "setup_plan":
        return _handle_setup_plan(text)
    if intent == "edit_plan":
        return _handle_edit_plan(text)
    if intent == "retrieve_workout":
        return _handle_retrieve_workout(routed)
    if intent == "retrieve_running":
        return _handle_retrieve_running()
    if intent == "day_brief":
        return _handle_day_brief(routed.muscles or [])
    if intent == "next_day":
        return _handle_next_day()
    if intent == "correction":
        return _handle_correction(text)
    if intent in {"sleep_question", "cross_domain"}:
        # No es del workout module, delegar al answerer de sleep
        return OrchestratorResult(intent=intent, reply_text="", handled_by_workout=False)

    # ── Fallback inteligente ────────────────────────────────────────────────
    # Si el router dudó (intent=other) pero el mensaje menciona workout +
    # tiene números, asumimos que es un LOG informal y dejamos que Sonnet
    # (workout_parser) haga lo que pueda. Mejor intentar y fallar gracefully
    # que rechazar de entrada.
    if _mentions_workout(text) and _has_numbers(text):
        log.info("Router dudó (intent=other) pero el mensaje parece workout — intento parsear")
        result = _handle_log_workout(text, voice_note_sid)
        # Si el parser tampoco encontró nada, ahí sí damos el hint
        if "no te entendí" in result.reply_text.lower() or "reintentá" in result.reply_text.lower():
            return OrchestratorResult(
                intent="workout_hint",
                reply_text=(
                    "Te entendí que estabas hablando del gym pero no pude sacar los datos. "
                    "Probá algo más explícito tipo:\n"
                    "'press banca cuatro series de cuatro con ochenta kilos'"
                ),
                handled_by_workout=True,
            )
        return result

    # Mensaje que NO tiene contexto de workout — delegar al answerer
    return OrchestratorResult(intent="other", reply_text="", handled_by_workout=False)


def _has_numbers(text: str) -> bool:
    """Detecta si un texto tiene números (dígitos o palabras como 'cuatro')."""
    import re
    if re.search(r"\d", text):
        return True
    spelled = {
        "cero", "uno", "una", "dos", "tres", "cuatro", "cinco", "seis", "siete",
        "ocho", "nueve", "diez", "once", "doce", "trece", "catorce", "quince",
        "veinte", "treinta", "cuarenta", "cincuenta", "sesenta", "setenta",
        "ochenta", "noventa", "cien", "ciento", "doscientos",
    }
    lower = text.lower()
    return any(w in lower for w in spelled)


# Keywords que sugieren contexto de gym/entrenamiento aunque el router no
# haya detectado un intent específico.
_WORKOUT_KEYWORDS = {
    "gym", "gimnasio", "entreno", "entrenamiento", "pesos", "peso", "musculacion",
    "musculación", "ejercicio", "ejercicios", "series", "repeticiones", "reps",
    "press", "sentadilla", "sentadillas", "peso muerto", "jalon", "jalón",
    "remo", "curl", "biceps", "bíceps", "triceps", "tríceps",
    "hombros", "espalda", "pecho", "piernas", "gluteos", "glúteos",
    "deadlift", "squat", "bench", "rep", "set", "rpe",
}


def _mentions_workout(text: str) -> bool:
    """Detecta si un texto menciona algo de workout aunque no haya intent claro."""
    lower = text.lower()
    return any(kw in lower for kw in _WORKOUT_KEYWORDS)


# ── Intent handlers ────────────────────────────────────────────────────────

def _handle_log_workout(text: str, voice_note_sid: str | None,
                          *, is_correction: bool = False) -> OrchestratorResult:
    parsed = workout_parser.parse(text)
    if not parsed.has_exercises:
        msg = ("No te entendí la corrección. Decime el ejercicio y el dato nuevo, "
                "tipo 'press banca fueron 4 reps no 5'." if is_correction else
                "No te entendí qué entrenaste. ¿Me lo dictás de nuevo?")
        return OrchestratorResult(
            intent="correction" if is_correction else "log_workout",
            reply_text=msg,
        )
    log_result = workout_logger.log_workout_session(parsed, voice_note_sid=voice_note_sid)
    reply = _format_workout_log_reply(log_result, parsed.resolved_date,
                                        is_correction=is_correction)
    return OrchestratorResult(
        intent="correction" if is_correction else "log_workout",
        reply_text=reply,
        actions=[f"logged_{log_result.get('logged', 0)}_exercises"],
    )


def _handle_log_cardio(text: str, voice_note_sid: str | None) -> OrchestratorResult:
    parsed = cardio_parser.parse(text)
    if not parsed.is_cardio:
        return OrchestratorResult(
            intent="log_cardio",
            reply_text="No me quedó claro qué cardio hiciste. ¿Repetimelo?",
        )
    log_result = workout_logger.log_cardio_session(parsed, voice_note_sid=voice_note_sid)
    reply = _format_cardio_log_reply(parsed, log_result)
    return OrchestratorResult(intent="log_cardio", reply_text=reply,
                              actions=[f"cardio_{log_result.get('logged', 0)}"])


def _handle_setup_plan(text: str) -> OrchestratorResult:
    parsed = plan_setup.parse(text)
    if parsed.mode != "setup" or not parsed.days:
        return OrchestratorResult(
            intent="setup_plan",
            reply_text="No te entendí el split. Decime tipo 'día 1 pecho y hombro, día 2 espalda y brazos, día 3 piernas'.",
        )
    saved = 0
    for d in parsed.days:
        if workout_logger.upsert_training_day(
            d.day_label, d.muscle_groups, d.suggested_exercises, d.cardio, d.notes,
        ):
            saved += 1
    reply = "Plan guardado:\n" + "\n".join(
        f"• {d.day_label}: {', '.join(d.muscle_groups) or 'sin músculos'}"
        for d in parsed.days
    )
    return OrchestratorResult(intent="setup_plan", reply_text=reply,
                              actions=[f"plan_saved_{saved}_days"])


def _handle_edit_plan(text: str) -> OrchestratorResult:
    parsed = plan_setup.parse(text)
    if parsed.mode != "edit_day" or not parsed.day_label:
        return OrchestratorResult(
            intent="edit_plan",
            reply_text="No entendí el cambio. Probá 'agregale gemelos al día 3' o similar.",
        )
    # Edits simples: solo soportamos add_muscle por ahora — el resto los suma con
    # operaciones más complejas que dejo para v2.
    return OrchestratorResult(
        intent="edit_plan",
        reply_text=f"Ok, ajusto {parsed.day_label}: {parsed.operation} → {parsed.value}",
        actions=[f"plan_edit_{parsed.operation}"],
    )


def _handle_retrieve_workout(routed: RouterResult) -> OrchestratorResult:
    if routed.muscle_group:
        r = workout_retriever.last_session_by_muscle(routed.muscle_group)
        return OrchestratorResult(
            intent="retrieve_workout",
            reply_text=_format_last_session_reply(r),
        )
    if routed.exercise:
        r = workout_retriever.last_session_by_exercise(routed.exercise)
        return OrchestratorResult(
            intent="retrieve_workout",
            reply_text=_format_last_exercise_reply(r),
        )
    return OrchestratorResult(
        intent="retrieve_workout",
        reply_text="¿De qué músculo o ejercicio querés saber? Decime 'última espalda' o 'última sentadilla'.",
    )


def _handle_retrieve_running() -> OrchestratorResult:
    r = workout_retriever.last_running()
    return OrchestratorResult(
        intent="retrieve_running",
        reply_text=_format_last_running_reply(r),
    )


def _handle_day_brief(muscles: list[str]) -> OrchestratorResult:
    """
    "Hoy entreno pecho y hombros" → trae los ejercicios del día que matchea
    con esos músculos en el plan.
    """
    if not muscles:
        return OrchestratorResult(
            intent="day_brief",
            reply_text="¿Qué músculos te tocan hoy? Decime tipo 'hoy entreno pecho y hombros'.",
        )
    r = workout_retriever.exercises_for_muscles(muscles)
    return OrchestratorResult(
        intent="day_brief",
        reply_text=_format_day_brief_reply(r, muscles),
    )


def _handle_next_day() -> OrchestratorResult:
    r = workout_retriever.next_day_suggestion()
    if not r.get("found"):
        return OrchestratorResult(
            intent="next_day",
            reply_text="Todavía no tengo plan armado. Decime cómo es tu split.",
        )
    muscles = ", ".join(r.get("muscle_groups", []))
    label = r.get("next_day_label", "")
    extra = f"\nSugerencias: {r.get('suggested_exercises', '')}" if r.get("suggested_exercises") else ""
    return OrchestratorResult(
        intent="next_day",
        reply_text=f"Hoy te toca {label}: {muscles}.{extra}",
    )


# Nota: el handler de correction reusa _handle_log_workout con is_correction=True.
# El upsert del logger se encarga de actualizar el ejercicio existente.


# ── Reply formatters ────────────────────────────────────────────────────────

def _format_workout_log_reply(log_result: dict, resolved_date,
                                *, is_correction: bool = False) -> str:
    exercises = log_result.get("exercises", [])
    skipped = log_result.get("skipped_exercises", [])

    if not exercises and not skipped:
        return "No pude guardar nada. Reintentá."

    lines = []
    if exercises:
        # Agrupar por día
        by_day: dict[int, list[dict]] = {}
        for e in exercises:
            by_day.setdefault(e.get("day_num") or 0, []).append(e)

        verb_count = len(exercises)
        if is_correction:
            actions = {e.get("action") for e in exercises}
            verb = "Corregido" if "updated" in actions else "Anotado"
        else:
            verb = "Anotado"
        lines.append(f"{verb}, {verb_count} ejercicio{'s' if verb_count > 1 else ''}:")
        for day_num in sorted(by_day):
            day_exercises = by_day[day_num]
            if day_num:
                lines.append(f"\n  Día {day_num}:")
            for e in day_exercises:
                ex_line = f"  • {e['exercise']}"
                if e.get("sets") and e.get("reps"):
                    ex_line += f" {e['sets']}x{e['reps']}"
                elif e.get("sets"):
                    ex_line += f" {e['sets']} series"
                elif e.get("reps"):
                    ex_line += f" {e['reps']} reps"
                if e.get("weight_kg"):
                    ex_line += f" {e['weight_kg']}kg"
                if e.get("rir") is not None:
                    ex_line += f" rir{e['rir']}"

                # En correcciones: mostrar QUÉ cambió específicamente
                changed = e.get("changed_fields") or []
                if is_correction and changed:
                    ex_line += f"  [cambio: {', '.join(changed)}]"
                elif not is_correction:
                    # En logs nuevos sobre un ejercicio existente, mostrar diff de peso
                    prev = e.get("previous") or {}
                    if prev.get("weight_kg") and e.get("weight_kg"):
                        diff = e["weight_kg"] - prev["weight_kg"]
                        if diff > 0:
                            ex_line += f" (+{diff:g}kg vs anterior)"
                        elif diff < 0:
                            ex_line += f" ({diff:g}kg vs anterior)"

                # Mostrar la nota si la tiene
                note = (e.get("notes") or "").strip()
                if note:
                    ex_line += f"  — {note}"
                lines.append(ex_line)

    if skipped:
        lines.append("\nNo pude asociar a un día del plan:")
        for s in skipped:
            lines.append(f"  • {s['exercise']} (músculos: {', '.join(s.get('muscle_groups') or [])})")
        lines.append("Si querés, ajustá el plan o decime el día explícito.")

    return "\n".join(lines)


def _format_cardio_log_reply(parsed, log_result: dict) -> str:
    if log_result.get("merged_with_apple"):
        return f"Ya tenía esa sesión registrada por el watch. Sumé tus notas y RPE."
    parts = [f"Cardio anotado: {parsed.sport}"]
    if parsed.distance_km:
        parts.append(f"{parsed.distance_km}km")
    if parsed.duration_min:
        parts.append(f"{int(parsed.duration_min)}min")
    if parsed.intensity:
        parts.append(parsed.intensity)
    return " · ".join(parts) + "."


def _format_last_session_reply(r: dict) -> str:
    if not r.get("found"):
        return f"No tengo registros de {r.get('muscle_group') or 'ese grupo'} todavía."
    days = r.get("days_ago")
    when = "ayer" if days == 1 else f"hace {days} días" if days else "hoy"
    lines = [f"Última {r.get('muscle_group') or 'sesión'} fue {when} ({r.get('date')}):"]
    for ex in r.get("exercises", []):
        line = f"• {ex.get('exercise')}"
        if ex.get("sets") and ex.get("reps"):
            line += f" {ex['sets']}x{ex['reps']}"
        elif ex.get("sets"):
            line += f" {ex['sets']} series"
        elif ex.get("reps"):
            line += f" {ex['reps']} reps"
        if ex.get("weight_kg"):
            line += f" {ex['weight_kg']}kg"
        lines.append(line)
    return "\n".join(lines)


def _format_last_exercise_reply(r: dict) -> str:
    if not r.get("found"):
        return f"No tengo registros de ese ejercicio todavía."
    days = r.get("days_ago")
    when = "ayer" if days == 1 else f"hace {days} días" if days else "hoy"
    line = f"Última vez ({when}, {r.get('date')}): {r.get('exercise')}"
    if r.get("sets") and r.get("reps"):
        line += f" {r['sets']}x{r['reps']}"
    elif r.get("sets"):
        line += f" {r['sets']} series"
    elif r.get("reps"):
        line += f" {r['reps']} reps"
    if r.get("weight_kg"):
        line += f" {r['weight_kg']}kg"
    if r.get("rir"):
        line += f" rir{r['rir']}"
    return line


def _format_day_brief_reply(r: dict, muscles: list[str]) -> str:
    if not r.get("found"):
        return (f"Tu plan no tiene un día con {', '.join(muscles)}. "
                "¿Querés ajustarlo o cargarlo igual?")
    day_num = r.get("day_num")
    exercises = r.get("exercises") or []
    if not exercises:
        return (f"Día {day_num} ({', '.join(muscles)}) — todavía no tenés ejercicios registrados. "
                "Mandame los pesos para ir armando el historial.")
    lines = [f"Día {day_num}, lo último que tenés cargado:"]
    for e in exercises:
        line = f"• {e['exercise']}"
        if e.get("sets") and e.get("reps"):
            line += f" {e['sets']}x{e['reps']}"
        elif e.get("sets"):
            line += f" {e['sets']} series"
        elif e.get("reps"):
            line += f" {e['reps']} reps"
        if e.get("weight_kg"):
            line += f" {e['weight_kg']}kg"
        if e.get("rir"):
            line += f" rir{e['rir']}"
        if e.get("last_updated"):
            line += f"  ({e['last_updated']})"
        lines.append(line)
    return "\n".join(lines)


def _format_last_running_reply(r: dict) -> str:
    if not r.get("found"):
        return "No tengo corridas registradas todavía."
    days = r.get("days_ago")
    when = "ayer" if days == 1 else f"hace {days} días" if days else "hoy"
    parts = [f"Última corrida {when}"]
    if r.get("distance_km"):
        parts.append(f"{r['distance_km']}km")
    if r.get("duration_min"):
        parts.append(f"{int(r['duration_min'])}min")
    if r.get("pace_min_per_km"):
        m = int(r["pace_min_per_km"])
        s = int((r["pace_min_per_km"] - m) * 60)
        parts.append(f"pace {m}:{s:02d}")
    if r.get("avg_hr"):
        parts.append(f"HR avg {int(r['avg_hr'])}")
    return ", ".join(parts) + "."
