# mrs-browser

Shared browser automation helper for MyWant machine-readable-skill plugins
(`~/.mywant/custom-types/*`). Replaces `playwright.chromium.connect_over_cdp`
with a CDP-free `browser_run()` that drives the mywant browser extension —
the same already-logged-in Chrome/Firefox session the user is using, no
`--remote-debugging-port` required.

## Usage

```python
import sys, os
sys.path.insert(0, os.path.expanduser("~/work/machine-readable-skills-browser"))
from mrs_browser import browser_run, xpath_literal

result = browser_run("https://example.com", [
    {"type": "customStep", "name": "read",
     "parameters": {"selector": "h1", "as": "heading"}},
])
print(result)  # {"heading": "Example Domain"}
```

`steps` is a [`@puppeteer/replay`](https://github.com/puppeteer/replay)
`UserFlow.steps` JSON array (the same format Chrome DevTools' Recorder
panel exports), plus mywant's own `customStep` extensions
(`read`/`readAll`/`loop`/`forEachClick`/`if`/`setResult`/`sleep`/
`reactChange`) — see
`mywant/mcp/playwright-app/webext-src/browser-run-interpreter.ts` in the
[mywant](https://github.com/onelittlenightmusic/mywant) repo for the full
step vocabulary and how each one is executed inside the target page.

## Execution modes

`browser_run()` picks automatically, with no configuration needed:

1. **Via the mywant server** (`MYWANT_URL`, default `http://localhost:8080`)
   — `POST /api/v1/web-wants/browser-run`. The server queues the request
   and blocks until the browser extension picks it up on its own polling
   cycle, runs the steps, and returns the result.
2. **Standalone** (mywant server not reachable) — this library stands up a
   tiny local HTTP server on the exact same host:port, implementing just
   the two endpoints (`GET pending-action` / `POST browser-run-result`)
   the extension already knows how to poll. The extension can't tell the
   difference between the real mywant server and this stand-in, so no
   extension changes are needed either way. This means a plugin using
   `mrs_browser` works even when mywant isn't running at all.

Requires the mywant browser extension to be installed and enabled (see
`mywant/mcp/playwright-app/dist/{chrome,firefox}-extension` — built via
`make build-webext` in the mywant repo).

## Requirements

Python 3.10+, standard library only — no pip install needed.
