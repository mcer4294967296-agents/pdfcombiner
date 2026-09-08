#!/usr/bin/env python3
"""PDF Combiner GUI — drag-and-drop web interface.

Local use:   ./gui.py        (random port, opens a browser, exits when idle)
Server use:  PDFCOMBINER_SERVER=1 PDFCOMBINER_PORT=10233 ./gui.py
             (fixed port, headless, no idle shutdown; for systemd behind nginx)
"""

import http.server
import io
import json
import os
import re
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from pypdf import PdfReader, PdfWriter

# ── State ──────────────────────────────────────────────────────────────
upload_dir = None
uploaded = {}  # id -> {path, name, num_pages}
last_heartbeat = time.time()

STATIC_DIR = Path(__file__).parent / "static"
LOCKFILE = Path(tempfile.gettempdir()) / "pdfcombiner.lock"
HEARTBEAT_TIMEOUT = 300  # 5 minutes

# Headless server mode (systemd behind a reverse proxy): fixed port, no browser,
# no idle self-shutdown, no single-session lockfile.
SERVER_MODE = os.environ.get("PDFCOMBINER_SERVER") == "1"
ENV_PORT = int(os.environ.get("PDFCOMBINER_PORT") or 0)


# ── Page spec parsing (shared logic with CLI) ─────────────────────────
def parse_page_spec(spec, total_pages):
    pages = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if start < 1 or end < 1 or start > total_pages or end > total_pages:
                raise ValueError(f"Range {token} out of bounds (1-{total_pages})")
            if start <= end:
                pages.extend(range(start - 1, end))
            else:
                pages.extend(range(start - 1, end - 2, -1))
        else:
            p = int(token)
            if p < 1 or p > total_pages:
                raise ValueError(f"Page {p} out of bounds (1-{total_pages})")
            pages.append(p - 1)
    return pages


# ── HTTP Handler ───────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress request logs

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, msg):
        self._send_json({"error": msg}, status)

    def _send_file(self, path, content_type, filename=None):
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(data))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    # ── GET ────────────────────────────────────────────────────────────
    def do_GET(self):
        global last_heartbeat
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")

        elif path == "/api/heartbeat":
            last_heartbeat = time.time()
            self._send_json({"ok": True})

        elif path.startswith("/api/pdf/"):
            file_id = path.split("/")[-1]
            if file_id not in uploaded:
                self._send_error(404, "File not found")
                return
            self._send_file(uploaded[file_id]["path"], "application/pdf")

        else:
            self._send_error(404, "Not found")

    # ── POST ───────────────────────────────────────────────────────────
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/upload":
            params = parse_qs(parsed.query)
            name = unquote(params.get("name", ["unknown.pdf"])[0])
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)

            file_id = str(uuid.uuid4())[:8]
            file_path = os.path.join(upload_dir, f"{file_id}.pdf")
            with open(file_path, "wb") as f:
                f.write(body)

            try:
                reader = PdfReader(file_path)
                num_pages = len(reader.pages)
            except Exception as e:
                os.unlink(file_path)
                self._send_error(400, f"Invalid PDF: {e}")
                return

            uploaded[file_id] = {"path": file_path, "name": name, "num_pages": num_pages}
            self._send_json({"id": file_id, "name": name, "num_pages": num_pages})

        elif path == "/api/combine":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            entries = body.get("entries", [])

            if not entries:
                self._send_error(400, "No entries provided")
                return

            writer = PdfWriter()
            total_added = 0
            try:
                for entry in entries:
                    file_id = entry["id"]
                    page_spec = entry.get("pages", "").strip()

                    if file_id not in uploaded:
                        raise ValueError(f"Unknown file: {file_id}")

                    info = uploaded[file_id]
                    reader = PdfReader(info["path"])

                    if not page_spec:
                        pages = list(range(len(reader.pages)))
                    else:
                        pages = parse_page_spec(page_spec, len(reader.pages))

                    for p in pages:
                        writer.add_page(reader.pages[p])
                        total_added += 1

                buf = io.BytesIO()
                writer.write(buf)
                data = buf.getvalue()

                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'attachment; filename="combined.pdf"')
                self.send_header("Content-Length", len(data))
                self.end_headers()
                self.wfile.write(data)
                print(f"  ✓ Combined {total_added} pages")
            except Exception as e:
                self._send_error(400, str(e))

        elif path == "/api/remove":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            file_id = body.get("id", "")
            if file_id in uploaded:
                try:
                    os.unlink(uploaded[file_id]["path"])
                except OSError:
                    pass
                del uploaded[file_id]
            self._send_json({"ok": True})

        else:
            self._send_error(404, "Not found")


# ── Heartbeat watchdog ─────────────────────────────────────────────────
def watchdog():
    while True:
        time.sleep(10)
        elapsed = time.time() - last_heartbeat
        if elapsed > HEARTBEAT_TIMEOUT:
            print(f"\n⏹  No browser activity for {HEARTBEAT_TIMEOUT}s. Shutting down.")
            cleanup()
            os._exit(0)


def cleanup():
    if upload_dir and os.path.isdir(upload_dir):
        shutil.rmtree(upload_dir, ignore_errors=True)
    try:
        LOCKFILE.unlink(missing_ok=True)
    except OSError:
        pass


# ── Session detection ──────────────────────────────────────────────────
def _check_existing_session() -> str | None:
    """Return the URL of a running session, or None."""
    if not LOCKFILE.exists():
        return None
    try:
        content = LOCKFILE.read_text().strip()
        pid_str, url = content.split("\n", 1)
        pid = int(pid_str)
        # Check if process is still alive
        os.kill(pid, 0)
        # Check if server actually responds
        import urllib.request
        resp = urllib.request.urlopen(url + "/api/heartbeat", timeout=2)
        if resp.status == 200:
            return url
    except (ValueError, OSError, Exception):
        # Stale lockfile or dead process
        LOCKFILE.unlink(missing_ok=True)
    return None


def _write_lockfile(port: int) -> None:
    LOCKFILE.write_text(f"{os.getpid()}\nhttp://127.0.0.1:{port}")


# ── Entry point ────────────────────────────────────────────────────────
def run_gui():
    global upload_dir, last_heartbeat

    if not SERVER_MODE:
        # Reuse an already-running local session if there is one
        existing_url = _check_existing_session()
        if existing_url:
            print(f"✦ PDF Combiner is already running")
            print(f"  → {existing_url}")
            webbrowser.open(existing_url)
            return

    upload_dir = tempfile.mkdtemp(prefix="pdfcombiner_")
    last_heartbeat = time.time()

    if ENV_PORT:
        port = ENV_PORT
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    if not SERVER_MODE:
        _write_lockfile(port)

    def _shutdown(*_):
        print("\n⏹  Shutting down.")
        cleanup()
        sys.exit(0)

    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _shutdown)
        except ValueError:
            pass  # not in the main thread

    if not SERVER_MODE:
        threading.Thread(target=watchdog, daemon=True).start()

    url = f"http://127.0.0.1:{port}"
    if SERVER_MODE:
        print(f"✦ PDF Combiner (server mode) → {url}", flush=True)
    else:
        print(f"✦ PDF Combiner GUI")
        print(f"  → {url}")
        print(f"  (auto-shuts down after {HEARTBEAT_TIMEOUT // 60}min of inactivity)")
        webbrowser.open(url)

    server.serve_forever()


if __name__ == "__main__":
    run_gui()
