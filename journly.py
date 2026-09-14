#!/usr/bin/env python3
"""Journly - a lightweight, distraction-free journal.

Stdlib only. Serves a small local UI on 127.0.0.1 and stores each day's entry
as a plain Markdown file in entries/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import tempfile
import threading
import webbrowser
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
ENTRIES_DIR = ROOT / "entries"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PREVIEW_CHARS = 120
SEARCH_RESULT_LIMIT = 200

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
# HTTP
# --------------------------------------------------------------------------


class JournlyHandler(BaseHTTPRequestHandler):
    server_version = "Journly"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the terminal distraction-free
        pass

    # -- helpers ----------------------------------------------------------

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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

    # -- routing ----------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/"):
            return self.handle_api_get(path, parse_qs(parsed.query))
        return self.serve_static(path)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if not path.startswith("/api/entries/"):
            return self.send_error_json(404, "Not found")

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
        # sendBeacon on page unload cannot issue PUT, so it posts here instead.
        parsed = urlparse(self.path)
        if parsed.path == "/api/save-beacon":
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

    def handle_api_get(self, path, query):
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


def main():
    parser = argparse.ArgumentParser(description="Journly - a distraction-free journal")
    parser.add_argument("--port", type=int, default=8765, help="preferred port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    ENTRIES_DIR.mkdir(parents=True, exist_ok=True)

    port = find_open_port(args.port)
    url = f"http://127.0.0.1:{port}/"

    server = ThreadingHTTPServer(("127.0.0.1", port), JournlyHandler)
    print(f"Journly is running at {url}")
    print(f"Entries: {ENTRIES_DIR}")
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
