#!/usr/bin/env bash
# run.sh — arranca uvicorn de SleepAgent en el puerto 8765 con reload.
# En otra terminal: ./tunnel.sh
#
# `caffeinate -is` evita que macOS duerma el sistema (idle / sleep) mientras
# el server este corriendo. CAVEAT: con la tapa de un MacBook cerrada la Mac
# entra en clamshell sleep igual; para eso hay que usar Amphetamine o algo
# similar.
set -e
cd "$(dirname "$0")"
exec caffeinate -is .venv/bin/uvicorn main:app --port 8765 --reload
