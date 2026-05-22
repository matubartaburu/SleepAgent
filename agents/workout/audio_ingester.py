"""
agents/workout/audio_ingester.py — descarga audio de Twilio y lo transcribe
con OpenAI Whisper.

Twilio en el webhook inbound nos manda 'MediaUrl0' apuntando a un archivo
hosteado por Twilio que requiere auth básica (account_sid + auth_token).
Lo descargamos en memoria, lo mandamos a Whisper, devolvemos el texto.

Costos: ~$0.006/min de audio. Un audio típico de gym (30-60 seg) cuesta
$0.003-0.006.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass

import httpx
from openai import OpenAI

log = logging.getLogger(__name__)


_WHISPER_MODEL = "whisper-1"


@dataclass
class TranscriptionResult:
    text: str
    language: str = ""
    duration_seconds: float = 0.0
    cost_usd: float = 0.0


def _openai_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY no seteado en el entorno")
    return OpenAI(api_key=api_key)


def download_twilio_media(media_url: str) -> tuple[bytes, str]:
    """
    Descarga el audio de Twilio. Devuelve (bytes, content_type).
    Requiere auth con TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN.
    """
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        raise RuntimeError("Falta TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        resp = client.get(media_url, auth=(sid, token))
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "audio/ogg")
        log.info("Twilio media descargado: %d bytes content_type=%s",
                 len(resp.content), content_type)
        return resp.content, content_type


def transcribe(audio_bytes: bytes, content_type: str = "audio/ogg",
                *, language: str = "es") -> TranscriptionResult:
    """
    Manda audio a Whisper y devuelve el texto transcrito.

    Args:
        audio_bytes: bytes del archivo de audio
        content_type: MIME type para inferir extensión
        language: hint de idioma ("es" para español, mejora accuracy)
    """
    if not audio_bytes:
        return TranscriptionResult(text="")

    # Whisper espera un file-like con nombre/extensión adecuados.
    ext = _ext_from_content_type(content_type)
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = f"audio.{ext}"

    client = _openai_client()
    resp = client.audio.transcriptions.create(
        model=_WHISPER_MODEL,
        file=audio_file,
        language=language,
        response_format="verbose_json",
    )

    text = (resp.text or "").strip()
    duration = float(getattr(resp, "duration", 0) or 0)
    # Whisper-1: $0.006/min
    cost = (duration / 60.0) * 0.006

    log.info("Whisper: transcribió %.1fs → %d chars (cost ~$%.4f)",
             duration, len(text), cost)
    return TranscriptionResult(
        text=text,
        language=getattr(resp, "language", language) or language,
        duration_seconds=duration,
        cost_usd=cost,
    )


def transcribe_from_twilio_url(media_url: str, *, language: str = "es") -> TranscriptionResult:
    """Conveniencia: descarga + transcribe en un solo paso."""
    audio_bytes, content_type = download_twilio_media(media_url)
    return transcribe(audio_bytes, content_type, language=language)


def _ext_from_content_type(ct: str) -> str:
    """Mapea MIME a extensión que Whisper acepta."""
    ct = (ct or "").lower().split(";")[0].strip()
    return {
        "audio/ogg":  "ogg",
        "audio/oga":  "ogg",
        "audio/webm": "webm",
        "audio/mp4":  "m4a",
        "audio/m4a":  "m4a",
        "audio/mpeg": "mp3",
        "audio/mp3":  "mp3",
        "audio/wav":  "wav",
        "audio/x-wav": "wav",
    }.get(ct, "ogg")
