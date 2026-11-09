"""Anteprima locale. Avvia con RICETTARIO_PASSWORD impostata: python3 serve.py."""

import hmac
import json
import os
import re
import secrets
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from time import monotonic
from urllib.parse import parse_qs, urlsplit

import archive_db
from importer import CaptionExtractionFailed, RecipeNotFound, TranslationQuotaExceeded, import_recipe, italian_caption, suggested_categories, translate

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
LOGIN = INDEX.with_name("login.html")
USERNAME = "elia"
SESSION_COOKIE = "__Host-ricettario" if RENDER else "ricettario"
SESSION_AGE = 12 * 60 * 60
STATIC_FILES = {
    "/logo-elia.webp": "image/webp",
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
    if record.get("scheda"):
        return {**common, **record["scheda"], "translated": True}
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
    def session_token(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        token = cookies.get(SESSION_COOKIE)
        return token.value if token else None

    def authorized(self):
        token = self.session_token()
        with self.server.session_lock:
            return self.server.sessions.get(token, 0) > monotonic()

    def redirect_home(self, token="", max_age=0):
        secure = "; Secure" if RENDER else ""
        cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"
        self.reply(303, b"", headers=(("Location", "/"), ("Set-Cookie", cookie)))

    def login_page(self, status=200, error=""):
        content = LOGIN.read_text(encoding="utf-8")
        if error:
            content = content.replace("<!-- errore accesso -->", f'<p class="error" role="alert">{error}</p>')
        self.reply(status, content.encode(), "text/html; charset=utf-8",
                   headers=(("Content-Security-Policy", "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"),))

    def login(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16 * 1024:
                raise ValueError
            fields = parse_qs(self.rfile.read(length).decode("ascii"), strict_parsing=True,
                              errors="strict", max_num_fields=2)
            username, = fields["username"]
            password, = fields["password"]
        except (KeyError, ValueError):
            self.login_page(400, "Inserisci nome utente e password.")
            return

        password_matches = hmac.compare_digest(password.encode(), self.server.password)
        if username != USERNAME or not password_matches:
            self.login_page(401, "Nome utente o password non corretti.")
            return

        token = secrets.token_urlsafe(32)
        previous = self.session_token()
        now = monotonic()
        with self.server.session_lock:
            self.server.sessions = {key: expires for key, expires in self.server.sessions.items() if expires > now}
            self.server.sessions.pop(previous, None)
            self.server.sessions[token] = now + SESSION_AGE
        self.redirect_home(token, SESSION_AGE)

    def logout(self):
        token = self.session_token()
        with self.server.session_lock:
            self.server.sessions.pop(token, None)
        self.redirect_home()

    def reply(self, status, content, content_type="application/json; charset=utf-8", *, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(content)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in STATIC_FILES:
            self.reply(200, INDEX.with_name(path[1:]).read_bytes(), STATIC_FILES[path])
            return
        if RENDER and self.path == "/health":
            self.reply(200, b'{"status":"ok"}')
            return
        if self.headers.get("Host") != EXPECTED_HOST:
            self.send_error(403)
            return
        if path in {"/", "/login"}:
            if self.authorized():
                self.reply(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            else:
                self.login_page()
            return
        if self.path == "/api/archive":
            if self.authorized():
                self.archive_request()
            else:
                self.reply(401, b'{"error":"Accedi al ricettario."}')
            return
        self.send_error(404)

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
        if self.headers.get("Host") != EXPECTED_HOST:
            self.send_error(403)
            return
        if self.path in {"/login", "/logout"}:
            if self.headers.get("Origin") != ORIGIN or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                self.send_error(403)
                return
            if self.path == "/login":
                self.login()
            else:
                self.logout()
            return
        if not self.authorized():
            self.reply(401, b'{"error":"Accedi al ricettario."}')
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
            if not 0 < length <= 2 * 1024 * 1024:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            url = payload["url"]
            caption = payload.get("caption", "")
            screenshots = payload.get("screenshots", [])
            if not isinstance(screenshots, list) or len(screenshots) > 4 or any(
                not isinstance(data, str) or len(data) > 450000 or not re.fullmatch(r"[A-Za-z0-9+/]+=*", data)
                for data in screenshots
            ):
                raise ValueError
            if not isinstance(caption, str) or len(caption) > 12000 or len(url) > 4096:
                raise ValueError
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            self.reply(400, b'{"error":"Inserisci un link HTTPS valido."}')
            return
        try:
            record = import_recipe(url, caption_override=caption.strip() or None, screenshots=screenshots)
        except TranslationQuotaExceeded as error:
            self.reply(429, json.dumps({"error": str(error)}).encode())
            return
        except CaptionExtractionFailed as error:
            self.reply(502, json.dumps({"error": str(error)}).encode())
            return
        except RecipeNotFound as error:
            self.reply(422, json.dumps({"error": str(error)}).encode())
            return
        except (OSError, ValueError, KeyError, TypeError):
            self.reply(422, b'{"error":"Importazione fallita. Controlla il link e riprova."}')
            return
        try:
            recipe = preview(record)
            recipe["categorie"] = suggested_categories(record, recipe)
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
        server.password = password.encode()
        server.sessions = {}
        server.session_lock = Lock()
        print(f"Ricettario disponibile su {ORIGIN} (utente {USERNAME})", flush=True)
        server.serve_forever()
