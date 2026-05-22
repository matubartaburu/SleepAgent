"""
agents/workout/ — sub-agents del módulo de workout.

Cada agente vive en su archivo, responsabilidad única:

- audio_ingester    descarga audio de Twilio + transcribe con Whisper
- workout_router    clasifica intent (log/retrieve/setup/correction/etc)
- workout_parser    texto natural → ejercicios estructurados (musculación)
- cardio_parser     texto natural → cardio session (no-Apple)
- muscle_classifier ejercicio → muscle_group, con alias dinámico
- plan_setup        dicta tu split → crea/actualiza Training Plan
- workout_logger    persiste workouts/cardio en Notion
- workout_retriever consulta Notion para preguntas tipo "última vez de X"

Notion client compartido vive en notion_client.py (raíz del repo)
para reutilizarse desde report.py (cross-domain con sueño).
"""
