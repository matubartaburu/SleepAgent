"""Tests del parser HAE de workouts.

Los fixtures replican el schema real de Health Auto Export v2:
- duration: número plano en SEGUNDOS
- distance / activeEnergyBurned / avgHeartRate: objetos {qty, units}
- name: localizado al idioma del iPhone (español por default acá)
"""

from __future__ import annotations

from hae_workouts_parser import parse_workouts, _detect_sport


def _payload(workouts: list[dict]) -> dict:
    return {"data": {"workouts": workouts}}


def test_running_workout_hae_v2():
    rows = parse_workouts(_payload([{
        "name": "Aire Libre Correr",
        "id":   "AC5CF021-7BEA-4ECD-9AE4-B2F59F4F8F3A",
        "start": "2026-05-17 15:37:29 -0300",
        "end":   "2026-05-17 16:40:58 -0300",
        "duration": 3809.65,
        "distance":           {"qty": 9.82, "units": "km"},
        "avgHeartRate":       {"qty": 168.92, "units": "bpm"},
        "maxHeartRate":       {"qty": 179, "units": "bpm"},
        "activeEnergyBurned": {"qty": 2951.36, "units": "kJ"},
    }]))
    assert len(rows) == 1
    w = rows[0]
    assert w["sport"] == "running"
    assert w["date"] == "2026-05-17"
    assert 63.4 <= w["duration_min"] <= 63.5
    assert w["distance_km"] == 9.82
    assert w["avg_hr"] == 168.92
    assert w["max_hr"] == 179
    # 2951.36 kJ / 4.184 ≈ 705.4 kcal
    assert 704 <= w["calories"] <= 707
    assert w["pace_min_per_km"] is not None
    assert 6.4 <= w["pace_min_per_km"] <= 6.5
    assert w["apple_workout_uuid"] == "AC5CF021-7BEA-4ECD-9AE4-B2F59F4F8F3A"


def test_calories_in_kcal_no_conversion():
    rows = parse_workouts(_payload([{
        "name": "Aire Libre Correr",
        "start": "2026-05-15 07:00:00 -0300",
        "end":   "2026-05-15 07:30:00 -0300",
        "duration": 1800,
        "activeEnergyBurned": {"qty": 250.5, "units": "kcal"},
    }]))
    assert rows[0]["calories"] == 250.5


def test_hr_falls_back_to_nested_structure():
    rows = parse_workouts(_payload([{
        "name": "Aire Libre Correr",
        "start": "2026-05-15 07:00:00 -0300",
        "end":   "2026-05-15 07:30:00 -0300",
        "duration": 1800,
        "heartRate": {
            "avg": {"qty": 150, "units": "bpm"},
            "max": {"qty": 175, "units": "bpm"},
        },
    }]))
    assert rows[0]["avg_hr"] == 150
    assert rows[0]["max_hr"] == 175


def test_workout_in_metrics_format():
    payload = {"data": {"metrics": [
        {"name": "workouts", "data": [{
            "name": "Bicicleta Aire Libre",
            "start": "2026-05-14 18:00:00 -0300",
            "end":   "2026-05-14 18:30:00 -0300",
            "duration": 1800,
            "distance": {"qty": 15.5, "units": "km"},
            "id": "X",
        }]}
    ]}}
    rows = parse_workouts(payload)
    assert len(rows) == 1
    assert rows[0]["sport"] == "cycling"
    assert rows[0]["distance_km"] == 15.5


def test_unknown_sport_falls_to_otro():
    rows = parse_workouts(_payload([{
        "name": "Pickleball",
        "start": "2026-05-15 10:00:00 -0300",
        "end":   "2026-05-15 11:00:00 -0300",
        "duration": 3600,
    }]))
    assert rows[0]["sport"] == "otro"


def test_distance_in_miles_converts_to_km():
    rows = parse_workouts(_payload([{
        "name": "Aire Libre Correr",
        "start": "2026-05-15 07:00:00 -0300",
        "end":   "2026-05-15 07:30:00 -0300",
        "duration": 1800,
        "distance": {"qty": 5, "units": "mi"},
    }]))
    assert 8.0 < rows[0]["distance_km"] < 8.1


def test_distance_in_meters_converts_to_km():
    rows = parse_workouts(_payload([{
        "name": "Aire Libre Correr",
        "start": "2026-05-15 07:00:00 -0300",
        "end":   "2026-05-15 07:30:00 -0300",
        "duration": 1800,
        "distance": {"qty": 5000, "units": "m"},
    }]))
    assert rows[0]["distance_km"] == 5.0


def test_no_workouts_returns_empty():
    assert parse_workouts({}) == []
    assert parse_workouts({"data": {}}) == []
    assert parse_workouts({"data": {"metrics": [
        {"name": "sleep_analysis", "data": []}
    ]}}) == []


def test_duration_from_start_end_if_missing():
    rows = parse_workouts(_payload([{
        "name": "Caminar Aire Libre",
        "start": "2026-05-15 12:00:00 -0300",
        "end":   "2026-05-15 12:25:00 -0300",
        "id": "Y",
    }]))
    assert rows[0]["duration_min"] == 25


def test_pace_calculated_when_distance_and_duration_present():
    rows = parse_workouts(_payload([{
        "name": "Aire Libre Correr",
        "start": "2026-05-15 07:00:00 -0300",
        "end":   "2026-05-15 08:00:00 -0300",
        "duration": 3600,
        "distance": {"qty": 10, "units": "km"},
    }]))
    assert rows[0]["pace_min_per_km"] == 6.0


def test_sport_detection_handles_localized_and_english_names():
    cases = {
        "Aire Libre Correr": "running",
        "Outdoor Run":       "running",
        "Indoor Run":        "running",
        "Cinta Correr":      "running",
        "Caminar Aire Libre": "walking",
        "Walking":           "walking",
        "Bicicleta Aire Libre": "cycling",
        "Cycling":           "cycling",
        "Yoga":              "yoga",
        "HIIT":              "hiit",
        "Tenis":             "tenis",
        "Fútbol":            "futbol",
        "Escalada":          "escalada",
        "Natación":          "swimming",
        "Swimming":          "swimming",
        "Fuerza Tradicional": "otro",
        "":                  "otro",
    }
    for name, expected in cases.items():
        assert _detect_sport(name) == expected, f"{name} -> {_detect_sport(name)}"
