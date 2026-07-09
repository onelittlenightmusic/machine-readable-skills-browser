"""mrs-browser: shared browser_run() helper for MyWant machine-readable-skill
plugins (~/.mywant/custom-types/*).

Runs a @puppeteer/replay UserFlow's Step[] JSON (plus mywant's own
read/readAll/loop/forEachClick/if/setResult/sleep/reactChange customStep
extensions — see mcp/playwright-app/webext-src/browser-run-interpreter.ts)
against a URL via the mywant browser extension. This is the CDP-free
replacement for playwright.chromium.connect_over_cdp: the extension opens a
tab in the user's own (already logged-in) browser, runs the steps, and
returns whatever the interpreter collected into its result object.

Two execution modes, chosen automatically — a plugin script only ever calls
browser_run(); it never needs to know which one ran:

  1. Via the mywant server: POST /api/v1/web-wants/browser-run. The server
     queues the request and blocks until the extension (polling
     GET /api/v1/web-wants/pending-action on its own 1-minute alarm — see
     background.js's drainPendingActions) picks it up, runs it, and POSTs
     the result back to /browser-run-result.

  2. Standalone (mywant server not running): this module stands up a tiny
     HTTP server on the exact same host:port mywant normally listens on,
     implementing just those same two endpoints
     (GET pending-action / POST browser-run-result) with the identical
     JSON shapes handlers_web_wants.go's pendingActionResponse/
     browserRunClaim/browserRunResult use — same FIFO-queue-plus-pending-map
     design as the Go server's claimQueue/browserRunPending, just
     reimplemented here. The extension's own polling code doesn't — and
     can't — tell the difference between the real mywant server and this
     stand-in, so background.js/manifest.json need no changes at all for
     this to work. Only meant for one-shot browser_run usage (no
     auto-launch/nav-launch claims are ever served), which is all a
     standalone plugin script needs.

     The stand-in server is a per-process singleton, started lazily on the
     first standalone call and left running for the rest of the process —
     needed because a single script can call browser_run() several times,
     including concurrently (e.g. smartgolf-list-plugin's per-location
     ThreadPoolExecutor): tearing the server down after each call would
     make concurrent callers race to rebind the same port, and restarting
     per call would cost each one a fresh alarm-tick wait for the
     extension to reconnect.

Mode selection is self-correcting: try mode 1 first; if the TCP connection
itself is refused/times out (nothing listening), fall back to mode 2; if
starting mode 2's stand-in server then fails to bind with "address already
in use" by something outside this process, that means mode 1's probe was a
false negative (the real server actually is up, just was momentarily slow)
— retry mode 1 once more.
"""

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

MYWANT_API = os.environ.get("MYWANT_URL", "http://localhost:8080")

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS, PATCH",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With, Accept, Origin, If-None-Match",
}


def xpath_literal(s: str) -> str:
    """XPath 1.0 has no string-literal escape, so a value containing both
    quote types needs the concat() trick — same problem CSS/SQL string
    literals have, just without a backslash escape to fall back on."""
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    parts = s.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"


class _Mode1Unreachable(Exception):
    """The mywant server's TCP connection itself failed (refused/timed
    out) — distinct from a real error response, which should propagate to
    the caller as-is rather than trigger a mode-2 fallback."""


def _post_browser_run(origin: str, payload: dict, timeout_ms: int) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{origin}/api/v1/web-wants/browser-run",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=(timeout_ms / 1000) + 10) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        # ConnectionRefusedError/socket.timeout at connect time surface as
        # URLError wrapping an OSError — a real HTTP error response (4xx/5xx)
        # instead raises HTTPError, a URLError subclass, so check the type
        # rather than isinstance(e, HTTPError) to catch both refused and
        # timed-out connection attempts uniformly.
        if isinstance(e, urllib.error.HTTPError):
            raise
        raise _Mode1Unreachable(str(e)) from e


class _Broker:
    """FIFO claim queue + request_id-keyed result delivery, shared by every
    browser_run() call in this process once standalone mode is active —
    mirrors handlers_web_wants.go's claimQueue + browserRunPending map."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: list[dict] = []
        self._events: dict[str, threading.Event] = {}
        self._results: dict[str, dict] = {}

    def enqueue(self, claim: dict) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._queue.append(claim)
            self._events[claim["request_id"]] = event
        return event

    def dequeue(self) -> dict | None:
        with self._lock:
            if not self._queue:
                return None
            return self._queue.pop(0)

    def deliver(self, request_id: str, result: dict) -> None:
        with self._lock:
            self._results[request_id] = result
            event = self._events.get(request_id)
        if event:
            event.set()

    def pop_result(self, request_id: str) -> dict:
        with self._lock:
            self._events.pop(request_id, None)
            return self._results.pop(request_id, {})


def _make_handler_cls(broker: _Broker) -> type:
    class _StandaloneHandler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in _CORS_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):  # noqa: N802 (BaseHTTPRequestHandler naming)
            self.send_response(200)
            for k, v in _CORS_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/v1/web-wants/pending-action"):
                claim = broker.dequeue()
                if claim is not None:
                    self._send_json(200, {"kind": "browser_run", "browser_run": claim})
                else:
                    self._send_json(200, {"kind": ""})
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path.startswith("/api/v1/web-wants/browser-run-result"):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b"{}"
                data = json.loads(body) if body else {}
                request_id = data.get("request_id", "")
                if request_id:
                    broker.deliver(request_id, data)
                self._send_json(200, {"ok": True})
                return
            self._send_json(404, {"error": "not found"})

        def log_message(self, format, *args):  # noqa: A002 - silence stdlib default stderr logging
            pass

    return _StandaloneHandler


_singleton_lock = threading.Lock()
_singleton_broker: _Broker | None = None
_singleton_server: HTTPServer | None = None


def _ensure_standalone_server(host: str, port: int) -> _Broker:
    """Starts the per-process stand-in server on first call; later calls
    (including from other threads) reuse the same running instance.
    Raises OSError if the port is held by something outside this process
    (see this module's docstring on the mode-1-retry fallback that
    triggers)."""
    global _singleton_broker, _singleton_server
    with _singleton_lock:
        if _singleton_server is not None:
            return _singleton_broker
        broker = _Broker()
        server = HTTPServer((host, port), _make_handler_cls(broker))  # raises OSError if the port's taken
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _singleton_broker = broker
        _singleton_server = server
        return broker


def _run_standalone(host: str, port: int, payload: dict, timeout_ms: int) -> dict:
    broker = _ensure_standalone_server(host, port)
    request_id = f"standalone-{uuid.uuid4()}"
    claim = {
        "request_id": request_id,
        "url": payload["url"],
        "steps": payload["steps"],
        "keep_open": payload.get("keep_open", False),
        "background": payload.get("background", True),
    }
    event = broker.enqueue(claim)
    if not event.wait(timeout=timeout_ms / 1000):
        broker.pop_result(request_id)
        return {"request_id": request_id, "error": "timed out waiting for the browser extension to run this request"}
    return broker.pop_result(request_id)


def browser_run(url: str, steps: list, keep_open: bool = False, background: bool = True, timeout_ms: int = 90000) -> dict:
    """Runs steps (a @puppeteer/replay UserFlow's Step[] JSON, plus mywant's
    read/readAll/loop/forEachClick/if/setResult/sleep/reactChange customStep
    extensions) against url via the mywant browser extension.
    background=True (default) opens the tab without stealing focus.
    Automatically falls back to a standalone mode if the mywant server
    (MYWANT_URL, default http://localhost:8080) isn't reachable — see this
    module's docstring."""
    payload = {"url": url, "steps": steps, "keep_open": keep_open, "background": background, "timeout_ms": timeout_ms}

    try:
        data = _post_browser_run(MYWANT_API, payload, timeout_ms)
    except _Mode1Unreachable:
        parsed = urllib.parse.urlparse(MYWANT_API)
        host = parsed.hostname or "localhost"
        port = parsed.port or 80
        try:
            data = _run_standalone(host, port, payload, timeout_ms)
        except OSError:
            # Someone's already bound to that port after all — mode 1's
            # probe was a false negative (e.g. a momentarily slow real
            # server). Retry mode 1 once, letting any error propagate.
            data = _post_browser_run(MYWANT_API, payload, timeout_ms)

    if data.get("error"):
        raise RuntimeError(data["error"])
    return data.get("result", {})
