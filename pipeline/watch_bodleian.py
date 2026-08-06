"""Stage 2 — Watch the Bodleian publisher-deals page for changes.

The hand-curated overlay (data/curated/oxford_overrides.yaml) encodes the
Oxford-specific facts that JCT doesn't carry (discount schemes, diamond
deals, funder restrictions, caps). This script keeps that file honest: it
fetches the Bodleian page, normalises the text, and compares it with the
snapshot stored in the repo. On a change it exits 3 and writes a diff, which
the CI workflow turns into a GitHub issue labelled `needs-review`.

No LLM anywhere: this is a plain text diff for a human to read.
"""
from __future__ import annotations

import difflib
import html.parser
import re
import sys
from pathlib import Path

from common import CACHE, FIXTURES_MODE, Manifest, http_get, load_config

SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "curated" / "bodleian_snapshot.txt"


class _TextExtractor(html.parser.HTMLParser):
    """Extract visible text from the page's main content only."""

    SKIP = {"script", "style", "nav", "header", "footer"}

    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.chunks.append(data.strip())


def normalise(html_text: str) -> str:
    p = _TextExtractor()
    p.feed(html_text)
    text = "\n".join(p.chunks)
    text = re.sub(r"[ \t]+", " ", text)
    return text


def main() -> None:
    if FIXTURES_MODE:
        print("[fixtures] skipping Bodleian watch (network stage)")
        return
    cfg = load_config()
    url = cfg["sources"]["bodleian_deals"]
    manifest = Manifest()
    resp = http_get(url)
    resp.raise_for_status()
    manifest.record("bodleian_deals_page", url, resp.content)
    current = normalise(resp.text)

    if not SNAPSHOT.exists():
        SNAPSHOT.write_text(current)
        print("No snapshot existed — stored initial snapshot. Review "
              "data/curated/oxford_overrides.yaml against the page manually once.")
        return

    previous = SNAPSHOT.read_text()
    if previous == current:
        print("Bodleian deals page unchanged.")
        return

    diff = "\n".join(difflib.unified_diff(
        previous.splitlines(), current.splitlines(),
        fromfile="snapshot", tofile="live", lineterm=""))
    diff_path = CACHE / "bodleian_diff.txt"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(diff)
    SNAPSHOT.write_text(current)  # commit in CI keeps history reviewable
    print("Bodleian deals page CHANGED — diff written to data/cache/bodleian_diff.txt")
    print("Review whether data/curated/oxford_overrides.yaml needs updating.")
    sys.exit(3)  # CI interprets exit 3 as "open a needs-review issue"


if __name__ == "__main__":
    main()
