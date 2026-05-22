"""
notion_client.py — wrapper sobre el SDK oficial de Notion para Oscar.

Responsabilidades:
1. Inicialización lazy del cliente con NOTION_TOKEN.
2. Bootstrap de las 4 DBs del módulo workout dentro de una página padre.
3. Helpers para create/query/update sobre cada DB.
4. Persistencia de los DB IDs en .env / runtime para queries futuras.

Diseño:
- Idempotente: bootstrap_databases() detecta DBs ya creadas (por nombre)
  y no las duplica.
- Schema versionado en SCHEMAS dict — fácil de auditar y migrar.
- No lanza excepciones a nivel I/O — devuelve None o dict vacío y loguea.
- Compatible con la práctica de Oscar: solo el backend toca Notion, no hay
  multi-usuario.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from notion_client import Client as _NotionSDKClient

log = logging.getLogger(__name__)


# ── Identificadores de las DBs (los nombres que va a usar en Notion) ────────

DB_TRAINING_PLAN = "Oscar — Training Plan"
DB_WORKOUTS = "Oscar — Workouts"
DB_CARDIO = "Oscar — Cardio Sessions"
DB_ALIASES = "Oscar — Exercise Aliases"


# ── Schemas Notion (properties dict) ────────────────────────────────────────

MUSCLE_GROUP_OPTIONS = [
    {"name": "pecho", "color": "red"},
    {"name": "espalda", "color": "blue"},
    {"name": "hombros", "color": "yellow"},
    {"name": "brazos", "color": "orange"},
    {"name": "piernas", "color": "green"},
    {"name": "glúteos", "color": "pink"},
    {"name": "core", "color": "purple"},
    {"name": "cuello", "color": "gray"},
]

SPORT_OPTIONS = [
    {"name": "running", "color": "red"},
    {"name": "cycling", "color": "blue"},
    {"name": "walking", "color": "green"},
    {"name": "swimming", "color": "purple"},
    {"name": "hiking", "color": "orange"},
    {"name": "futbol", "color": "yellow"},
    {"name": "tenis", "color": "pink"},
    {"name": "escalada", "color": "brown"},
    {"name": "yoga", "color": "gray"},
    {"name": "hiit", "color": "default"},
    {"name": "otro", "color": "default"},
]

INTENSITY_OPTIONS = [
    {"name": "suave", "color": "green"},
    {"name": "moderada", "color": "yellow"},
    {"name": "intensa", "color": "red"},
    {"name": "intervalos", "color": "purple"},
]

SOURCE_OPTIONS = [
    {"name": "apple_health", "color": "blue"},
    {"name": "manual", "color": "orange"},
]


SCHEMAS: dict[str, dict[str, Any]] = {
    DB_TRAINING_PLAN: {
        "day_label":           {"title": {}},
        "muscle_groups":       {"multi_select": {"options": MUSCLE_GROUP_OPTIONS}},
        "suggested_exercises": {"rich_text": {}},
        "cardio":              {"checkbox": {}},
        "notes":               {"rich_text": {}},
        "active":              {"checkbox": {}},
    },
    DB_WORKOUTS: {
        "exercise":      {"title": {}},
        "date":          {"date": {}},
        "session_id":    {"rich_text": {}},
        "day_label":     {"rich_text": {}},
        "muscle_group":  {"multi_select": {"options": MUSCLE_GROUP_OPTIONS}},
        "sets":          {"number": {"format": "number"}},
        "reps":          {"number": {"format": "number"}},
        "weight_kg":     {"number": {"format": "number"}},
        "rpe":           {"number": {"format": "number"}},
        "notes":         {"rich_text": {}},
        "voice_note_sid":{"rich_text": {}},
    },
    DB_CARDIO: {
        "sport":               {"title": {}},
        "date":                {"date": {}},
        "duration_min":        {"number": {"format": "number"}},
        "distance_km":         {"number": {"format": "number"}},
        "avg_hr":              {"number": {"format": "number"}},
        "max_hr":              {"number": {"format": "number"}},
        "pace_min_per_km":     {"number": {"format": "number"}},
        "calories":            {"number": {"format": "number"}},
        "intensity":           {"select": {"options": INTENSITY_OPTIONS}},
        "rpe":                 {"number": {"format": "number"}},
        "notes":               {"rich_text": {}},
        "source":              {"select": {"options": SOURCE_OPTIONS}},
        "apple_workout_uuid":  {"rich_text": {}},
    },
    DB_ALIASES: {
        "alias":         {"title": {}},
        "canonical":     {"rich_text": {}},
        "muscle_group":  {"multi_select": {"options": MUSCLE_GROUP_OPTIONS}},
    },
}


# ── Variables de entorno con los IDs ────────────────────────────────────────
# Después del bootstrap inicial, los IDs se guardan en .env para reutilizar.
# En Fly los seteamos como secrets.

ENV_VAR_BY_DB = {
    DB_TRAINING_PLAN: "NOTION_DB_TRAINING_PLAN_ID",
    DB_WORKOUTS:      "NOTION_DB_WORKOUTS_ID",
    DB_CARDIO:        "NOTION_DB_CARDIO_ID",
    DB_ALIASES:       "NOTION_DB_ALIASES_ID",
}

# Notion 2025+ usa "data sources" para CRUD. Mantenemos los DB IDs por compat
# pero los accessors públicos prefieren el data_source_id.
ENV_VAR_DS_BY_DB = {
    DB_TRAINING_PLAN: "NOTION_DS_TRAINING_PLAN_ID",
    DB_WORKOUTS:      "NOTION_DS_WORKOUTS_ID",
    DB_CARDIO:        "NOTION_DS_CARDIO_ID",
    DB_ALIASES:       "NOTION_DS_ALIASES_ID",
}


# ── Cliente lazy ────────────────────────────────────────────────────────────

_client: _NotionSDKClient | None = None


def _sdk() -> _NotionSDKClient:
    global _client
    if _client is None:
        token = os.getenv("NOTION_TOKEN")
        if not token:
            raise RuntimeError("NOTION_TOKEN no está seteado en el entorno")
        _client = _NotionSDKClient(auth=token)
    return _client


# ── Page ID parsing ────────────────────────────────────────────────────────

_PAGE_ID_PATTERN = re.compile(r"([0-9a-f]{32})|([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.IGNORECASE)


def extract_page_id(url_or_id: str) -> str:
    """
    Acepta un URL completo de Notion o un ID raw (con o sin guiones) y
    devuelve el ID normalizado (sin guiones, 32 chars).

    >>> extract_page_id("https://www.notion.so/Oscar-abc123def456...")
    'abc123def456...'
    """
    if not url_or_id:
        raise ValueError("url_or_id vacío")
    m = _PAGE_ID_PATTERN.search(url_or_id)
    if not m:
        raise ValueError(f"No pude extraer page ID de: {url_or_id!r}")
    raw = (m.group(1) or m.group(2)).replace("-", "")
    return raw.lower()


# ── Bootstrap ──────────────────────────────────────────────────────────────

@dataclass
class BootstrapResult:
    created: dict[str, str]    # db_name → db_id
    existed: dict[str, str]    # db_name → db_id (ya existían, no se tocaron)
    errors:  dict[str, str]    # db_name → mensaje

    @property
    def all_db_ids(self) -> dict[str, str]:
        return {**self.created, **self.existed}


def _find_existing_db(parent_page_id: str, db_name: str) -> str | None:
    """
    Busca una DB con el `db_name` exacto entre los children directos de
    parent_page_id. Devuelve el ID si existe, None si no.
    """
    try:
        res = _sdk().blocks.children.list(block_id=parent_page_id, page_size=100)
        for block in res.get("results", []):
            if block.get("type") == "child_database":
                title = (block.get("child_database") or {}).get("title", "")
                if title == db_name:
                    return block["id"]
        return None
    except Exception as exc:
        log.warning("No pude listar children de %s: %s", parent_page_id, exc)
        return None


def _create_database(parent_page_id: str, db_name: str, properties: dict) -> str:
    """Crea una DB nueva dentro de parent_page_id. Devuelve el DB ID."""
    res = _sdk().databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": db_name}}],
        properties=properties,
    )
    return res["id"]


def bootstrap_databases(parent_page_id: str) -> BootstrapResult:
    """
    Crea las 4 DBs del módulo workout dentro de parent_page_id si no existen.

    Idempotente: si una DB ya existe (mismo nombre), no la duplica — devuelve
    su ID existente. Si Notion falla en una, sigue con las otras y reporta
    el error.

    Devuelve un BootstrapResult con created / existed / errors por DB.
    """
    parent_page_id = extract_page_id(parent_page_id)
    log.info("Bootstrap Notion DBs en page=%s", parent_page_id[:8] + "…")

    result = BootstrapResult(created={}, existed={}, errors={})

    for db_name, properties in SCHEMAS.items():
        try:
            existing = _find_existing_db(parent_page_id, db_name)
            if existing:
                log.info("DB %r ya existía (id=%s…)", db_name, existing[:8])
                result.existed[db_name] = existing
                continue
            new_id = _create_database(parent_page_id, db_name, properties)
            log.info("DB %r creada (id=%s…)", db_name, new_id[:8])
            result.created[db_name] = new_id
        except Exception as exc:
            log.exception("Fallo creando DB %r: %s", db_name, exc)
            result.errors[db_name] = str(exc)

    return result


def persist_db_ids_to_env_file(db_ids: dict[str, str], env_path: str = ".env") -> None:
    """
    Escribe los DB IDs al .env local para que persistan entre reinicios.
    En Fly los seteamos con `fly secrets set` en su lugar.

    Idempotente: si la var ya existe en .env, la reemplaza.
    """
    if not os.path.exists(env_path):
        log.warning("%s no existe, salteo persist_db_ids_to_env_file", env_path)
        return

    with open(env_path) as fh:
        lines = fh.readlines()

    updates = {ENV_VAR_BY_DB[name]: db_id for name, db_id in db_ids.items() if name in ENV_VAR_BY_DB}
    seen = set()
    new_lines = []
    for line in lines:
        for key in updates:
            if line.startswith(f"{key}="):
                new_lines.append(f"{key}={updates[key]}\n")
                seen.add(key)
                break
        else:
            new_lines.append(line)
    for key, val in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={val}\n")

    with open(env_path, "w") as fh:
        fh.writelines(new_lines)
    log.info("Persisted %d DB IDs to %s", len(updates), env_path)


# ── Accessors (lazy resolution via env vars) ───────────────────────────────

def _db_id(env_var: str) -> str | None:
    return os.getenv(env_var)


def workouts_db_id() -> str | None:
    """Devuelve el data_source_id (preferido) o el database_id (fallback)."""
    return _db_id("NOTION_DS_WORKOUTS_ID") or _db_id("NOTION_DB_WORKOUTS_ID")


def cardio_db_id() -> str | None:
    return _db_id("NOTION_DS_CARDIO_ID") or _db_id("NOTION_DB_CARDIO_ID")


def plan_db_id() -> str | None:
    return _db_id("NOTION_DS_TRAINING_PLAN_ID") or _db_id("NOTION_DB_TRAINING_PLAN_ID")


def aliases_db_id() -> str | None:
    return _db_id("NOTION_DS_ALIASES_ID") or _db_id("NOTION_DB_ALIASES_ID")


# ── Sub-DBs por día del Training Plan ──────────────────────────────────────
#
# Estructura: cada Día (1-5) tiene su propio sub-database adentro de la page
# del día. Los ejercicios viven ahí, agrupados por día.

def day_subdb_id(day_number: int) -> str | None:
    """Devuelve el data_source_id del sub-DB de ejercicios del Día N."""
    return _db_id(f"NOTION_DS_DIA_{day_number}_ID")


def all_day_subdbs() -> dict[int, str]:
    """Devuelve {1: ds_id, 2: ds_id, ...} con todos los Día N que están seteados."""
    result = {}
    for n in range(1, 10):  # support hasta Día 9
        ds_id = day_subdb_id(n)
        if ds_id:
            result[n] = ds_id
    return result


def find_day_for_muscles(target_muscles: list[str]) -> tuple[int, str] | None:
    """
    Busca qué día del Training Plan contiene los `target_muscles`.

    Reglas:
    1. Match exacto (target_muscles == day.muscle_groups) → ese día gana.
    2. Si target_muscles ⊆ day.muscle_groups → ese día gana.
    3. Si solo intersección parcial → primer día (orden ascendente) que
       contenga al menos uno.

    Devuelve (day_number, ds_id) o None si no hay match.
    """
    plan_id = plan_db_id()
    if not plan_id or not target_muscles:
        return None

    target_set = set(m.lower() for m in target_muscles)
    rows = query_db(plan_id, sorts=[{"property": "day_label", "direction": "ascending"}])

    exact_match = None
    subset_match = None
    intersect_match = None

    for row in rows:
        p = read_page_props(row)
        day_label = p.get("day_label") or ""
        day_muscles = set((m or "").lower() for m in (p.get("muscle_groups") or []))
        if not day_muscles:
            continue

        import re
        m = re.search(r"\d+", day_label)
        if not m:
            continue
        day_num = int(m.group(0))
        ds_id = day_subdb_id(day_num)
        if not ds_id:
            continue

        if day_muscles == target_set and exact_match is None:
            exact_match = (day_num, ds_id)
        elif target_set.issubset(day_muscles) and subset_match is None:
            subset_match = (day_num, ds_id)
        elif target_set & day_muscles and intersect_match is None:
            intersect_match = (day_num, ds_id)

    return exact_match or subset_match or intersect_match


# ── CRUD helpers (page-level) ──────────────────────────────────────────────
#
# Notion 2025+ exige que pages se creen con parent.data_source_id (no
# database_id). Si el llamador nos pasa un ID que parece de DB, intentamos
# usarlo igual con data_source_id como fallback.

def create_page_in_db(ds_id: str, properties: dict) -> dict | None:
    """
    Crea una fila en un data source. `ds_id` debe ser el data_source_id
    (las funciones workouts_db_id() etc. ya devuelven el DS por default).
    Devuelve la fila creada o None si falla.
    """
    if not ds_id:
        log.warning("create_page_in_db: ds_id vacío, ignoro")
        return None
    try:
        return _sdk().pages.create(
            parent={"type": "data_source_id", "data_source_id": ds_id},
            properties=properties,
        )
    except Exception as exc:
        log.exception("Notion create_page falló: %s", exc)
        return None


def update_page(page_id: str, properties: dict) -> dict | None:
    """Actualiza propiedades de una fila existente."""
    try:
        return _sdk().pages.update(page_id=page_id, properties=properties)
    except Exception as exc:
        log.exception("Notion update_page falló: %s", exc)
        return None


def query_db(ds_id: str, *, filter_: dict | None = None,
             sorts: list[dict] | None = None, page_size: int = 50) -> list[dict]:
    """
    Query a un data source. `ds_id` es el data_source_id.
    Devuelve la lista de results (puede estar vacía).
    """
    if not ds_id:
        return []
    try:
        kwargs: dict[str, Any] = {"data_source_id": ds_id, "page_size": page_size}
        if filter_:
            kwargs["filter"] = filter_
        if sorts:
            kwargs["sorts"] = sorts
        res = _sdk().data_sources.query(**kwargs)
        return res.get("results", [])
    except Exception as exc:
        log.exception("Notion query_db falló: %s", exc)
        return []


# ── Property helpers (boilerplate Notion → Python) ─────────────────────────

def prop_title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text}}]}


def prop_text(text: str | None) -> dict:
    if text is None:
        return {"rich_text": []}
    return {"rich_text": [{"type": "text", "text": {"content": text}}]}


def prop_number(value: float | int | None) -> dict:
    return {"number": value}


def prop_date(iso_date: str | None) -> dict:
    if not iso_date:
        return {"date": None}
    return {"date": {"start": iso_date}}


def prop_multi_select(values: list[str]) -> dict:
    return {"multi_select": [{"name": v} for v in (values or [])]}


def prop_select(value: str | None) -> dict:
    if not value:
        return {"select": None}
    return {"select": {"name": value}}


def prop_checkbox(value: bool) -> dict:
    return {"checkbox": bool(value)}


# ── Inverse helpers (Notion → simple dict) ─────────────────────────────────

def read_page_props(page: dict) -> dict[str, Any]:
    """
    Convierte una página Notion (dict crudo del SDK) a un dict plano de
    {nombre_prop: valor_python_simple}. Util para queries que leen filas.
    """
    out: dict[str, Any] = {"_id": page.get("id")}
    for name, prop in (page.get("properties") or {}).items():
        ptype = prop.get("type")
        if ptype == "title":
            out[name] = "".join(t.get("plain_text", "") for t in prop.get("title", []))
        elif ptype == "rich_text":
            out[name] = "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
        elif ptype == "number":
            out[name] = prop.get("number")
        elif ptype == "date":
            d = prop.get("date") or {}
            out[name] = d.get("start")
        elif ptype == "multi_select":
            out[name] = [opt.get("name") for opt in prop.get("multi_select", [])]
        elif ptype == "select":
            sel = prop.get("select")
            out[name] = sel.get("name") if sel else None
        elif ptype == "checkbox":
            out[name] = bool(prop.get("checkbox"))
        else:
            out[name] = prop  # passthrough para tipos no manejados
    return out
