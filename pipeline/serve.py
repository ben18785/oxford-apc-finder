"""Local preview server for _site/, with one write endpoint.

Serves the built site like `python -m http.server`, and additionally accepts

    POST /api/guidelines/<journal id>   (JSON body)

which saves the body to data/guidelines/<journal id>.json — the reader-supplied
word and figure limits from the journal detail form. The next build_site.py
run picks the file up. Local use only: the deployed static site has no server,
so the form's save fails there by design.
"""
from __future__ import annotations

import json
import re
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from common import DATA, ROOT

SITE = ROOT / "_site"
GUIDELINES = DATA / "guidelines"
# The id becomes a filename, so nothing that could step outside the directory.
ROUTE = re.compile(r"^/api/guidelines/([A-Za-z0-9-]+)$")


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Always revalidate, so a rebuilt app.js / style.css shows on reload.
        # Safari otherwise keeps serving stale copies from its cache.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_POST(self):
        m = ROUTE.match(self.path)
        if not m:
            return self._reply(404, {"error": "unknown endpoint"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            record = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            return self._reply(400, {"error": "body is not valid JSON"})
        if not isinstance(record, dict) or not isinstance(record.get("article_types"), list):
            return self._reply(400, {"error": "expected an object with article_types"})

        path = GUIDELINES / f"{m.group(1)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        print(f"  saved {path.relative_to(ROOT)}")
        self._reply(200, {"saved": str(path.relative_to(ROOT))})

    def _reply(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(SITE)))
    print(f"Serving {SITE.relative_to(ROOT)}/ on http://localhost:{port}/")
    server.serve_forever()
