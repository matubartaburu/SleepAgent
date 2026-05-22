# Oscar — Agente personal de sueño y entrenamiento por WhatsApp

> Un asistente AI que monitorea cómo dormís, cómo entrenás, y te manda reportes
> diarios por WhatsApp. Construido sobre FastAPI, Claude (Anthropic) y un
> sistema de varios agentes especializados.

**Stack:** Python · FastAPI · Claude (Sonnet 4.6 + Haiku 4.5 + Opus 4.7) · Whisper · Twilio · Supabase · Notion · APScheduler · Docker · Fly.io · 137 tests con pytest

**📐 Arquitectura completa con diagramas:** [`ARCHITECTURE.md`](./ARCHITECTURE.md) — 6 diagramas Mermaid (flujo de ingesta, secuencia del reporte diario, modelo de datos, etc.) que GitHub renderiza inline. Recomendado para una vista técnica rápida.

---

## Tabla de contenidos

1. [¿Qué es esto?](#qué-es-esto)
2. [Cómo se usa (en la práctica)](#cómo-se-usa-en-la-práctica)
3. [¿Por qué lo construí?](#por-qué-lo-construí)
4. [Vista general — cómo funciona](#vista-general--cómo-funciona)
5. [Stack técnico](#stack-técnico)
6. [Sistema multi-agente](#sistema-multi-agente)
7. [Seguridad](#seguridad)
8. [Cómo correrlo localmente](#cómo-correrlo-localmente)
9. [Lecciones aprendidas](#lecciones-aprendidas)
10. [Roadmap](#roadmap)

---

## ¿Qué es esto?

Oscar es un agente personal que vive en un servidor y se comunica con su
usuario **enteramente por WhatsApp**. Tres capacidades principales:

### 🌙 Sueño automático
Mi Apple Watch registra el sueño cada noche. Esa data se sincroniza al
iPhone (Apple Health), y una app llamada *Health Auto Export* la manda a un
endpoint HTTP de Oscar todas las mañanas. Oscar la procesa, la guarda en
una base de datos, y a las 08:30 me llega un WhatsApp con un resumen del
estilo:

> *"Anoche 7h25 (vs 6h50 promedio semana), REM 22% — bien. HRV 67ms,
> estable. Sigue así."*

### 🏋️ Workouts por mensaje de voz
Le mando un audio de WhatsApp tipo *"hice press banca 4x8 con 80, después
sentadilla 3x10 con 100"* y Oscar:

1. Transcribe el audio con **Whisper**
2. Entiende qué dije con **Claude Sonnet**
3. Guarda los ejercicios en **Notion**, en la rutina del día correcto
4. Me responde confirmando

### 💬 Conversaciones
Le puedo preguntar cosas como *"¿cuánto dormí ayer?"*, *"¿qué hice la
última vez de pierna?"*, *"¿cómo vengo de sueño esta semana?"* y me
contesta usando la data real.

---

## Cómo se usa (en la práctica)

```
                    ┌─────────────────────────┐
                    │       📱 WhatsApp        │
                    └────────────┬────────────┘
                                 │
                                 ▼
        ┌──────────────────────────────────────────────┐
        │   Yo:  "hice press banca 4x8 con 80"         │
        │                                              │
        │   Oscar:  Anotado, 1 ejercicio:              │
        │            Día 1:                            │
        │            • press banca 4x8 80kg            │
        └──────────────────────────────────────────────┘

        ┌──────────────────────────────────────────────┐
        │   Oscar (auto-mañana):                       │
        │   Anoche 8h10 — sólida.                     │
        │   REM 77min, deep 31min, HRV 122ms.         │
        │   Mejor noche de la semana, seguí así.      │
        └──────────────────────────────────────────────┘

        ┌──────────────────────────────────────────────┐
        │   Yo:  "¿cómo dormí la semana?"             │
        │                                              │
        │   Oscar:  Promedio 7h05, 1 noche bajo 6h.    │
        │           Consistencia OK, deep medio bajo.  │
        └──────────────────────────────────────────────┘
```

---

## ¿Por qué lo construí?

Tres razones:

1. **Quería un coach personal sin pagar uno.** Las apps de fitness/sueño
   te muestran data pero no te *hablan* — son dashboards. Yo quería algo
   que me diga *"hermano, dormiste mal 3 días seguidos, frená el café"*.

2. **Quería aprender a construir agentes con LLMs.** Multi-agente,
   parsing de lenguaje natural, transcripción de voz, integración con
   APIs reales (Twilio, Notion, Supabase, Fly), deploy en producción.

3. **Compound engineering.** Quería un proyecto donde el conocimiento
   se acumule: cada bug arreglado documentado, cada decisión justificada,
   cada feature reutilizable.

---

## Vista general — cómo funciona

```
              ┌────────────────────────────────────────────┐
              │         🍎 Apple Watch + iPhone            │
              │  (sueño, HRV, frecuencia cardíaca)         │
              └────────────────────┬───────────────────────┘
                                   │
                                   ▼
              ┌────────────────────────────────────────────┐
              │     Health Auto Export (app iOS)           │
              │  manda JSON a Oscar todas las mañanas      │
              └────────────────────┬───────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│                    🚀 OSCAR (FastAPI en Fly.io)                  │
│                                                                  │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐    │
│  │ /sleep       │   │ /whatsapp/   │   │  Cron 08:30      │    │
│  │ (webhook     │   │  inbound     │   │  (reporte diario)│    │
│  │  Apple)      │   │  (Twilio)    │   │                  │    │
│  └──────┬───────┘   └──────┬───────┘   └────────┬─────────┘    │
│         │                  │                    │              │
│         │   ┌──────────────┴───────┐            │              │
│         │   │   Si es audio →      │            │              │
│         │   │   Whisper transcribe │            │              │
│         │   └──────────┬───────────┘            │              │
│         │              │                        │              │
│         │              ▼                        ▼              │
│         │   ┌────────────────────┐   ┌────────────────────┐    │
│         │   │  ROUTER (Haiku)    │   │  REPORTER (Sonnet) │    │
│         │   │  ¿Qué intentás?    │   │  + VALIDATOR(Opus) │    │
│         │   │  - log workout     │   │  Arma el resumen   │    │
│         │   │  - correction      │   │  y lo valida       │    │
│         │   │  - sleep question  │   └─────────┬──────────┘    │
│         │   │  - retrieve...     │             │               │
│         │   └────────┬───────────┘             │               │
│         │            │                         │               │
│         │            ▼                         │               │
│         │  ┌─────────────────────┐             │               │
│         │  │ SPECIALIST agents:  │             │               │
│         │  │ • workout_parser    │             │               │
│         │  │ • cardio_parser     │             │               │
│         │  │ • muscle_classifier │             │               │
│         │  │ • answerer          │             │               │
│         │  └─────────┬───────────┘             │               │
│         │            │                         │               │
│         ▼            ▼                         ▼               │
└──────────┼────────────────────────────────────┼────────────────┘
           │                                    │
           ▼                                    ▼
  ┌────────────────┐                  ┌─────────────────┐
  │  Supabase      │                  │  Twilio         │
  │  (sueño)       │                  │  WhatsApp       │
  └────────────────┘                  └─────────────────┘
           │
           ▼
  ┌────────────────┐
  │  Notion        │
  │  (workouts +   │
  │   cardio +     │
  │   training     │
  │   plan)        │
  └────────────────┘
```

---

## Stack técnico

| Capa | Tecnología | Por qué |
|---|---|---|
| **Web server** | FastAPI + uvicorn | Async nativo, ideal para webhooks y LLMs |
| **LLMs** | Claude Sonnet 4.6 + Haiku 4.5 + Opus 4.7 | Sonnet parsea/escribe, Haiku rutea (rápido y barato), Opus valida (más caro pero certero) |
| **Voz → texto** | OpenAI Whisper API | Mejor calidad para español rioplatense con jerga |
| **WhatsApp** | Twilio Sandbox | Gratis para uso personal; sin templates aprobados |
| **Datos de sueño** | Supabase (Postgres) | Free tier generoso, schema relacional simple |
| **Workouts + plan** | Notion API | Reusa mi base de notas, fácil de visualizar/editar a mano |
| **Cron scheduler** | APScheduler (in-process) | Sin servidor de jobs separado; vive dentro de FastAPI |
| **Container** | Docker | Reproducible local + Fly |
| **Hosting** | Fly.io (región `gru`) | Latencia baja a Sudamérica, free tier suficiente para 1 usuario |
| **Source de sueño** | Apple Health Auto Export | App iOS que automatiza export del HealthKit |
| **Tests** | pytest (137 tests) | Unit + integración + tests del webhook |

---

## Sistema multi-agente

La parte más interesante del proyecto. Oscar no es un solo LLM gigante —
son **varios agentes pequeños**, cada uno con un trabajo específico.

### ¿Por qué multi-agente?

Imaginá que tenés un asistente humano. ¿Le pedirías a la misma persona
que sea contador, cocinero, médico y abogado? No — buscás especialistas.

Lo mismo acá. En vez de un mega-prompt que tiene que hacer todo, cada
agente:

- Tiene **un prompt corto y específico**
- Usa **el modelo que conviene** (rápido y barato si la tarea es simple,
  potente si es crítica)
- Es **testeable de forma aislada**
- Falla **de forma predecible** (si rompe el parser de workouts, no
  afecta el reporte de sueño)

### Los agentes de Oscar

```
┌─────────────────────────────────────────────────────────────────┐
│                    Agentes en runtime                            │
│              (corren cuando llega un mensaje)                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  🎯 Router (Haiku)                                              │
│     Clasifica tu mensaje en 1 de 11 categorías                 │
│     (log_workout, correction, sleep_question, etc.)             │
│                                                                 │
│  🎤 Audio ingester (Whisper)                                    │
│     Transcribe audios de WhatsApp a texto                       │
│                                                                 │
│  💪 Workout parser (Sonnet)                                     │
│     De "press banca 4x8 con 80" extrae                          │
│     {ejercicio, sets, reps, peso, RIR}                          │
│                                                                 │
│  🏃 Cardio parser (Sonnet)                                      │
│     De "salí a correr 5km en 25min" extrae                      │
│     {sport, distance, duration, intensity}                      │
│                                                                 │
│  🧠 Muscle classifier (Haiku)                                   │
│     Decide a qué día del plan va cada ejercicio                 │
│                                                                 │
│  💬 Answerer (Sonnet)                                           │
│     Responde preguntas de sueño usando memoria + DB             │
│                                                                 │
│  📊 Reporter (Sonnet)                                           │
│     Escribe el reporte diario en rioplatense                    │
│                                                                 │
│  🛡 Validator (Opus)                                            │
│     Lee el reporte y verifica que no haya alucinaciones         │
│     (números equivocados, frases inconsistentes, etc.)          │
│                                                                 │
│  ⚠️ Preflight                                                   │
│     Antes del reporte verifica que todo esté OK                 │
│     (data completa, Twilio responde, etc.)                      │
│                                                                 │
│  🏷 Tagger (Haiku)                                              │
│     Etiqueta tus respuestas para análisis posterior             │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Patrón LLM-as-judge (Validator)

El **Validator** es especialmente interesante. Cuando el Reporter (Sonnet)
escribe el reporte diario, antes de mandarlo por WhatsApp lo revisa
**Opus** (un modelo más potente) jugando de juez:

```
Reporter (Sonnet) → escribe reporte
        │
        ▼
Validator (Opus) → ¿el reporte miente sobre algún número?
                   ¿hay contradicciones internas?
                   ¿el tono coincide con el contrato?
        │
        ├─── ✅ OK → se manda
        │
        └─── ❌ FAIL → vuelve al reporter con feedback
                       (max 3 intentos)
                       Si fallan los 3 → alerta al usuario
```

Este patrón es **caro en tokens** pero crítico para confianza: si Oscar
te dice un dato equivocado sobre tu salud, perdés la confianza completa.
Mejor pagar el extra del juez.

---

## Seguridad

Lo que cuidé al diseñar Oscar:

### Secrets
- **`.env` siempre gitignored.** Sin excepciones. Pre-commit hook bloquea
  intentos de commitear el archivo.
- **`.env.example`** con los nombres de las vars (sin valores) sí va al
  repo, para que cualquiera sepa qué configurar.
- **Secrets en Fly.io** vía `fly secrets import` (encriptados at-rest).

### Validación de webhooks
- **`POST /sleep`** requiere header `X-Ingest-Secret` con un valor random
  de 32 chars. Sin él → 401.
- **`POST /whatsapp/inbound`** valida la firma HMAC de Twilio
  reconstruyendo la URL con headers `X-Forwarded-Proto` y `X-Forwarded-Host`
  (necesario detrás de proxy reverso).
- **Filtro de remitentes**: solo procesa mensajes del número de WhatsApp
  autorizado. El resto se descarta sin loguear (anti-enumeration).

### Anti-downgrade attack
La app de Apple Watch a veces re-sincroniza data parcial (ej: solo 4h
en vez de las 8h reales). Oscar **rechaza overrides sospechosos**: si la
nueva data es menor al 70% de la existente y la existente tenía más de
6h, se ignora.

### Sanitización de logs
Filtros regex aplicados a TODOS los logs en producción que detectan y
redactan automáticamente:
- API keys (`sk-ant-***`, `sk-proj-***`)
- Tokens de Notion (`secret_***`)
- Auth tokens de Twilio
- IDs de mensajes y URLs con tokens en query string

### Defense in depth
- **Supabase RLS habilitado** (Row Level Security) sin policies abiertas:
  solo el backend con `service_role` key puede tocar las tablas.
- **`raw_payload` JSONB** además de columnas tipadas — defensa contra
  alucinaciones del parser y campos nuevos que aparezcan en futuro.
- **Pre-commit hook anti-leak**: corre antes de cada commit y revisa que
  no haya secrets en los archivos staged.
- **`security_guard` agent**: audit con 7 reglas (S001-S007) que se
  puede correr a demanda para revisar el repo.

---

## Cómo correrlo localmente

### Requisitos
- Python 3.13+
- Cuentas: [Anthropic](https://console.anthropic.com),
  [OpenAI](https://platform.openai.com), [Supabase](https://supabase.com),
  [Notion](https://developers.notion.com), [Twilio](https://twilio.com)
- iPhone con [Health Auto Export](https://www.healthyapps.dev/) (opcional)

### Setup

```bash
# Clonar
git clone https://github.com/<tu-usuario>/SleepAgent.git
cd SleepAgent

# Virtualenv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Config
cp .env.example .env
# editar .env con tus credenciales reales

# Schema de Supabase
# Ir al SQL editor de tu proyecto y pegar el contenido de:
cat supabase_setup.sql

# Notion: crear las databases (script auto-init)
.venv/bin/python -c "from notion_store import ensure_all_dbs; ensure_all_dbs()"

# Tests (deben pasar todos)
.venv/bin/python -m pytest

# Levantar local
./run.sh           # FastAPI en http://localhost:8765
./tunnel.sh        # ngrok para exponer a Twilio (otra terminal)
```

### Deploy a Fly.io

```bash
# Una vez:
fly launch                      # configura la app
fly secrets import < .env       # secrets encriptados

# Cada deploy:
fly deploy --remote-only
```

---

## Lecciones aprendidas

Bugs reales que aparecieron en producción y lo que enseñaron. Algunos
están documentados en detalle en [`docs/solutions/`](./docs/solutions/).

### 1. Apple Health manda el sueño en varios pedazos
Si me despierto, voy al baño, y vuelvo a dormir, Apple lo registra como
**2 sesiones de sueño separadas**. El parser inicial solo guardaba la
última y reportaba 30 min en vez de 7h.
**Fix**: agregar los items de `sleep_analysis` por `night_date` antes
de guardar.

### 2. Re-sincronización puede sobrescribir data buena con data parcial
HAE re-mandó una noche con solo 4h25 cuando la original tenía 8h10.
**Fix**: rechazar overrides donde la nueva data es <70% de la existente.

### 3. Validación de firma Twilio detrás de proxy
En Fly.io, el request llega al app por HTTP interno (no HTTPS) y con un
host distinto al público. La firma HMAC fallaba.
**Fix**: reconstruir la URL pública usando `X-Forwarded-Proto` y
`X-Forwarded-Host` antes de validar.

### 4. Twilio webhook timeout de 15 segundos
Procesar un audio largo (Whisper + Sonnet) puede tardar más. Twilio mata
la conexión.
**Fix**: responder 200 inmediatamente, procesar en background con
`asyncio.create_task` y mandar la respuesta cuando termine.

### 5. Compound engineering > arreglar bugs y seguir
La diferencia entre código que crece en deuda y código que crece en
sabiduría está en **documentar las soluciones mientras el contexto está
fresco**. Skill [`/ce-compound`](https://github.com/anthropics/claude-code)
me genera los docs automáticamente al cerrar un bug feo.

---

## Roadmap

- [ ] Coach de corrida (vo2max, ritmo, tendencias)
- [ ] Comparación mes a mes (gráficos texto-ASCII por WhatsApp)
- [ ] Alerta de overtraining (3+ días con poco sueño y entrenamiento alto)
- [ ] Integración con GitHub: abrir issue con label → Oscar lo implementa
      en una rama y abre PR (loop self-improving)
- [ ] Dashboard web simple en Vercel (solo lectura)
- [ ] Migrar de Twilio sandbox a número Twilio propio (sin ventana 24h)

---

## Licencia

MIT — usá el código libremente, citando el repo si te sirve de base.

## Créditos

- [Claude Code](https://claude.com/claude-code) — asistencia de desarrollo
- [Anthropic Claude](https://anthropic.com) — modelos LLM
- [Health Auto Export](https://www.healthyapps.dev/) — bridge de Apple Health
