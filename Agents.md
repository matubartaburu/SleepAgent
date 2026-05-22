# SleepAgent — Agente personal de sueño

## Qué es esto
Agente personal de Mateo (Uruguay) que toma datos del Apple Watch (sueño con
fases REM/Core/Deep/Awake, HRV SDNN, frecuencia cardíaca, frecuencia cardíaca
en reposo, frecuencia respiratoria, temperatura de muñeca), los guarda en
Supabase, y cada mañana arma un reporte breve y lo manda por WhatsApp en
rioplatense informal. Solo para mí, no es multi-tenant.

El agente se llama **Oscar**.

## Stack

| Capa            | Tecnología                                       | Notas                              |
|---|---|---|
| Web server      | FastAPI + uvicorn                                | Puerto 8765 (8000 lo usa el spa)   |
| LLM principal   | Claude Sonnet 4.6 (`claude-sonnet-4-6`)          | F1.5                               |
| LLM auxiliar    | Claude Haiku 4.5 (`claude-haiku-4-5-20251001`)   | Reservado, no en F1                |
| WhatsApp        | Twilio (sandbox)                                 | F1.5                               |
| Storage         | Supabase (Postgres)                              | Instancia nueva, separada del spa  |
| Scheduler       | APScheduler (AsyncIOScheduler)                   | F1.5 — cron 08:30 America/Montevideo |
| Config          | python-dotenv                                    | `.env` gitignored                  |

## Variables de entorno

Ver `.env.example`. F1.0 solo necesita `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` e
`INGEST_SECRET`. El resto se enchufa en F1.5.

## Archivos del proyecto

| Archivo                | Función                                                              | Fase |
|---|---|---|
| `main.py`              | FastAPI app: `POST /sleep`, `GET /health`                            | F1.0 |
| `config.py`            | Carga `.env`, valida vars críticas                                   | F1.0 |
| `db.py`                | Cliente Supabase + `upsert_sleep_log()` idempotente                  | F1.0 |
| `load_history.py`      | Backfill puntual: carga un export manual de HAE al DB, idempotente   | F1.0 |
| `supabase_setup.sql`   | DDL de `sleep_logs` + RLS (correr a mano en Supabase)                | F1.0 |
| `twilio_client.py`     | `send_whatsapp_text()` via Twilio sandbox (bypasea ventana 24h)      | F1.5 |
| `report.py`            | Arma contexto, llama a Sonnet 4.6, devuelve texto plano del reporte  | F1.5 |
| `data/`                | Carpeta gitignored donde va `export.xml`                             | —    |
| `docs/solutions/`      | Learnings documentados de problemas resueltos (organizados por categoría: integration-issues/, runtime-errors/, etc.) con frontmatter YAML para búsqueda. Generados con `/ce-compound`. | F1.5 |
## Decisiones de diseño

- **Una fila por noche en `sleep_logs`**, indexada por `night_date` (la fecha
  del despertar). UNIQUE `(night_date, source)` para que cualquier reingestión
  sea idempotente con `ON CONFLICT DO UPDATE`.
- **`raw_payload` JSONB** además de columnas tipadas. Defensa contra
  alucinaciones de parser y futuros campos de Health Auto Export.
- **`source` columna preservada en el schema** aunque hoy todo entra como
  `'webhook'`. El backfill por export manual usa el mismo path que el
  webhook automático: una sola fila por noche, idempotente vía
  UNIQUE(night_date, source).
- **Sin reporte si no hay data esa noche.** Silencio total.
- **Reporte cubre la noche que termina la madrugada del mismo día del envío.**
- **Sin pgvector / sin RAG en F1.** Cuentas + texto a mano.
- **Cliente Supabase y cliente Anthropic como singleton de módulo** — patrón
  heredado del proyecto del spa.

## Plan en fases

### F1.0 — base (en curso)
- [x] `git init`, `.gitignore`, `.env.example`, `requirements.txt`, `Agents.md`
- [x] `config.py` con vars F1.0 + F1.5 (validación solo F1.0)
- [x] `db.py` con `upsert_sleep_logs()` batch (init lazy de Supabase)
- [x] `main.py` con `POST /sleep` (auth `X-Ingest-Secret`) y `GET /health`
- [x] `inspect_xml.py`
- [x] Muestra real del JSON de Health Auto Export en `data/sample_payload.json`
- [x] `supabase_setup.sql` con schema final (incluye `inBedStart/End`, `Min/Max/Avg HR`, sin `wrist_temperature`)
- [x] Parser real `parse_health_auto_export()` en `main.py`. Dry-run validado: 6 noches del sample se mapean correctamente.
- [x] `.env` con credenciales reales del Supabase nuevo + `INGEST_SECRET` random
- [x] `supabase_setup.sql` aplicado (incluye RLS enabled, sin policies)
- [x] Smoke test end-to-end: parser + DB + webhook con auth + idempotencia, todo verde

### F1.0b — backfill (hecho)
- [x] Descartado el path por `export.xml` — Health Auto Export deja exportar
      por rango de fechas al mismo formato JSON que el webhook, mucho más
      simple.
- [x] `load_history.py`: lee un JSON local y hace upsert con el mismo parser.
- [x] Cargado export de 3 meses (2026-02-07 a 2026-05-08): 19 noches en DB.

### F1.5 — reporte de Oscar (foco: sueño) — DONE
Decisión: Oscar arranca como doctor del sueño. Las otras voces (corrida,
cardiovascular, luz solar, actividad, oídos, esquí, tendencias) se agregan
después en F2 con tablas nuevas (`daily_metrics`, `workouts`).

Canal de envío: **Twilio sandbox** (bypasea ventana 24h con opt-in). Para
producción real (sin opt-in, múltiples destinatarios) se migraría a Twilio
production con templates aprobados por Meta.

- [x] `twilio_client.py` con `send_whatsapp_text()`, ping de prueba OK
- [x] Prompt de Oscar definido (system prompt en `report.py`)
- [x] `report.py` con noche anterior + promedio 7d, llama a Sonnet 4.6
- [x] Scheduler diario 08:30 America/Montevideo en `main.py` (APScheduler)
- [x] Endpoint `POST /report/test` para disparar a demanda
- [x] Test end-to-end: data → Claude → Twilio → WhatsApp, todo verde

### F2 — Oscar multi-dominio (después de F1.5)
- Coach de corrida: nueva tabla `workouts` + lectura de `running_*` y vo2_max
- Coach cardiovascular: tendencias de RHR, walking_HR, vo2_max
- Coach de luz solar: nueva tabla `daily_metrics` con `time_in_daylight`
- Coach de actividad diaria: pasos, energy, exercise_time, flights_climbed
- Coach de oídos: audio exposure
- Coach de tendencias longitudinales (semana vs mes vs trimestre)
- Notas manuales por WhatsApp ("dormí mal por café tarde", etc.)
- Alertas de umbrales (HRV bajo varias noches seguidas, etc.)
- Modo conversacional vía WhatsApp
- Tabla `sleep_reports` con histórico de lo que mandó Oscar
- Dashboard

## Cómo correr (F1.0)

```bash
# Una vez:
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env  # editar con credenciales reales

# Cada vez (dos terminales):
./run.sh                  # terminal 1: uvicorn en 8765
./tunnel.sh               # terminal 2: ngrok con static domain (lee NGROK_DOMAIN de .env)

# Verificar:
curl http://localhost:8765/health

# Backfill puntual (cuando exportas un rango grande desde Health Auto Export):
.venv/bin/python load_history.py data/<archivo>.json
```

## Setup en el iPhone (Health Auto Export)

Una vez corriendo `run.sh` + `ngrok http 8765`, copiar la URL pública que da
ngrok (ej: `https://abc123.ngrok-free.dev`) y configurar en la app:

1. **Automations** (tab de abajo) → `+`
2. **Trigger**: Schedule, diario a las 08:00 (o la hora que quieras, con el
   reloj ya sincronizado al iPhone).
3. **Action**: REST API
   - URL: `https://<tu-ngrok>.ngrok-free.dev/sleep`
   - Method: `POST`
   - Format: `JSON`
   - Headers:
     - `X-Ingest-Secret: <valor de INGEST_SECRET en .env>`
     - `Content-Type: application/json`
   - Aggregation: "Since last successful automation" (evita gaps)
4. **Métricas a incluir** (mínimo, F1.5 sueño):
   - sleep_analysis
   - heart_rate_variability
   - resting_heart_rate
   - heart_rate
   - respiratory_rate

Dato importante: ngrok con plan free rota la URL cada vez que reiniciás. Si
no tenés un static domain configurado, hay que actualizar la URL en HAE
después de cada reinicio. La alternativa es deploy a Fly/Railway (F2+).
