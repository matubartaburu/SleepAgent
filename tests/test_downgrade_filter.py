"""Tests del filtro anti-downgrade en el ingest de /sleep."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _existing(total):
    """Helper: devuelve mock de Supabase respondiendo con una fila existente."""
    fake = MagicMock()
    q = MagicMock()
    fake.table.return_value = q
    for m in ("select", "eq", "limit"):
        getattr(q, m).return_value = q
    q.execute.return_value = MagicMock(data=[{"total_sleep_minutes": total}] if total is not None else [])
    return fake


def test_filter_allows_normal_data():
    """Sin fila existente → todo pasa."""
    from main import _filter_suspicious_overrides
    rows = [{"night_date": "2026-05-16", "source": "webhook", "total_sleep_minutes": 490}]
    with patch("main._supabase" if hasattr(__import__("main"), "_supabase") else "db._client",
               return_value=_existing(None)):
        legit, rejected = _filter_suspicious_overrides(rows)
    assert len(legit) == 1
    assert rejected == []


def test_filter_rejects_downgrade():
    """Fila existente 490min, nueva 265min (54% del original) → rechazada."""
    from main import _filter_suspicious_overrides
    rows = [{"night_date": "2026-05-16", "source": "webhook", "total_sleep_minutes": 265}]
    with patch("db._client", return_value=_existing(490)):
        legit, rejected = _filter_suspicious_overrides(rows)
    assert legit == []
    assert len(rejected) == 1
    assert rejected[0]["total_sleep_minutes"] == 265


def test_filter_allows_small_correction():
    """Fila existente 490min, nueva 460min (94%) → pasa (Apple refinó).”"""
    from main import _filter_suspicious_overrides
    rows = [{"night_date": "2026-05-16", "source": "webhook", "total_sleep_minutes": 460}]
    with patch("db._client", return_value=_existing(490)):
        legit, rejected = _filter_suspicious_overrides(rows)
    assert len(legit) == 1
    assert rejected == []


def test_filter_allows_when_existing_is_short():
    """Si la fila existente tenía < 6h (probablemente siesta), no protegemos."""
    from main import _filter_suspicious_overrides
    rows = [{"night_date": "2026-05-16", "source": "webhook", "total_sleep_minutes": 100}]
    with patch("db._client", return_value=_existing(180)):  # existing 3h, no es "noche completa"
        legit, rejected = _filter_suspicious_overrides(rows)
    assert len(legit) == 1
    assert rejected == []


def test_filter_allows_when_new_has_no_total():
    """Si la nueva fila no trae total_sleep, no es candidato a downgrade."""
    from main import _filter_suspicious_overrides
    rows = [{"night_date": "2026-05-16", "source": "webhook", "total_sleep_minutes": None}]
    with patch("db._client", return_value=_existing(490)):
        legit, rejected = _filter_suspicious_overrides(rows)
    assert len(legit) == 1
    assert rejected == []


def test_filter_allows_upgrade():
    """Si nueva > existing (Apple finalmente reportó la noche entera),
    NO rechazamos: ratio >= 0.70 siempre."""
    from main import _filter_suspicious_overrides
    rows = [{"night_date": "2026-05-16", "source": "webhook", "total_sleep_minutes": 500}]
    with patch("db._client", return_value=_existing(360)):
        legit, rejected = _filter_suspicious_overrides(rows)
    assert len(legit) == 1
    assert rejected == []
