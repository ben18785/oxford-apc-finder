"""Stage 5 — Validate the merged dataset before it can ship.

Checks:
  1. Schema — required fields, ISSN shapes, known enum values.
  2. Sanity thresholds — the deal-journal count may not fall more than
     config.validation.max_weekly_drop_pct vs the previously shipped dataset.
  3. Oracle — a random sample of journals is cross-checked against the live
     JCT API (api.journalcheckertool.org/ta?issn=&ror=): our "covered" verdict
     must match theirs. Any mismatch fails the build. (Skipped in fixture
     mode, where there is no network.)

Exit code != 0 blocks deploy; the site keeps serving last week's data.
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timezone

from common import (FIXTURES_MODE, OUT, ROOT, http_get, load_config,
                    read_json)

ISSN_RX = r"^\d{4}-\d{3}[\dX]$"


def check_schema(data: dict) -> list[str]:
    import re
    errors = []
    seen_ids = set()
    for j in data["journals"]:
        jid = j.get("id", "?")
        if jid in seen_ids:
            errors.append(f"duplicate id {jid}")
        seen_ids.add(jid)
        if not j.get("title"):
            errors.append(f"{jid}: missing title")
        for issn in j.get("issns", []):
            if not re.match(ISSN_RX, issn):
                errors.append(f"{jid}: malformed ISSN {issn}")
        if j["deal"]["status"] not in ("covered", "discount", "diamond", "none"):
            errors.append(f"{jid}: bad deal status {j['deal']['status']}")
        if j["cost"]["kind"] not in ("covered", "diamond", "discount",
                                     "discount_unknown_base", "list_price",
                                     "no_apc", "unknown"):
            errors.append(f"{jid}: bad cost kind {j['cost']['kind']}")
        if j["deal"]["status"] == "covered" and not j["provenance"].get("deal"):
            errors.append(f"{jid}: covered but no deal provenance")
    return errors[:50]


LAST_COUNTS = OUT / "last_counts.json"


def check_thresholds(data: dict, cfg: dict) -> list[str]:
    """Compare against the counts from the last successfully-validated build
    (committed as data/out/last_counts.json), so a sudden drop in coverage
    blocks the deploy for human review rather than silently shipping."""
    if FIXTURES_MODE:
        # The committed baseline describes the real dataset (~43,000 journals).
        # A fixtures build is a dozen hand-picked journals, so the comparison is
        # meaningless and would fail every offline run.
        print("  [fixtures] threshold check skipped (baseline is real data)")
        return []
    if not LAST_COUNTS.exists():
        return []
    prev = read_json(LAST_COUNTS)
    errors = []
    for key in ("covered", "total"):
        old, new = prev.get(key, 0), data["counts"].get(key, 0)
        if old and new < old * (1 - cfg["validation"]["max_weekly_drop_pct"] / 100):
            errors.append(f"count '{key}' dropped {old} → {new} "
                          f"(> {cfg['validation']['max_weekly_drop_pct']}% fall); "
                          "refusing to ship without human review")
    return errors


def check_oracle(data: dict, cfg: dict) -> list[str]:
    if FIXTURES_MODE:
        print("  [fixtures] oracle check skipped (no network)")
        return []
    if os.environ.get("APC_SKIP_ORACLE") == "1":
        print("  oracle check skipped (APC_SKIP_ORACLE=1)")
        return []
    ror = cfg["institution_ror"]
    api = cfg["sources"]["jct_api"]

    # Seeded on the date so a failing build can be re-run against the same
    # sample once the data is fixed, while still rotating week to week.
    rng = random.Random(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    n = cfg["validation"]["oracle_sample_size"]
    covered = [j for j in data["journals"] if j["deal"]["status"] == "covered"]
    uncovered = [j for j in data["journals"] if j["deal"]["status"] != "covered"]
    # Stratified: an unstratified sample is almost all uncovered journals, so
    # it would barely exercise the coverage logic that matters most.
    sample = (rng.sample(covered, min(n // 2, len(covered)))
              + rng.sample(uncovered, min(n - n // 2, len(uncovered))))

    def jct_covers(issn: str) -> bool:
        resp = http_get(f"{api}/ta", params={"issn": issn, "ror": ror}, retries=2)
        # "No agreement" is returned as HTTP 200 with a bare `404` integer as
        # the body, not as a list and not as an HTTP error — so check the shape
        # rather than trusting the status code.
        payload = resp.json() if resp.status_code == 200 else None
        results = payload if isinstance(payload, list) else []
        return any((r.get("result") or {}).get("compliant") == "yes"
                   for r in results if isinstance(r, dict))

    errors = []
    for j in sample:
        ours = j["deal"]["status"] == "covered"
        # JCT indexes an agreement's journals under the specific ISSN the
        # agreement lists — often the online one. Querying only the first ISSN
        # reports a false mismatch for any journal listed under another. Ask
        # about the rest only when the first answer disagrees with ours, so the
        # normal case still costs one request per sampled journal.
        theirs = jct_covers(j["issns"][0])
        if theirs != ours:
            for issn in j["issns"][1:]:
                if jct_covers(issn):
                    theirs = True
                    break

        if ours != theirs:
            errors.append(f"{j['id']} {j['title']}: we say covered={ours}, "
                          f"JCT API says {theirs} (checked {', '.join(j['issns'])})")
    print(f"  oracle: {len(sample)} sampled, {len(errors)} mismatches")
    return errors


def main() -> None:
    cfg = load_config()
    data = read_json(OUT / "journals.json")
    failures = (check_schema(data)
                + check_thresholds(data, cfg)
                + check_oracle(data, cfg))
    if failures:
        print("VALIDATION FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    # Record this run's counts as the baseline for next week's threshold check.
    from common import write_json
    write_json(LAST_COUNTS, data["counts"])
    print(f"Validation passed: {data['counts']['total']} journals, "
          f"{data['counts']['covered']} covered.")


if __name__ == "__main__":
    main()
