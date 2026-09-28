"""Turn a word-limits submission issue into data/guidelines/<id>.json.

Run by .github/workflows/guidelines-submission.yml. The issue is opened from
the journal detail form on the site and carries only what that form edits
(article type, word min/max, note, structure, figures & tables, guidelines URL); this merges it into the
existing file so fields the form does not show — descriptions, required
sections, anything scraped — survive. Same merge as the site's local save.

The issue body is written by anyone with a GitHub account, so everything is
checked before it reaches the repository, and the id becomes a filename only
after it has been matched against a strict pattern.

Environment:
  ISSUE_BODY   the issue text
  ISSUE_URL    recorded in the file, so a reviewer can trace where it came from
Writes `id` and `journal` to $GITHUB_OUTPUT. On a rejected submission, writes
the reason to $RUNNER_TEMP/guidelines-error.md for the workflow to post back,
and exits 1.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDELINES = ROOT / "data" / "guidelines"

ID_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")
JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n```", re.S)
MAX_TYPES = 100
MAX_WORDS = 1_000_000


class Rejected(Exception):
    pass


def text(value, field: str, limit: int, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise Rejected(f"`{field}` is missing.")
        return None
    if not isinstance(value, str):
        raise Rejected(f"`{field}` must be text.")
    value = " ".join(value.split())          # no newlines into outputs or titles
    if len(value) > limit:
        raise Rejected(f"`{field}` is longer than {limit} characters.")
    return value


def count(value, field: str) -> int | None:
    if value is None:
        return None
    # bool is an int in Python; true/false is not a word count.
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_WORDS:
        raise Rejected(f"`{field}` must be a whole number between 0 and {MAX_WORDS:,}.")
    return value


def parse(body: str) -> dict:
    m = JSON_BLOCK.search(body)
    if not m:
        raise Rejected("No ```json block found in the issue.")
    try:
        sub = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise Rejected(f"The JSON block does not parse: {e}.")
    if not isinstance(sub, dict):
        raise Rejected("The JSON block must be an object.")

    journal_id = sub.get("id")
    if not isinstance(journal_id, str) or not ID_RE.match(journal_id):
        raise Rejected("`id` must be the journal id shown on the site (letters, digits and dashes).")

    url = text(sub.get("url"), "url", 500)
    if url and not re.match(r"^https?://", url):
        raise Rejected("`url` must start with http:// or https://.")

    issns = sub.get("issns") or []
    if not isinstance(issns, list) or len(issns) > 4:
        raise Rejected("`issns` must be a short list.")

    types = sub.get("article_types")
    if not isinstance(types, list) or not 1 <= len(types) <= MAX_TYPES:
        raise Rejected(f"`article_types` must list between 1 and {MAX_TYPES} types.")
    rows, seen = [], set()
    for i, t in enumerate(types, 1):
        if not isinstance(t, dict):
            raise Rejected(f"Article type {i} must be an object.")
        name = text(t.get("type"), f"article_types[{i}].type", 200, required=True)
        if name in seen:
            raise Rejected(f"Article type “{name}” is listed twice.")
        seen.add(name)
        lo = count(t.get("min"), f"{name}: min")
        hi = count(t.get("max"), f"{name}: max")
        if lo is not None and hi is not None and lo > hi:
            raise Rejected(f"{name}: word min is above max.")
        row = {"type": name, "min": lo, "max": hi,
               "notes": text(t.get("notes"), f"{name}: notes", 1000)}
        # Absent (an issue opened before the form had these boxes) keeps what
        # the file holds; present but blank clears it, as for notes.
        for key in ("structure", "figures_tables"):
            if key in t:
                row[key] = text(t[key], f"{name}: {key}", 2000)
        rows.append(row)

    return {
        "id": journal_id,
        "journal": text(sub.get("journal"), "journal", 500, required=True),
        "publisher": text(sub.get("publisher"), "publisher", 500),
        "issns": [text(x, "issns", 20) for x in issns],
        "url": url,
        "rows": rows,
    }


def merge(sub: dict, existing: dict, issue_url: str | None) -> dict:
    by_type = {t.get("type"): t for t in existing.get("article_types") or []}
    types = []
    for r in sub["rows"]:
        prev = by_type.get(r["type"], {})
        limit = None
        if r["min"] is not None or r["max"] is not None:
            limit = {"unit": "words", "excludes": [], "notes": None,
                     **(prev.get("total_word_limit") or {}),
                     "min": r["min"], "max": r["max"]}
        extra = {k: r[k] for k in ("structure", "figures_tables") if k in r}
        types.append({**prev, "type": r["type"], "notes": r["notes"], **extra,
                      "total_word_limit": limit})
    issns = sub["issns"] + [None, None]
    return {
        **existing,
        "journal": sub["journal"],
        "publisher": sub["publisher"] or existing.get("publisher"),
        "issn": existing.get("issn") or {"print": issns[0], "electronic": issns[1]},
        "LLM": False,
        "validated": False,
        "url": sub["url"],
        "date_scraped": date.today().isoformat(),
        "article_types": types,
        "source": "user",
        "submitted_in": issue_url,
    }


def main() -> int:
    try:
        sub = parse(os.environ.get("ISSUE_BODY", ""))
    except Rejected as e:
        err = Path(os.environ.get("RUNNER_TEMP", ".")) / "guidelines-error.md"
        err.write_text(str(e))
        print(f"Rejected: {e}", file=sys.stderr)
        return 1

    path = GUIDELINES / f"{sub['id']}.json"
    existing = json.loads(path.read_text()) if path.exists() else {}
    record = merge(sub, existing, os.environ.get("ISSUE_URL"))
    GUIDELINES.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {path.relative_to(ROOT)}")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"id={sub['id']}\njournal={sub['journal']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
