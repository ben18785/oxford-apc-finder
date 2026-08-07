"""Regenerate data/fixtures/ from a real pipeline run.

Run this after `python pipeline/run_all.py` (live) when the shape of any source
changes. It takes a small slice of the real data chosen to exercise every
branch in merge.py, so the offline test suite stays representative instead of
drifting away from what the live path actually produces.

    python tests/make_fixtures.py

Deterministic: same inputs always select the same journals.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from common import FIXTURES, OUT, read_json, write_json  # noqa: E402

# Branches the fixture set must cover, so a regression in any of them fails a
# test rather than reaching the site.
WANTED = {
    "covered": lambda j: j["deal"]["status"] == "covered",
    "covered_with_caveats": lambda j: (j["deal"]["status"] == "covered"
                                       and len(j["deal"]["caveats"]) > 5),
    "discount_with_price": lambda j: j["cost"]["kind"] == "discount",
    "discount_no_price": lambda j: j["cost"]["kind"] == "discount_unknown_base",
    "diamond": lambda j: j["deal"]["status"] == "diamond",
    "disputed": lambda j: bool(j["deal"].get("disputed")),
    "no_apc": lambda j: j["cost"]["kind"] == "no_apc",
    "list_price": lambda j: j["cost"]["kind"] == "list_price",
    "unknown_cost": lambda j: j["cost"]["kind"] == "unknown",
    "in_doaj": lambda j: j["in_doaj"] and j["deal"]["status"] == "none",
    "waiver": lambda j: j.get("waiver"),
}


def main() -> None:
    journals = read_json(OUT / "journals.json")["journals"]
    deals = read_json(OUT / "deals.json")
    meta = read_json(OUT / "metadata.json")

    picked: dict[str, dict] = {}
    for label, test in WANTED.items():
        # Sorted by id, first match: stable across runs.
        match = next((j for j in sorted(journals, key=lambda x: x["id"])
                      if test(j) and j["id"] not in picked), None)
        if match is None:
            print(f"  WARNING: no journal matches branch {label!r}")
            continue
        picked[match["id"]] = match
        print(f"  {label:22} -> {match['id']}  {match['title'][:44]}")

    ids = set(picked)
    all_issns = {i for j in picked.values() for i in j["issns"]}

    # --- metadata slice
    fx_meta = {
        "generated": meta["generated"],
        "sources": meta["sources"],
        "openalex": {k: v for k, v in meta["openalex"].items() if k in ids},
        "doaj": {k: v for k, v in meta["doaj"].items() if k in all_issns},
        # Keep a real misconduct withdrawal so the exclusion is exercised.
        "doaj_withdrawn": dict(sorted(
            ((k, v) for k, v in meta["doaj_withdrawn"].items()
             if "best practice" in v.get("reason", "").lower()),
            key=lambda kv: kv[0])[:3]),
    }

    # A journal that must be EXCLUDED for misconduct: give it an OpenAlex
    # record whose ISSN is in the withdrawal list.
    if fx_meta["doaj_withdrawn"]:
        bad_issn = sorted(fx_meta["doaj_withdrawn"])[0]
        fx_meta["openalex"][bad_issn] = {
            "openalex_id": "https://openalex.org/S000000000",
            "issn_l": bad_issn, "issns": [bad_issn],
            "title": "Fixture Journal Withdrawn For Misconduct",
            "alternate_titles": [], "publisher": "Elsevier",
            "homepage": "https://example.invalid/withdrawn",
            "is_in_doaj": False, "is_oa": True, "type": "journal",
            "works_count": 10, "apc_usd": 1000,
            "apc_prices": [{"price": 1000, "currency": "USD"}], "topics": [],
        }
        print(f"  {'misconduct_excluded':22} -> {bad_issn}  (synthetic)")

    # --- deals slice: keep only agreements that cover a picked journal
    fx_agreements = []
    for a in deals["agreements"]:
        keep = [j for j in a["journals"] if set(j["issns"]) & all_issns]
        if keep:
            fx_agreements.append({**a, "journals": keep, "journal_count": len(keep)})

    write_json(FIXTURES / "deals.json", {
        "generated": deals["generated"],
        "sample_data": True,
        "institution_ror": deals["institution_ror"],
        "source": deals["source"],
        "agreements": fx_agreements,
    })
    write_json(FIXTURES / "metadata.json", fx_meta)
    print(f"\n{len(picked)} journals, {len(fx_agreements)} agreements, "
          f"{len(fx_meta['doaj_withdrawn'])} withdrawals written to data/fixtures/")


if __name__ == "__main__":
    main()
