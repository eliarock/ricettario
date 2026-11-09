"""Private Postgres storage for the single-owner ricettario."""
import os
from threading import Lock

DATABASE_URL = os.environ.get("RICETTARIO_DATABASE_URL")
_schema_lock = Lock()
_ready = False


def connect():
    import psycopg

    return psycopg.connect(DATABASE_URL, connect_timeout=5, sslmode="require")


def ensure_schema():
    global _ready
    with _schema_lock:
        if _ready:
            return
        with connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(721851403)")
            conn.execute("CREATE SCHEMA IF NOT EXISTS ricettario_private")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ricettario_private.recipes (
                    source_key text PRIMARY KEY,
                    recipe jsonb NOT NULL,
                    saved_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            conn.execute("ALTER TABLE ricettario_private.recipes ENABLE ROW LEVEL SECURITY")
        _ready = True


def list_recipes():
    ensure_schema()
    with connect() as conn:
        rows = conn.execute("""
            SELECT source_key, recipe FROM ricettario_private.recipes
            ORDER BY saved_at DESC, source_key
        """).fetchall()
    return [{"key": key, "recipe": recipe} for key, recipe in rows]


def save_recipes(entries, replace):
    from psycopg.types.json import Jsonb

    ensure_schema()
    if replace:
        conflict = "DO UPDATE SET recipe = EXCLUDED.recipe, saved_at = now()"
    else:
        conflict = "DO NOTHING"
    with connect() as conn, conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO ricettario_private.recipes (source_key, recipe) VALUES (%s, %s) "
            "ON CONFLICT (source_key) " + conflict,
            [(entry["key"], Jsonb(entry["recipe"])) for entry in entries],
        )
        if replace:
            return []
        stored = dict(conn.execute("SELECT source_key, recipe FROM ricettario_private.recipes WHERE source_key = ANY(%s)",
                                   ([entry["key"] for entry in entries],)).fetchall())
        return [entry["key"] for entry in entries if stored.get(entry["key"]) == entry["recipe"]]


def remove_recipe(key):
    ensure_schema()
    with connect() as conn:
        conn.execute("DELETE FROM ricettario_private.recipes WHERE source_key = %s", (key,))
