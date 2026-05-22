"""
hae_workouts_parser.py — extrae sesiones de cardio (workouts del Apple
Watch) del payload de Health Auto Export.

HAE v2 puede mandar workouts en 2 lugares del payload:
1. `data.workouts` (top-level array) → formato v2 actual
2. Como métrica adicional `workouts` dentro de `data.metrics` (legacy)

HAE v2 expresa la mayoría de campos como objetos {qty, units}:
    "distance":            {"qty": 9.82, "units": "km"}
    "activeEnergyBurned":  {"qty": 2951.35, "units": "kJ"}
    "avgHeartRate":        {"qty": 168.9, "units": "bpm"}
    "duration":            3809.65   ← excepción: número plano en SEGUNDOS

Los nombres (`name`) llegan localizados según el idioma del iPhone
(ej. "Aire Libre Correr" en español). Por eso detectamos sport por
substring tolerante a idioma.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)


_HAE_DATETIME_FMT = "%Y-%m-%d %H:%M:%S %z"


# ── Sport detection ────────────────────────────────────────────────────────
#
# HAE manda `name` localizado al idioma del iPhone. En lugar de mapear
# strings exactos, detectamos por substring para sobrevivir a "Aire Libre
# Correr", "Outdoor Run", "Indoor Run", etc.

# Orden: el primer match gana. Por eso "correr"/"run" antes que "walking"
# para que "Aire Libre Correr" no caiga en walking si tuviera ambas.
_SPORT_RULES: list[tuple[tuple[str, ...], str]] = [
    (("correr", "running", "run "),                "running"),
    (("caminar", "walking", "walk"),               "walking"),
    (("bici", "ciclismo", "cycling", "cycle", "bike"), "cycling"),
    (("natación", "natacion", "swim"),             "swimming"),
    (("yoga",),                                    "yoga"),
    (("hiit",),                                    "hiit"),
    (("tenis", "tennis"),                          "tenis"),
    (("fútbol", "futbol", "soccer"),               "futbol"),
    (("escalada", "climb"),                        "escalada"),
    (("senderismo", "hiking", "hike"),             "hiking"),
]


def _detect_sport(name: str) -> str:
    n = (name or "").strip().lower()
    if not n:
        return "otro"
    # Asegura que "run " (con espacio) también matchee si está al final.
    n_padded = f" {n} "
    for needles, sport in _SPORT_RULES:
        for needle in needles:
            if needle in n_padded:
                return sport
    return "otro"


# ── Helpers ────────────────────────────────────────────────────────────────


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, _HAE_DATETIME_FMT)
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None


def _num(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return None


def _qty(x: Any) -> float | None:
    """HAE v2 envuelve casi todo en {qty, units}. Acepta también número plano."""
    if isinstance(x, dict):
        return _num(x.get("qty"))
    return _num(x)


def _units(x: Any) -> str | None:
    if isinstance(x, dict):
        u = x.get("units")
        return u.lower() if isinstance(u, str) else None
    return None


# ── Parser principal ──────────────────────────────────────────────────────


def parse_workouts(payload: dict) -> list[dict]:
    workouts_raw = _extract_workouts_array(payload)
    if not workouts_raw:
        return []

    rows: list[dict] = []
    for w in workouts_raw:
        parsed = _normalize_workout(w)
        if parsed:
            rows.append(parsed)
    log.info("HAE workouts parser: %d workouts extraídos", len(rows))
    return rows


def _extract_workouts_array(payload: dict) -> list[dict]:
    data = payload.get("data") or {}
    if isinstance(data.get("workouts"), list):
        return data["workouts"]
    metrics = data.get("metrics") or []
    for m in metrics:
        if isinstance(m, dict) and m.get("name") == "workouts":
            return m.get("data") or []
    return []


def _normalize_workout(w: dict) -> dict | None:
    if not isinstance(w, dict):
        return None

    raw_name = (w.get("name") or w.get("workoutType") or w.get("type")
                or w.get("activityType") or "")
    if not raw_name:
        return None
    sport = _detect_sport(raw_name)

    start = _parse_dt(w.get("start") or w.get("startDate"))
    end   = _parse_dt(w.get("end")   or w.get("endDate"))

    date_iso = start.date().isoformat() if start else None

    duration_min = _duration_min(w, start, end)
    distance_km  = _distance_km(w)
    avg_hr       = _hr(w, kind="avg")
    max_hr       = _hr(w, kind="max")
    calories     = _calories_kcal(w)
    pace         = _pace_min_per_km(duration_min, distance_km)

    return {
        "sport":              sport,
        "date":               date_iso,
        "duration_min":       duration_min,
        "distance_km":        distance_km,
        "avg_hr":             avg_hr,
        "max_hr":             max_hr,
        "pace_min_per_km":    pace,
        "calories":           calories,
        "apple_workout_uuid": w.get("uuid") or w.get("id") or "",
        "notes":              "",
    }


# ── Campo a campo ──────────────────────────────────────────────────────────


def _duration_min(w: dict, start: datetime | None, end: datetime | None) -> float | None:
    """HAE v2 manda `duration` como número plano en SEGUNDOS."""
    raw = w.get("duration")
    if raw is not None:
        # Si viene como objeto con units, respetamos units; si no, segundos.
        if isinstance(raw, dict):
            qty = _num(raw.get("qty"))
            unit = (_units(raw) or "").lower()
            if qty is None:
                return None
            if unit in {"h", "hr", "hour", "hours"}:
                return round(qty * 60, 2)
            if unit in {"min", "mins", "minute", "minutes"}:
                return round(qty, 2)
            # default y "s"/"sec"/"seconds"
            return round(qty / 60, 2)
        qty = _num(raw)
        if qty is not None:
            return round(qty / 60, 2)  # HAE v2: segundos
    if start and end:
        return round((end - start).total_seconds() / 60, 2)
    return None


def _distance_km(w: dict) -> float | None:
    raw = w.get("distance") or w.get("totalDistance")
    qty = _qty(raw)
    if qty is None:
        return None
    unit = _units(raw) or ""
    if unit in {"mi", "miles", "mile"}:
        return round(qty * 1.609344, 2)
    if unit in {"m", "meter", "meters"}:
        return round(qty / 1000, 3)
    # default km (HAE con Metric Units manda km)
    return round(qty, 2)


def _hr(w: dict, *, kind: str) -> float | None:
    """kind = 'avg' | 'max'."""
    # 1) Top-level avgHeartRate / maxHeartRate como {qty, units}
    direct_key = "avgHeartRate" if kind == "avg" else "maxHeartRate"
    val = _qty(w.get(direct_key))
    if val is not None:
        return val
    # 2) Estructura anidada heartRate.{avg|max}.qty
    hr = w.get("heartRate")
    if isinstance(hr, dict):
        nested = hr.get(kind)
        val = _qty(nested)
        if val is not None:
            return val
    return None


_KJ_PER_KCAL = 4.184


def _calories_kcal(w: dict) -> float | None:
    raw = w.get("activeEnergyBurned") or w.get("activeEnergy") or w.get("calories")
    # `activeEnergy` también aparece como lista de samples; en ese caso ignoramos
    # (preferimos el agregado de `activeEnergyBurned`).
    if isinstance(raw, list):
        return None
    qty = _qty(raw)
    if qty is None:
        return None
    unit = _units(raw) or ""
    if unit in {"kj", "kjoule", "kjoules"}:
        return round(qty / _KJ_PER_KCAL, 1)
    return round(qty, 1)  # asumir kcal


def _pace_min_per_km(duration_min: float | None, distance_km: float | None) -> float | None:
    if not duration_min or not distance_km or distance_km <= 0:
        return None
    return round(duration_min / distance_km, 2)
