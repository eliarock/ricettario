"""Anteprima locale. Avvia con RICETTARIO_PASSWORD impostata: python3 serve.py."""

import base64
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import archive_db
from importer import RecipeNotFound, TranslationQuotaExceeded, import_recipe, italian_caption, translate

RENDER = os.environ.get("RENDER") == "true"
HOST = "0.0.0.0" if RENDER else "127.0.0.1"
PORT = int(os.environ.get("PORT", "8765")) if RENDER else 8765
def render_origin():
    external_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    parsed = urlsplit(external_url)
    if parsed.scheme == "https" and parsed.hostname and not (parsed.path or parsed.query or parsed.fragment or parsed.username):
        return external_url

    hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if hostname.endswith(".onrender.com") and hostname.removesuffix(".onrender.com").replace("-", "").isalnum():
        return f"https://{hostname}"
    return ""


ORIGIN = render_origin() if RENDER else f"http://{HOST}:{PORT}"
EXPECTED_HOST = urlsplit(ORIGIN).netloc
INDEX = Path(__file__).with_name("index.html")
ICONS = {
    "/favicon.svg": "image/svg+xml",
    "/favicon.ico": "image/x-icon",
    "/icon-192.png": "image/png",
    "/icon-512.png": "image/png",
    "/manifest.webmanifest": "application/manifest+json",
    "/apple-touch-icon.png": "image/png",
}


def photo(value):
    if isinstance(value, list):
        return photo(value[0]) if value else None
    if isinstance(value, dict):
        return photo(value.get("url") or value.get("contentUrl"))
    if isinstance(value, str) and value.startswith("https://"):
        return value


def steps(value):
    if isinstance(value, list):
        return [step for item in value for step in steps(item)]
    if isinstance(value, dict):
        return steps(value.get("itemListElement") or value.get("text") or "")
    return [value] if isinstance(value, str) and value else []


class TranslationUnavailable(Exception):
    pass


def preview(record):
    raw = record.get("recipe", {})
    common = {
        "author": record.get("author"),
        "reel": record.get("reel"),
        "blog": record.get("blog"),
        "image": photo(raw.get("image")) or photo(record.get("image")),
        "needs_confirmation": record["needs_confirmation"],
    }
    language = record.get("language")
    if isinstance(language, str) and language.lower().replace("_", "-").split("-", 1)[0] == "it":
        return {
            **common,
            "titolo": raw.get("name"),
            "ingredienti": raw.get("recipeIngredient", []),
            "passaggi": steps(raw.get("recipeInstructions")),
            "porzioni": raw.get("recipeYield"),
            "tempi": {label: raw[key] for label, key in (
                ("Preparazione", "prepTime"), ("Cottura", "cookTime"), ("Totale", "totalTime")
            ) if raw.get(key)},
            "translated": True,
        }
    original = italian_caption(record.get("caption", ""))
    if original:
        return {**common, **original, "translated": True}
    if os.getenv("GEMINI_API_KEY"):
        return {**common, **translate(record)["scheda"], "translated": True}
    if RENDER:
        raise TranslationUnavailable
    return {
        **common,
        "titolo": raw.get("name") or record.get("title"),
        "ingredienti": raw.get("recipeIngredient", []),
        "passaggi": steps(raw.get("recipeInstructions")),
        "descrizione": record.get("caption"),
        "translated": False,
    }


class Handler(BaseHTTPRequestHandler):
    def authorized(self):
        if self.headers.get("Host") != EXPECTED_HOST:
            self.send_error(403)
            return False
        supplied = self.headers.get("Authorization", "").encode()
        if hmac.compare_digest(supplied, self.server.authorization):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Ricettario", charset="UTF-8"')
        self.end_headers()
        return False

    def reply(self, status, content, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ICONS:
            self.reply(200, INDEX.with_name(path[1:]).read_bytes(), ICONS[path])
            return
        if RENDER and self.path == "/health":
            self.reply(200, b'{"status":"ok"}')
            return
        if not self.authorized():
            return
        if self.path == "/api/archive":
            self.archive_request()
            return
        if self.path != "/":
            self.send_error(404)
            return
        self.reply(200, INDEX.read_bytes(), "text/html; charset=utf-8")

    def archive_request(self):
        if not archive_db.DATABASE_URL:
            self.reply(200, b'{"configured":false}')
            return
        try:
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4 * 1024 * 1024:
                    raise ValueError
                payload = json.loads(self.rfile.read(length))
                action = payload["action"]
                if action == "remove":
                    key = payload["key"]
                    if not isinstance(key, str) or not 0 < len(key) <= 2048:
                        raise ValueError
                elif action in {"save", "migrate"}:
                    entries = payload["entries"]
                    if not isinstance(entries, list) or not entries or (action == "save" and len(entries) != 1):
                        raise ValueError
                    for entry in entries:
                        key = entry["key"]
                        recipe = entry["recipe"]
                        if not isinstance(key, str) or not 0 < len(key) <= 2048 or not isinstance(recipe, dict):
                            raise ValueError
                        if not isinstance(recipe.get("titolo") or "Ricetta senza titolo", str):
                            raise ValueError
                else:
                    raise ValueError
        except (KeyError, TypeError, ValueError):
            self.reply(400, b'{"error":"Dati archivio non validi."}')
            return

        try:
            if self.command == "POST":
                transferred = []
                if action == "remove":
                    archive_db.remove_recipe(key)
                else:
                    transferred = archive_db.save_recipes(entries, replace=action == "save")
                self.reply(200, json.dumps({"configured": True, "transferred": transferred}).encode())
                return
            entries = archive_db.list_recipes()
            self.reply(200, json.dumps({"configured": True, "entries": entries}).encode())
        except Exception as error:
            # Database exceptions can contain connection parameters. Log only the type.
            self.log_error("Archivio: %s", type(error).__name__)
            self.reply(503, b'{"error":"Archivio online non disponibile. Riprova tra poco."}')

    def do_POST(self):
        if not self.authorized():
            return
        if self.path == "/api/archive":
            if self.headers.get("Origin") != ORIGIN or self.headers.get_content_type() != "application/json":
                self.send_error(403)
                return
            self.archive_request()
            return
        if self.path != "/api/import":
            self.send_error(404)
            return
        if self.headers.get("Origin") != ORIGIN or self.headers.get_content_type() != "application/json":
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError
            url = json.loads(self.rfile.read(length))["url"]
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            self.reply(400, b'{"error":"Inserisci un link HTTPS valido."}')
            return
        try:
            record = import_recipe(url)
        except RecipeNotFound as error:
            self.reply(422, json.dumps({"error": str(error)}).encode())
            return
        except (OSError, ValueError, KeyError, TypeError):
            self.reply(422, b'{"error":"Importazione fallita. Controlla il link e riprova."}')
            return
        try:
            recipe = preview(record)
        except TranslationQuotaExceeded as error:
            self.reply(429, json.dumps({"error": str(error)}).encode())
            return
        except TranslationUnavailable:
            message = "Ricetta trovata. Per elaborarla in italiano, configura GEMINI_API_KEY su Render."
            self.reply(503, json.dumps({"error": message}).encode())
            return
        except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
            self.log_error("Traduzione: %s", type(error).__name__)
            message = "Ricetta trovata, ma la traduzione in italiano non è riuscita. Riprova o controlla la configurazione API su Render."
            self.reply(502, json.dumps({"error": message}).encode())
            return
        self.reply(200, json.dumps(recipe, ensure_ascii=False).encode())


if __name__ == "__main__":
    password = os.environ.get("RICETTARIO_PASSWORD")
    if not password:
        raise SystemExit("Imposta RICETTARIO_PASSWORD e avvia nuovamente il server.")
    if RENDER and len(password) < 8:
        raise SystemExit("RICETTARIO_PASSWORD deve contenere almeno 8 caratteri.")
    if RENDER and not EXPECTED_HOST:
        raise SystemExit("URL pubblico Render mancante: controlla RENDER_EXTERNAL_URL e RENDER_EXTERNAL_HOSTNAME.")
    with ThreadingHTTPServer((HOST, PORT), Handler) as server:
        server.authorization = b"Basic " + base64.b64encode(f"elia:{password}".encode())
        print(f"Ricettario disponibile su {ORIGIN} (utente elia)", flush=True)
        server.serve_forever()
