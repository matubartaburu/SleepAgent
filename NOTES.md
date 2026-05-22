# NOTES — Bitácora de sesiones

Recaps breves de cada sesión de laburo con Claude. Mateo pide el recap,
Claude lo escribe acá. Más nuevo arriba.

Formato por entrada:
- Fecha (YYYY-MM-DD)
- Qué se hizo (bullets cortos)
- Qué quedó a medias / próximos pasos
- Archivos tocados (si aplica)

---

## 2026-05-15 — sesión 3 (tarde-noche)

**Features cerradas (F-WEEKLY-MONTHLY-001 + F-SLEEP-NOTES-001):**

### Reportes semanal + mensual
- `build_weekly_report()` y `build_monthly_report()` en `report.py` con system prompts dedicados.
- Helpers nuevos: `_to_minutes_since_noon`, `_to_minutes_since_midnight`, `_stddev_minutes`, `_consistency_label`, `_minutes_to_clock`.
- `_aggregate_period()` calcula promedios, SD de bedtime/waketime, distribución por bucket (<6h, 6-7h, 7-8h, 8h+), mejor/peor noche, cuenta de tags de notas.
- Monthly suma comparación con los 30 días previos (sleep, HRV, RHR).
- **Reto autoimpuesto 7h10 (430 min)**: si daily < 430 o promedio weekly/monthly < 430, FLAG INTERNO en el user_message → Oscar te rete cariñoso pero firme.
- Nuevos jobs en APScheduler: **domingo 09:30** weekly, **día 1 a las 10:30** monthly.
- Endpoints `POST /report/test-weekly` y `POST /report/test-monthly` para disparar a demanda.

### Detector de anomalías + conversación inversa
- `_detect_anomalies(night, baseline)` flag: sueño corto, deep flaco, awake alto, HRV bajo, RHR alto, RR alto.
- `_generate_followup_question(anomalies)` con Sonnet — 1 mensaje corto que pregunta qué pasó, ofrece opciones, libre para responder.
- Tras enviar el daily, si hay anomalías, Oscar manda 2° WhatsApp con la pregunta y persiste en `sleep_notes`.
- Endpoint `POST /whatsapp/inbound` con validación de firma Twilio (ignora remitentes desconocidos).
- `agents/tagger.py` usa **Haiku 4.5** (más barato) para mapear respuesta libre → tags controlados (`comida_tarde`, `alcohol`, `deporte_tarde`, `estres`, etc.).
- Cuando llega respuesta: tagger → `update_note_answer()` persiste tags + answer + answered_at.

### Tabla sleep_notes
- Nueva en `supabase_setup.sql` con UNIQUE(night_date), `tags TEXT[]`, `anomalies TEXT[]`, `tagger_raw JSONB`, RLS lockdown.
- Helpers `db.py`: `insert_sleep_note`, `get_open_note`, `update_note_answer`, `get_notes_for_range`.

### Cross-reference
- Weekly y monthly leen sleep_notes del periodo, agrupan por tag, incluyen en el user_message como "CAUSAS REPORTADAS".
- Oscar (system prompt) sabe usar esto: *"de las 3 noches con deep flaco, 2 fueron post-comida-tarde"*.

### Tests: 79/79 verde
- Nuevos: `test_consistency_helpers.py`, `test_daily_reto.py`, `test_weekly_monthly.py`, `test_tagger.py`, `test_inbound_webhook.py`.

### Pasos manuales (single user, no automatizables)
1. **Aplicar migración** del bloque `sleep_notes` en Supabase SQL Editor.
2. **Twilio sandbox**: configurar el inbound webhook a `https://<tu-ngrok>.ngrok-free.dev/whatsapp/inbound` (POST). Tab: WhatsApp Sandbox Settings → "When a message comes in".

### Costos esperados por reporte
- Daily: Sonnet ~$0.005 + (si OSCAR_VALIDATOR_ENABLED=1) Opus ~$0.05 + (si anomalía) Sonnet followup ~$0.005 + Haiku tagger ~$0.0005 = **$0.01-$0.06**
- Weekly: Sonnet ~$0.015 una vez por semana
- Monthly: Sonnet ~$0.025 una vez por mes

## 2026-05-15 — sesión 2 (tarde)

**Diagnóstico del "Oscar no manda":**
- El server (uvicorn :8765) y ngrok están corriendo, `/health` 200.
- `/report/test` devolvía `no_data` → la regla diseñada `night_date != today → silencio` se estaba disparando.
- Causa: HAE no entregó payload para `night_date=2026-05-15`. Última fila en DB era 2026-05-14. Probable: Apple Health no había finalizado los stages a las 08:00.
- **No es bug del código**, es falta de visibilidad cuando upstream falla.
- Smoke test end-to-end OK: `dev_force_report.py` mandó reporte usando 2026-05-14 vía Anthropic → Twilio → WhatsApp (SID `SM2bd16db572668af54fb9caa2acb4a34f`).

**Multi-agente scaffolding:**
- `.agents/` con shared state (`features.json`, `validation.contract.md`, `handoffs.jsonl`, `budget.json`, `README.md`).
- `agents/` package: `base.py` (cliente Claude + budget tracking + handoff log), `validator.py` (LLM-as-judge Opus 4.7), `constructor.py` (planificador), `test_agent.py`, `orchestrator.py` (loop con max-iters y budget cap), `preflight.py` (no-LLM, chequea data + Twilio + Anthropic antes del reporte).
- CLI del orchestrator: `--list`, `--feature ID --dry-run`, `--apply`, `--budget 2.00`, `--reset-budget`.
- 3 features inicializadas en `features.json`: F-PREFLIGHT-001 (in_progress), F-VALIDATOR-001, F-REPORTS-TABLE-001.

**Validator integrado al flujo real (opt-in):**
- `send_daily_report()` en `report.py` ahora valida y regenera (max 3) si `OSCAR_VALIDATOR_ENABLED=1` en `.env`. Si los 3 intentos fallan, manda alerta a Mateo en vez del reporte.
- Cuesta ~$0.05/reporte adicional con Opus. Default OFF para no quemar tokens.

**Preflight integrado al scheduler:**
- `main.py` agrega job `preflight` a las 08:25 (5 min antes del reporte 08:30). Si falta data de hoy, manda WhatsApp explicando qué pasa (HAE, Twilio o Anthropic). Adiós silencio cuando upstream falla.

**Tests (38/38 verde):**
- `pytest.ini` + `tests/conftest.py` con fixtures.
- `test_helpers.py` (8), `test_parser.py` (10), `test_ingest.py` (6 con TestClient + mocks), `test_preflight.py` (5), `test_validator.py` (6).
- Marker `integration` reservado para los que toquen Supabase real (no escritos aún).
- `pytest` + `pytest-asyncio` instalados en `.venv`.

**Archivos tocados:**
- Nuevos: `.agents/*`, `agents/__init__.py`, `agents/base.py`, `agents/constructor.py`, `agents/test_agent.py`, `agents/validator.py`, `agents/orchestrator.py`, `agents/preflight.py`, `dev_force_report.py`, `pytest.ini`, `tests/conftest.py`, `tests/test_*.py`.
- Editados: `main.py` (job preflight 08:25), `report.py` (loop validator + alerta si rechaza).

**A medias / próximo:**
- F-VALIDATOR-001 y F-REPORTS-TABLE-001 quedan en `features.json` pendientes. Correr con `python -m agents.orchestrator --feature F-... --dry-run` para ver plan sin gastar tokens.
- Para activar el validator en producción: agregar `OSCAR_VALIDATOR_ENABLED=1` a `.env`.
- Mañana ver si HAE entrega data antes del preflight de 08:25.

## 2026-05-15 — sesión 1 (mañana)

- Retomamos después de ~6 días sin tocar el repo (último cambio: 2026-05-09 en `main.py` y `run.sh`).
- Creamos `NOTES.md` como bitácora manual: Mateo pide recap → Claude actualiza acá.
- Repo sigue sin commits. `inspect_xml.py` marcado para borrar.
- **Archivos tocados:** `NOTES.md` (nuevo).
