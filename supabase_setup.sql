-- ============================================================
-- supabase_setup.sql — correr a mano en Supabase > SQL Editor
-- (NO se aplica desde código)
--
-- Schema construido contra muestra real de Health Auto Export
-- (export del 2026-05-01 al 2026-05-08).
-- ============================================================

CREATE TABLE IF NOT EXISTS sleep_logs (
    id                       BIGSERIAL    PRIMARY KEY,

    -- Clave de negocio: la fecha del despertar (mismo `date` que trae
    -- sleep_analysis en el JSON). UNIQUE junto con `source` para idempotencia.
    night_date               DATE         NOT NULL,
    source                   TEXT         NOT NULL CHECK (source IN ('webhook', 'backfill')),

    -- Tiempos reales de la noche
    sleep_start              TIMESTAMPTZ,
    sleep_end                TIMESTAMPTZ,
    in_bed_start             TIMESTAMPTZ,
    in_bed_end               TIMESTAMPTZ,

    -- Duraciones convertidas a minutos (el JSON trae horas con decimales)
    total_sleep_minutes      INT,
    in_bed_minutes           INT,
    rem_minutes              INT,
    core_minutes             INT,
    deep_minutes             INT,
    awake_minutes            INT,

    -- Métricas cardio / respiratorias.
    -- Apple las da promediadas por día calendario, no específicas del sueño;
    -- las asociamos al night_date para tener todo junto.
    hrv_sdnn_ms              NUMERIC(6,2),
    resting_hr_bpm           NUMERIC(5,2),
    avg_hr_bpm               NUMERIC(5,2),
    min_hr_bpm               NUMERIC(5,2),
    max_hr_bpm               NUMERIC(5,2),
    respiratory_rate_brpm    NUMERIC(5,2),

    -- Payload crudo: defensa contra campos nuevos que aparezcan después.
    raw_payload              JSONB,

    -- Auditoría
    received_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    UNIQUE (night_date, source)
);

CREATE INDEX IF NOT EXISTS idx_sleep_logs_night_date
    ON sleep_logs (night_date DESC);


-- Trigger para mantener updated_at fresco en cada UPDATE
CREATE OR REPLACE FUNCTION sleep_logs_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS sleep_logs_updated_at ON sleep_logs;
CREATE TRIGGER sleep_logs_updated_at
    BEFORE UPDATE ON sleep_logs
    FOR EACH ROW EXECUTE FUNCTION sleep_logs_set_updated_at();


-- ─────────────────────────────────────────────────────────────────
-- RLS: lockdown total
-- ─────────────────────────────────────────────────────────────────
-- El backend pega siempre con service_role, que bypasea RLS por diseño.
-- Activando RLS sin definir policies, anon y authenticated quedan
-- bloqueados. Es lo que queremos: data de salud, solo backend la toca.
ALTER TABLE sleep_logs ENABLE ROW LEVEL SECURITY;


-- ============================================================
-- sleep_notes — preguntas que Oscar hace cuando ve algo raro,
-- y respuestas en lenguaje natural de Mateo + tags estructurados.
-- ============================================================

CREATE TABLE IF NOT EXISTS sleep_notes (
    id                BIGSERIAL    PRIMARY KEY,
    night_date        DATE         NOT NULL,

    -- Qué Oscar preguntó y por qué.
    question          TEXT         NOT NULL,
    anomalies         TEXT[]       NOT NULL DEFAULT '{}',
    asked_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- Lo que Mateo respondió (puede llegar tarde o nunca).
    answer            TEXT,
    answered_at       TIMESTAMPTZ,

    -- Tags extraídos por el agente tagger (Haiku) a partir de la respuesta.
    -- Ej: ['comida_tarde', 'alcohol'].
    tags              TEXT[]       NOT NULL DEFAULT '{}',
    tagger_raw        JSONB,

    -- Para evitar duplicar preguntas si una noche dispara múltiples runs.
    UNIQUE (night_date)
);

CREATE INDEX IF NOT EXISTS idx_sleep_notes_night_date
    ON sleep_notes (night_date DESC);

DROP TRIGGER IF EXISTS sleep_notes_updated_at ON sleep_notes;

ALTER TABLE sleep_notes ENABLE ROW LEVEL SECURITY;
