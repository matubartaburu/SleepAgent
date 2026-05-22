"""
Tests del endpoint POST /sleep.

Mockean upsert_sleep_logs para no tocar Supabase (la auth y el parsing
viven en main.py, los unit tests del parser ya cubren la conversión).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from main import app
    return TestClient(app)


def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["agent"] == "Oscar"


def test_post_sleep_unauthorized_without_header(client, sample_payload):
    r = client.post("/sleep", json=sample_payload)
    assert r.status_code == 401


def test_post_sleep_unauthorized_with_wrong_header(client, sample_payload):
    r = client.post("/sleep",
                    headers={"X-Ingest-Secret": "wrong"},
                    json=sample_payload)
    assert r.status_code == 401


def test_post_sleep_ok(client, sample_payload):
    with patch("main.upsert_sleep_logs") as mock_save:
        mock_save.return_value = [{"night_date": "2026-05-15"}]
        r = client.post("/sleep",
                        headers={"X-Ingest-Secret": "test-secret"},
                        json=sample_payload)
        assert r.status_code == 200
        body = r.json()
        assert body["nights"] == 1
        assert body["night_dates"] == ["2026-05-15"]
        mock_save.assert_called_once()


def test_post_sleep_empty_payload(client):
    """Payload sin sleep_analysis → 0 noches, sigue siendo 200."""
    with patch("main.upsert_sleep_logs") as mock_save:
        r = client.post("/sleep",
                        headers={"X-Ingest-Secret": "test-secret"},
                        json={"data": {"metrics": []}})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["nights"] == 0
        assert body["night_dates"] == []
        # apple_workouts ahora siempre se reporta (puede ser 0 o más)
        assert "apple_workouts" in body
        mock_save.assert_not_called()


def test_post_sleep_db_error_returns_500(client, sample_payload):
    with patch("main.upsert_sleep_logs", side_effect=RuntimeError("boom")):
        r = client.post("/sleep",
                        headers={"X-Ingest-Secret": "test-secret"},
                        json=sample_payload)
        assert r.status_code == 500
