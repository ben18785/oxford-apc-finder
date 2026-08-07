"""Stage 7 — Diff this build against the last one and record what changed.

Two things make this worth having beyond curiosity:

  * Accountability. When someone says "it told me this was free last month",
    there is a dated, public record of exactly what the site said and when it
    changed — rather than the maintainer's word against theirs.
  * Early warning. A deal silently vanishing from the JCT data, or 4,000
    journals changing status at once, is far more likely to be a source
    problem than a real event. Seeing it in the changelog is how you catch it.

The baseline is data/state/journal_state.tsv: one sorted line per journal
holding only the facts users act on. It is committed, so git carries the full
history for free and each week's diff is small and readable.

Outputs:
  data/state/journal_state.tsv   — new baseline (committed by CI)
  data/out/changes.json          — machine-readable diff, shipped to the site
  CHANGELOG-data.md              — human-readable, newest entry first

No LLM: this is a set difference over deterministic fields.
"""
from __future__ import annotations

import json
from pathlib import Path

import datetime

from common import (DATA, FIXTURES_MODE, OUT, ROOT, load_config, read_json,
                    utcnow, write_json)

STATE_DIR = DATA / "state"

# A fixtures build must never touch the real baseline: it would diff a dozen
# hand-picked journals against the ~43,000 in the committed state file, report
# the whole dataset as removed, and then overwrite the baseline with fixture
# data — destroying the history this file exists to keep.
STATE_FILE = STATE_DIR / ("journal_state.fixtures.tsv" if FIXTURES_MODE
                          else "journal_state.tsv")
CHANGELOG = ROOT / ("CHANGELOG-data.fixtures.md" if FIXTURES_MODE
                    else "CHANGELOG-data.md")

# Every journal the site has ever listed. fetch_metadata always re-looks-up this
# set, and merge treats membership as an inclusion route, so a journal cannot
# vanish just because a source had a bad day — while every fact about it is
# still freshly fetched. Accumulate scope, never accumulate facts.
KNOWN = STATE_DIR / ("known_journals.fixtures.tsv" if FIXTURES_MODE
                     else "known_journals.tsv")
KNOWN_COLUMNS = ["issn_l", "first_seen", "last_seen"]

# Only fields a user would act on. Scope text and topic lists churn constantly
# as OpenAlex reclassifies, and tracking them would bury the real changes.
COLUMNS = ["issn_l", "title", "status", "cost_kind", "price", "currency", "disputed"]


def state_row(j: dict) -> list[str]:
    cost = j.get("cost") or {}
    priced = cost.get("estimated") or cost.get("list") or {}
    return [
        j["id"],
        (j.get("title") or "").replace("\t", " "),
        j["deal"]["status"],
        cost.get("kind", ""),
        str(priced.get("price", "")),
        priced.get("currency", "") or "",
        "1" if j["deal"].get("disputed") else "",
    ]


def read_state(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    out = {}
    lines = path.read_text().splitlines()
    for line in lines[1:]:                      # skip header
        parts = line.split("\t")
        if len(parts) == len(COLUMNS):
            out[parts[0]] = dict(zip(COLUMNS, parts))
    return out


def write_state(path: Path, journals: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted((state_row(j) for j in journals), key=lambda r: r[0])
    body = "\n".join("\t".join(r) for r in rows)
    path.write_text("\t".join(COLUMNS) + "\n" + body + "\n")


def describe_cost(rec: dict) -> str:
    if rec["price"]:
        return f"{rec['price']} {rec['currency']}".strip()
    return rec["cost_kind"] or "unknown"


def update_known(journals: list[dict], today: str, retain_days: int) -> tuple[int, int]:
    """Remember every journal listed today; keep ones that have dropped out.

    Returns (total remembered, number dropped for age). Entries are retired
    after retain_days so genuinely dead journals do not accumulate for ever —
    bounded, and visible in the changelog when it happens.
    """
    previous: dict[str, dict] = {}
    if KNOWN.exists():
        for line in KNOWN.read_text().splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) == len(KNOWN_COLUMNS):
                previous[parts[0]] = dict(zip(KNOWN_COLUMNS, parts))

    cutoff = (datetime.date.fromisoformat(today)
              - datetime.timedelta(days=retain_days)).isoformat()
    current_ids = {j["id"] for j in journals}
    rows, retired = {}, 0
    for issn, rec in previous.items():
        # Only journals genuinely absent today can be retired. Ageing out one
        # that is in this very build would report a retirement that did not
        # happen, since it is re-added immediately below.
        if issn not in current_ids and rec["last_seen"] < cutoff:
            retired += 1
            continue
        rows[issn] = rec
    for j in journals:
        issn = j["id"]
        first = rows.get(issn, {}).get("first_seen", today)
        rows[issn] = {"issn_l": issn, "first_seen": first, "last_seen": today}

    KNOWN.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join("\t".join(rows[k][c] for c in KNOWN_COLUMNS)
                     for k in sorted(rows))
    KNOWN.write_text("\t".join(KNOWN_COLUMNS) + "\n" + body + "\n")
    return len(rows), retired


def main() -> None:
    data = read_json(OUT / "journals.json")
    journals = data["journals"]
    previous = read_state(STATE_FILE)

    if not previous:
        write_state(STATE_FILE, journals)
        update_known(journals, utcnow()[:10],
                     load_config()["inclusion"]["remember_journals_days"])
        write_json(OUT / "changes.json", {
            "generated": utcnow(), "baseline": True,
            "summary": {"added": len(journals), "removed": 0, "changed": 0},
            "added": [], "removed": [], "changed": [],
        })
        print(f"No previous state — stored baseline for {len(journals)} journals.")
        return

    current = {r[0]: dict(zip(COLUMNS, r))
               for r in (state_row(j) for j in journals)}

    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    changed = []
    for issn in sorted(set(current) & set(previous)):
        now, before = current[issn], previous[issn]
        diffs = {c: [before[c], now[c]] for c in
                 ("status", "cost_kind", "price", "currency", "disputed")
                 if before[c] != now[c]}
        if diffs:
            changed.append({"issn_l": issn, "title": now["title"], "changes": diffs})

    changes = {
        "generated": utcnow(),
        "baseline": False,
        "summary": {"added": len(added), "removed": len(removed),
                    "changed": len(changed)},
        # Cap the shipped lists: a source glitch can produce thousands, and the
        # site should stay small. The counts above are always exact.
        "added": [{"issn_l": i, "title": current[i]["title"],
                   "status": current[i]["status"]} for i in added[:500]],
        "removed": [{"issn_l": i, "title": previous[i]["title"],
                     "status": previous[i]["status"]} for i in removed[:500]],
        "changed": changed[:500],
    }
    write_json(OUT / "changes.json", changes)

    # --- human-readable log, newest first
    date = utcnow()[:10]
    lines = [f"## {date}", "",
             f"- **{len(added)}** journals added, **{len(removed)}** removed, "
             f"**{len(changed)}** changed."]

    status_moves = [c for c in changed if "status" in c["changes"]]
    if status_moves:
        lines += ["", f"### Deal status changes ({len(status_moves)})", ""]
        for c in status_moves[:100]:
            before, now = c["changes"]["status"]
            lines.append(f"- {c['title']} ({c['issn_l']}): `{before}` → `{now}`")
        if len(status_moves) > 100:
            lines.append(f"- …and {len(status_moves) - 100} more "
                         "(see `data/out/changes.json` in the run artifact).")

    price_moves = [c for c in changed
                   if "price" in c["changes"] and "status" not in c["changes"]]
    if price_moves:
        lines += ["", f"### Price changes ({len(price_moves)})", ""]
        for c in price_moves[:50]:
            b, n = c["changes"]["price"]
            cur = c["changes"].get("currency", ["", ""])[1] or ""
            lines.append(f"- {c['title']} ({c['issn_l']}): {b or '—'} → {n or '—'} {cur}".rstrip())
        if len(price_moves) > 50:
            lines.append(f"- …and {len(price_moves) - 50} more.")

    if removed:
        lines += ["", f"### Removed ({len(removed)})", ""]
        for i in removed[:50]:
            lines.append(f"- {previous[i]['title']} ({i})")
        if len(removed) > 50:
            lines.append(f"- …and {len(removed) - 50} more.")

    entry = "\n".join(lines) + "\n"
    header = ("# Data changelog\n\n"
              "What the site said, and when it changed. Generated automatically by\n"
              "`pipeline/changelog.py` on every refresh — newest entry first.\n\n")
    existing = CHANGELOG.read_text() if CHANGELOG.exists() else header
    body = existing[len(header):] if existing.startswith(header) else existing
    CHANGELOG.write_text(header + entry + "\n" + body)

    write_state(STATE_FILE, journals)
    remembered, retired = update_known(
        journals, date, load_config()["inclusion"]["remember_journals_days"])
    print(f"Changelog: +{len(added)} / -{len(removed)} / ~{len(changed)} "
          f"({len(status_moves)} deal-status changes); "
          f"{remembered:,} journals remembered"
          + (f", {retired} retired for age" if retired else ""))


if __name__ == "__main__":
    main()
