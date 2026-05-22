#!/usr/bin/env bash
# tunnel.sh — abre el tunel ngrok con el static domain configurado en .env.
# Variable: NGROK_DOMAIN (ej: mateo-sleep.ngrok-free.app)
set -e
cd "$(dirname "$0")"

if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

if [ -z "$NGROK_DOMAIN" ]; then
    echo "ERROR: NGROK_DOMAIN no esta seteado en .env"
    echo "Crea el static domain en https://dashboard.ngrok.com/domains"
    echo "y pegalo en .env como NGROK_DOMAIN=tu-dominio.ngrok-free.app"
    exit 1
fi

exec ngrok http --url="https://${NGROK_DOMAIN}" 8765
