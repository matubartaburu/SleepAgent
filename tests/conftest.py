"""
conftest.py — fixtures compartidos.

- `sample_payload`: payload de HAE de ejemplo con 1 noche completa.
- `client`: FastAPI TestClient con INGEST_SECRET seteado.
- `frozen_today`: monkeypatcha date.today() en report._build_context.
"""

from __future__ import annotations

import os
from datetime import date

import pytest


# Setear vars antes de importar la app (config.py se carga al importar)
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")
os.environ.setdefault("INGEST_SECRET", "test-secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
os.environ.setdefault("MY_PHONE", "+59891000000")


@pytest.fixture
def sample_payload() -> dict:
    """Payload HAE con 1 noche + métricas asociadas."""
    return {
        "data": {
            "metrics": [
                {
                    "name": "sleep_analysis",
                    "units": "hr",
                    "data": [{
                        "date": "2026-05-15 08:00:00 -0300",
                        "sleepStart": "2026-05-15 00:30:00 -0300",
                        "sleepEnd":   "2026-05-15 07:42:00 -0300",
                        "inBedStart": "2026-05-15 00:15:00 -0300",
                        "inBedEnd":   "2026-05-15 07:50:00 -0300",
                        "totalSleep": 7.20,
                        "inBed":      0,
                        "asleep":     0,
                        "rem":   1.45,
                        "core":  4.10,
                        "deep":  1.10,
                        "awake": 0.55,
                    }],
                },
                {
                    "name": "heart_rate_variability",
                    "units": "ms",
                    "data": [{"date": "2026-05-15 00:00:00 -0300", "qty": 58.3}],
                },
                {
                    "name": "resting_heart_rate",
                    "units": "count/min",
                    "data": [{"date": "2026-05-15 00:00:00 -0300", "qty": 52}],
                },
                {
                    "name": "heart_rate",
                    "units": "count/min",
                    "data": [{"date": "2026-05-15 00:00:00 -0300",
                              "Avg": 61.4, "Min": 48, "Max": 88}],
                },
                {
                    "name": "respiratory_rate",
                    "units": "count/min",
                    "data": [{"date": "2026-05-15 00:00:00 -0300", "qty": 14.2}],
                },
            ]
        }
    }


@pytest.fixture
def sample_payload_two_nights(sample_payload) -> dict:
    """Variante con 2 noches para chequear que se generan 2 filas."""
    out = {"data": {"metrics": [dict(m) for m in sample_payload["data"]["metrics"]]}}
    for m in out["data"]["metrics"]:
        # duplicamos cada item con fecha -1 día
        original = m["data"][0]
        extra = dict(original)
        extra["date"] = original["date"].replace("2026-05-15", "2026-05-14")
        if "sleepStart" in extra:
            extra["sleepStart"] = original["sleepStart"].replace("2026-05-15", "2026-05-14")
            extra["sleepEnd"]   = original["sleepEnd"].replace("2026-05-15", "2026-05-14")
            extra["inBedStart"] = original["inBedStart"].replace("2026-05-15", "2026-05-14")
            extra["inBedEnd"]   = original["inBedEnd"].replace("2026-05-15", "2026-05-14")
        m["data"] = [original, extra]
    return out
