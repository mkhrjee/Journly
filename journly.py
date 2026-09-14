#!/usr/bin/env python3
"""Journly - a lightweight, distraction-free journal.

Stdlib only. Serves a small local UI on 127.0.0.1 and stores each day's entry
as a plain Markdown file in entries/.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import http.cookies
import json
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
ENTRIES_DIR = ROOT / "entries"
AUTH_FILE = ROOT / ".journly_auth.json"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PREVIEW_CHARS = 120
SEARCH_RESULT_LIMIT = 200

SESSION_COOKIE = "journly_session"
# Explicit logout on page close (see /api/auth/logout beacon in app.js) is the
# primary way sessions end. This is only a fallback ceiling for cases where
# that beacon never fires (browser crash, force-quit, etc.).
SESSION_DURATION = 1800  # 30 minutes, not extended by activity
PBKDF2_ITERATIONS = 200_000

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}

# Serialize writes so concurrent saves to the same file cannot interleave.
_write_lock = threading.Lock()


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def valid_date(value: str) -> bool:
    """True only for a well-formed, real calendar date in YYYY-MM-DD form."""
    if not DATE_RE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def entry_path(entry_date: str) -> Path:
    """Resolve a date to its file. Callers must have validated the date."""
    return ENTRIES_DIR / f"{entry_date}.md"


def word_count(text: str) -> int:
    return len(text.split())


def derive_preview(text: str) -> str:
    """First non-empty line, trimmed to a sidebar-sized snippet."""
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            if len(stripped) > PREVIEW_CHARS:
                return stripped[:PREVIEW_CHARS].rstrip() + "\u2026"
            return stripped
    return ""


def read_entry(entry_date: str) -> str:
    path = entry_path(entry_date)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_entry(entry_date: str, content: str) -> bool:
    """Persist an entry atomically.

    An entry that is empty (or only whitespace) is never kept on disk, so the
    sidebar does not accumulate blank days. Returns True if a file now exists.
    """
    path = entry_path(entry_date)
    with _write_lock:
        if not content.strip():
            if path.exists():
                path.unlink()
            return False

        ENTRIES_DIR.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory, then atomically swap it
        # into place so an interrupted save can never truncate the entry.
        fd, tmp_name = tempfile.mkstemp(dir=str(ENTRIES_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return True


def entry_summary(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return {
        "date": path.stem,
        "preview": derive_preview(text),
        "words": word_count(text),
        "updated": path.stat().st_mtime,
    }


def list_entries() -> list[dict]:
    if not ENTRIES_DIR.exists():
        return []
    summaries = []
    for path in ENTRIES_DIR.glob("*.md"):
        if not valid_date(path.stem):
            continue
        try:
            summaries.append(entry_summary(path))
        except OSError:
            continue
    summaries.sort(key=lambda item: item["date"], reverse=True)
    return summaries


def search_entries(query: str) -> list[dict]:
    """Case-insensitive substring search, returning the first matching line."""
    needle = query.strip().lower()
    if not needle:
        return []

    results = []
    for summary in list_entries():
        path = entry_path(summary["date"])
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if needle not in text.lower():
            continue

        match_line = ""
        for line in text.splitlines():
            if needle in line.lower():
                match_line = line.strip()
                break
        if len(match_line) > PREVIEW_CHARS:
            match_line = match_line[:PREVIEW_CHARS].rstrip() + "\u2026"

        hit = dict(summary)
        hit["preview"] = match_line or summary["preview"]
        results.append(hit)
        if len(results) >= SEARCH_RESULT_LIMIT:
            break
    return results


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
#
# A single local password gates the journal. The password is never stored in
# plaintext: only a salted PBKDF2 hash lives in AUTH_FILE, alongside a random
# session secret used to sign session cookies. Sessions are stateless signed
# tokens (expiry + HMAC), not server-side session storage, so restarting the
# server does not require re-implementing anything - it just re-validates.

_auth_lock = threading.Lock()


def load_auth() -> dict | None:
    if not AUTH_FILE.exists():
        return None
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def auth_configured() -> bool:
    return load_auth() is not None


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()


def set_password(password: str) -> None:
    """Create or overwrite the stored password. Generates a fresh session
    secret too, which invalidates any previously issued session cookies."""
    salt = secrets.token_bytes(16)
    data = {
        "salt": salt.hex(),
        "hash": hash_password(password, salt),
        "session_secret": secrets.token_hex(32),
    }
    with _auth_lock:
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(AUTH_FILE.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data))
        os.replace(tmp_name, AUTH_FILE)
        try:
            os.chmod(AUTH_FILE, 0o600)
        except OSError:
            pass


def verify_password(password: str) -> bool:
    auth = load_auth()
    if auth is None:
        return False
    expected = auth.get("hash", "")
    salt = bytes.fromhex(auth.get("salt", ""))
    candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, expected)


def _session_secret() -> str | None:
    auth = load_auth()
    return auth.get("session_secret") if auth else None


def make_session_token() -> str | None:
    secret = _session_secret()
    if secret is None:
        return None
    expiry = int(time.time()) + SESSION_DURATION
    signature = hmac.new(secret.encode("utf-8"), str(expiry).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{expiry}.{signature}"


def verify_session_token(token: str) -> bool:
    secret = _session_secret()
    if secret is None or not token or "." not in token:
        return False
    expiry_str, _, signature = token.partition(".")
    if not expiry_str.isdigit():
        return False
    expected = hmac.new(secret.encode("utf-8"), expiry_str.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False
    return int(expiry_str) > int(time.time())


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class JournlyHandler(BaseHTTPRequestHandler):
    server_version = "Journly"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the terminal distraction-free
        pass

    # -- helpers ----------------------------------------------------------

    def send_json(self, payload, status=200, set_cookie=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie is not None:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"error": message}, status=status)

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    # -- auth ---------------------------------------------------------------

    def session_token_from_request(self) -> str | None:
        header = self.headers.get("Cookie")
        if not header:
            return None
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(header)
        except http.cookies.CookieError:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def is_authenticated(self) -> bool:
        token = self.session_token_from_request()
        return bool(token) and verify_session_token(token)

    def session_cookie_header(self, token: str | None) -> str:
        cookie = http.cookies.SimpleCookie()
        if token is None:
            cookie[SESSION_COOKIE] = ""
            cookie[SESSION_COOKIE]["max-age"] = 0
        else:
            cookie[SESSION_COOKIE] = token
            cookie[SESSION_COOKIE]["max-age"] = SESSION_DURATION
        cookie[SESSION_COOKIE]["path"] = "/"
        cookie[SESSION_COOKIE]["httponly"] = True
        cookie[SESSION_COOKIE]["samesite"] = "Strict"
        # Output only the Set-Cookie value portion (SimpleCookie prefixes
        # "Set-Cookie: " which send_header already adds for us).
        return cookie[SESSION_COOKIE].OutputString()

    def require_auth(self) -> bool:
        """If not authenticated, writes a 401 JSON response and returns False."""
        if not auth_configured():
            self.send_error_json(403, "Password not set up yet")
            return False
        if not self.is_authenticated():
            self.send_error_json(401, "Not authenticated")
            return False
        return True

    # -- routing ----------------------------------------------------------

    PUBLIC_API_PATHS = {"/api/auth/status", "/api/auth/setup", "/api/auth/login"}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/"):
            if path not in self.PUBLIC_API_PATHS and not self.require_auth():
                return
            return self.handle_api_get(path, parse_qs(parsed.query))
        return self.serve_static(path)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if not path.startswith("/api/entries/"):
            return self.send_error_json(404, "Not found")
        if not self.require_auth():
            return

        entry_date = unquote(path[len("/api/entries/"):])
        if not valid_date(entry_date):
            return self.send_error_json(400, "Invalid date")

        payload = self.read_json_body()
        if payload is None or not isinstance(payload, dict):
            return self.send_error_json(400, "Invalid JSON body")

        content = payload.get("content", "")
        if not isinstance(content, str):
            return self.send_error_json(400, "content must be a string")

        try:
            exists = write_entry(entry_date, content)
        except OSError as exc:
            return self.send_error_json(500, f"Could not save entry: {exc}")

        return self.send_json(
            {
                "date": entry_date,
                "saved": True,
                "exists": exists,
                "words": word_count(content),
                "preview": derive_preview(content),
            }
        )

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/auth/setup":
            return self.handle_auth_setup()
        if path == "/api/auth/login":
            return self.handle_auth_login()
        if path == "/api/auth/logout":
            return self.handle_auth_logout()

        if path == "/api/save-beacon":
            if not self.require_auth():
                return
            # sendBeacon on page unload cannot issue PUT, so it posts here instead.
            payload = self.read_json_body()
            if not isinstance(payload, dict):
                return self.send_error_json(400, "Invalid JSON body")
            entry_date = payload.get("date", "")
            content = payload.get("content", "")
            if not isinstance(entry_date, str) or not valid_date(entry_date):
                return self.send_error_json(400, "Invalid date")
            if not isinstance(content, str):
                return self.send_error_json(400, "content must be a string")
            try:
                write_entry(entry_date, content)
            except OSError as exc:
                return self.send_error_json(500, f"Could not save entry: {exc}")
            return self.send_json({"saved": True})
        return self.send_error_json(404, "Not found")

    # -- auth endpoints -----------------------------------------------------

    def handle_auth_setup(self):
        if auth_configured():
            return self.send_error_json(409, "Password already set up")
        payload = self.read_json_body()
        password = (payload or {}).get("password", "")
        if not isinstance(password, str) or len(password) < 4:
            return self.send_error_json(400, "Password must be at least 4 characters")
        set_password(password)
        token = make_session_token()
        return self.send_json({"ok": True}, set_cookie=self.session_cookie_header(token))

    def handle_auth_login(self):
        if not auth_configured():
            return self.send_error_json(403, "Password not set up yet")
        payload = self.read_json_body()
        password = (payload or {}).get("password", "")
        if not isinstance(password, str) or not verify_password(password):
            return self.send_error_json(401, "Incorrect password")
        token = make_session_token()
        return self.send_json({"ok": True}, set_cookie=self.session_cookie_header(token))

    def handle_auth_logout(self):
        return self.send_json({"ok": True}, set_cookie=self.session_cookie_header(None))

    def handle_api_get(self, path, query):
        if path == "/api/auth/status":
            return self.send_json(
                {"configured": auth_configured(), "authenticated": self.is_authenticated()}
            )

        if path == "/api/entries":
            return self.send_json({"entries": list_entries(), "today": date.today().isoformat()})

        if path == "/api/search":
            q = (query.get("q") or [""])[0]
            return self.send_json({"entries": search_entries(q), "query": q})

        if path.startswith("/api/entries/"):
            entry_date = unquote(path[len("/api/entries/"):])
            if not valid_date(entry_date):
                return self.send_error_json(400, "Invalid date")
            content = read_entry(entry_date)
            return self.send_json(
                {"date": entry_date, "content": content, "words": word_count(content)}
            )

        return self.send_error_json(404, "Not found")

    def serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"

        # Resolve inside web/ and reject anything that escapes it.
        candidate = (WEB_DIR / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self.send_error_json(403, "Forbidden")

        if not candidate.is_file():
            return self.send_error_json(404, "Not found")

        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", CONTENT_TYPES.get(candidate.suffix, "application/octet-stream")
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def find_open_port(preferred: int, host: str = "127.0.0.1") -> int:
    """Return the preferred port, or the next free one if it is taken."""
    for candidate in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit(f"No free port found near {preferred}")


def prompt_set_password() -> None:
    """Interactively set the password from the terminal (input is hidden)."""
    if auth_configured():
        confirm = input("A password is already set. Replace it? [y/N] ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return
    while True:
        pw1 = getpass.getpass("New Journly password: ")
        if len(pw1) < 4:
            print("Password must be at least 4 characters.")
            continue
        pw2 = getpass.getpass("Confirm password: ")
        if pw1 != pw2:
            print("Passwords did not match, try again.")
            continue
        break
    set_password(pw1)
    print("Password set. Existing browser sessions are now signed out.")


def main():
    parser = argparse.ArgumentParser(description="Journly - a distraction-free journal")
    parser.add_argument("--port", type=int, default=8765, help="preferred port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--set-password",
        action="store_true",
        help="set or replace the journal password (prompts securely, then exits)",
    )
    args = parser.parse_args()

    if args.set_password:
        prompt_set_password()
        sys.exit(0)

    ENTRIES_DIR.mkdir(parents=True, exist_ok=True)

    port = find_open_port(args.port)
    url = f"http://127.0.0.1:{port}/"

    server = ThreadingHTTPServer(("127.0.0.1", port), JournlyHandler)
    print(f"Journly is running at {url}")
    print(f"Entries: {ENTRIES_DIR}")
    if not auth_configured():
        print("No password set yet - you'll be asked to create one in the browser.")
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
