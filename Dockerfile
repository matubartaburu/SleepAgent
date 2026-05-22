# syntax=docker/dockerfile:1.7
#
# Dockerfile — SleepAgent (Oscar).
#
# Multi-stage para mantener la imagen final chica:
# 1) builder: instala deps en un venv
# 2) runtime: copia solo lo necesario, no incluye toolchain de compilación
#
# La imagen final usa Python 3.12 slim (~80MB base) + deps + código.
# No corre como root.

# ── Stage 1: builder ────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Deps de sistema necesarias para compilar wheels nativas (psycopg2, etc.).
# Se quedan en el stage builder, no llegan al runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && rm -rf /var/lib/apt/lists/*

# Creamos un venv aislado para copiar limpio al runtime.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Marca el entorno como producción: log sanitization, no /docs públicos, etc.
    OSCAR_ENV=production

# ca-certificates es necesario para hacer requests HTTPS salientes (Anthropic, Twilio, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates

# Usuario no-root para minimizar blast radius si algo se rompe.
RUN groupadd --system --gid 1000 oscar \
    && useradd --system --uid 1000 --gid oscar --create-home --shell /bin/bash oscar

WORKDIR /app

# Copiamos el venv pre-armado del builder.
COPY --from=builder /opt/venv /opt/venv

# Copiamos el código. .dockerignore se encarga de excluir .env, .venv, tests, etc.
COPY --chown=oscar:oscar . .

USER oscar

EXPOSE 8765

# Healthcheck para que Fly sepa que el container está vivo.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/health', timeout=3)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
