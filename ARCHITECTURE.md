# Arquitectura de SleepAgent (Oscar)

Diagramas en Mermaid. En VS Code abrir con el preview de Markdown + extensión
de Mermaid; en GitHub se renderizan solos.

---

## 1. Vista de pájaro — qué le pasa a una noche

```mermaid
flowchart LR
    Watch["⌚ Apple Watch<br/>(sueño, HRV, HR, RR)"] --> iPhone["📱 iPhone<br/>Apple Health"]
    iPhone --> HAE["Health Auto Export<br/>(automation diaria 08:00)"]
    HAE -- "POST /sleep<br/>X-Ingest-Secret" --> Ngrok["🌐 ngrok<br/>(domain estático)"]
    Ngrok --> Server["FastAPI :8765<br/>(main.py)"]
    Server -- "upsert idempotente" --> DB[("🗄 Supabase<br/>sleep_logs")]

    Cron["⏰ APScheduler<br/>cron 08:30 Mvd"] --> Report["report.py<br/>build_daily_report()"]
    Report -- "lee anoche + 7d" --> DB
    Report -- "system + data" --> Claude["🧠 Claude<br/>Sonnet 4.6"]
    Claude -- "texto plano" --> Report
    Report --> Twilio["Twilio sandbox<br/>WhatsApp"]
    Twilio --> Mateo["📲 Mateo"]

    classDef ext fill:#fef3c7,stroke:#d97706,color:#000
    classDef code fill:#dbeafe,stroke:#2563eb,color:#000
    classDef data fill:#dcfce7,stroke:#16a34a,color:#000
    class Watch,iPhone,HAE,Ngrok,Claude,Twilio,Mateo ext
    class Server,Report,Cron code
    class DB data
```

**Tres colores:**
- 🟡 Amarillo: cosas externas (Apple, ngrok, Claude, Twilio, vos).
- 🔵 Azul: código que corre en tu Mac.
- 🟢 Verde: el store.

---

## 2. Mapa de archivos y dependencias

```mermaid
flowchart TB
    subgraph entry ["🚪 Entry points"]
        run["run.sh<br/>uvicorn :8765"]
        tunnel["tunnel.sh<br/>ngrok"]
        loadhist["load_history.py<br/>backfill manual"]
    end

    subgraph app ["📦 App"]
        main["main.py<br/>━━━━━━━━━━━━<br/>FastAPI app<br/>POST /sleep<br/>POST /report/test<br/>GET /health<br/>parse_health_auto_export()<br/>scheduler 08:30"]
        report["report.py<br/>━━━━━━━━━━━━<br/>_build_context()<br/>build_daily_report()<br/>send_daily_report()"]
        db["db.py<br/>━━━━━━━━━━<br/>upsert_sleep_logs()<br/>_client() lazy"]
        twilio["twilio_client.py<br/>━━━━━━━━━━━━<br/>send_whatsapp_text()"]
        config["config.py<br/>━━━━━━━━━<br/>load_dotenv()<br/>valida vars"]
    end

    subgraph schema ["🏗 Schema"]
        sql["supabase_setup.sql<br/>(correr a mano)"]
    end

    subgraph env ["🔐 Secrets"]
        dotenv[".env<br/>(gitignored)"]
    end

    run --> main
    tunnel -. expone .-> main
    loadhist --> main
    loadhist --> db

    main --> report
    main --> db
    main --> config

    report --> db
    report --> twilio
    report --> config

    twilio --> config
    db --> config

    config --> dotenv
    sql -. define tabla .-> db

    classDef entry fill:#fce7f3,stroke:#be185d,color:#000
    classDef code fill:#dbeafe,stroke:#2563eb,color:#000
    classDef secret fill:#fee2e2,stroke:#dc2626,color:#000
    classDef schema fill:#dcfce7,stroke:#16a34a,color:#000
    class run,tunnel,loadhist entry
    class main,report,db,twilio,config code
    class dotenv secret
    class sql schema
```

---

## 3. Ingesta — qué pasa cuando llega un `POST /sleep`

```mermaid
sequenceDiagram
    autonumber
    participant HAE as Health Auto Export
    participant API as FastAPI /sleep
    participant Parser as parse_health_auto_export
    participant DB as Supabase

    HAE->>API: POST /sleep + X-Ingest-Secret + JSON
    API->>API: valida header
    alt secret inválido
        API-->>HAE: 401 unauthorized
    else OK
        API->>Parser: payload completo
        Parser->>Parser: indexa hrv/rhr/hr/rr por fecha
        Parser->>Parser: 1 fila por sleep_analysis item
        Parser-->>API: list[dict] (N noches)
        alt N == 0
            API-->>HAE: {nights: 0}
        else
            API->>DB: upsert ON CONFLICT (night_date, source)
            DB-->>API: filas guardadas
            API-->>HAE: {nights: N, night_dates: [...]}
        end
    end
```

---

## 4. Reporte diario — qué pasa a las 08:30

```mermaid
sequenceDiagram
    autonumber
    participant Cron as APScheduler
    participant Report as report.py
    participant DB as Supabase
    participant Claude as Sonnet 4.6
    participant Twilio
    participant WA as WhatsApp (Mateo)

    Cron->>Report: send_daily_report()
    Report->>DB: select last 7 nights
    DB-->>Report: rows[]

    alt no hay filas
        Report-->>Cron: {sent: false, reason: no_data}
    else night_date != today
        Note over Report: silencio total
        Report-->>Cron: {sent: false, reason: no_data}
    else hay data de anoche
        Report->>Report: calcula baseline 7d
        Report->>Report: arma user_message
        Report->>Claude: system + data (max 400 tok)
        Claude-->>Report: texto plano rioplatense
        Report->>Twilio: send_whatsapp_text(body)
        Twilio->>WA: mensaje
        Twilio-->>Report: sid
        Report-->>Cron: {sent: true, sid, chars, preview}
    end
```

---

## 5. Modelo de datos (hoy — F1.5)

```mermaid
erDiagram
    sleep_logs {
        bigserial id PK
        date night_date "fecha del despertar"
        text source "webhook | backfill"
        timestamptz sleep_start
        timestamptz sleep_end
        timestamptz in_bed_start
        timestamptz in_bed_end
        int total_sleep_minutes
        int in_bed_minutes
        int rem_minutes
        int core_minutes
        int deep_minutes
        int awake_minutes
        numeric hrv_sdnn_ms
        numeric resting_hr_bpm
        numeric avg_hr_bpm
        numeric min_hr_bpm
        numeric max_hr_bpm
        numeric respiratory_rate_brpm
        jsonb raw_payload "defensa contra campos nuevos"
        timestamptz received_at
        timestamptz updated_at
    }
```

**Reglas:**
- `UNIQUE(night_date, source)` → upsert idempotente.
- RLS ON sin policies → solo el backend con `service_role` puede tocarla.
- Trigger `BEFORE UPDATE` mantiene `updated_at` fresco.

---

## 6. Hacia dónde va (F2)

```mermaid
flowchart LR
    sleep[("sleep_logs<br/>✅ hoy")]
    workouts[("workouts<br/>🔮 corrida + vo2max")]
    daily[("daily_metrics<br/>🔮 luz, pasos, energy")]
    reports[("sleep_reports<br/>🔮 histórico de envíos")]

    oscar["🤖 Oscar multi-dominio"]

    sleep --> oscar
    workouts --> oscar
    daily --> oscar
    oscar --> reports

    classDef now fill:#dcfce7,stroke:#16a34a,color:#000
    classDef future fill:#f3f4f6,stroke:#9ca3af,color:#000,stroke-dasharray: 5 5
    class sleep now
    class workouts,daily,reports future
```
