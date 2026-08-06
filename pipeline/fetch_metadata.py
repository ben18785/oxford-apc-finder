"""Stage 3 — Journal metadata for every ISSN in scope.

Scope = (deal journals from Stage 1) ∪ (all DOAJ journals) ∪ (journals whose
publisher is on the curated allowlist).

Sources:
  * OpenAlex sources API (CC0) — names, ISSNs, publisher, homepage, topics,
    APC list prices, is_in_doaj, works_count.
    Batched list queries: ?filter=issn:A|B|... (50 per request). With a free
    API key (env OPENALEX_API_KEY) a full refresh costs ~$0.02 of the $1/day
    free credit.
  * DOAJ journal API (metadata CC0) — APC amount+currency, waiver, license,
    keywords, and the journal's own aims-&-scope URL. Paged search; the bulk
    CSV (doaj.org/csv) is preferred when reachable but the API is the
    fallback because doaj.org/csv rejects some datacenter IPs.
  * DOAJ withdrawal changelog (CC BY-SA) — reasons for removals since 2024.

Output: data/out/metadata.json  {issn_l: record}
Fixture mode: reads data/fixtures/metadata.json.
"""
from __future__ import annotations

import csv
import io
import itertools
import os
import time

import requests
import yaml

from common import (CURATED, FIXTURES, FIXTURES_MODE, Manifest, OUT,
                    fetch_json, http_get, load_config, normalise_issn,
                    read_json, utcnow, write_json)

# OpenAlex bills a flat $0.0001 per request regardless of page size, and the
# free API key allows $1/day (~10,000 requests); anonymous callers get $0.10.
# So the pipeline is tuned to make few, large requests: 100 ISSNs per OR-filter
# and the maximum 200 records per page keeps a full refresh under ~800 calls.
OPENALEX_BATCH = 100
PER_PAGE = 200
PUBLISHER_BATCH = 25          # publisher IDs per host_organization_lineage filter

# Running total of OpenAlex spend, reported at the end of the run.
_openalex_cost = 0.0
_openalex_calls = 0


def openalex_params(cfg: dict, extra: dict) -> dict:
    params = dict(extra)
    key = os.environ.get("OPENALEX_API_KEY")
    if key:
        params["api_key"] = key
    return params


def openalex_get(cfg, manifest, session, url, key, params) -> dict:
    """fetch_json plus OpenAlex spend accounting."""
    global _openalex_cost, _openalex_calls
    data = fetch_json(url, manifest, key, session, openalex_params(cfg, params))
    _openalex_calls += 1
    _openalex_cost += (data.get("meta") or {}).get("cost_usd") or 0.0
    return data


def compact_openalex(src: dict) -> dict:
    """Keep only the fields the site needs, with provenance-friendly shape."""
    return {
        "openalex_id": src.get("id"),
        "issn_l": src.get("issn_l"),
        "issns": src.get("issn") or [],
        "title": src.get("display_name"),
        "alternate_titles": src.get("alternate_titles") or [],
        "publisher": src.get("host_organization_name"),
        "homepage": src.get("homepage_url"),
        "is_in_doaj": bool(src.get("is_in_doaj")),
        "is_oa": bool(src.get("is_oa")),
        "type": src.get("type"),
        "works_count": src.get("works_count"),
        "apc_usd": src.get("apc_usd"),
        "apc_prices": src.get("apc_prices") or [],
        "topics": [
            {"name": t.get("display_name"),
             "subfield": (t.get("subfield") or {}).get("display_name"),
             "field": (t.get("field") or {}).get("display_name"),
             "count": t.get("count")}
            for t in (src.get("topics") or [])[:8]
        ],
    }


def fetch_openalex_by_issns(cfg, manifest, session, issns: list[str]) -> dict:
    """Batched ISSN lookups → {issn_l: compact record}"""
    out: dict[str, dict] = {}
    base = cfg["sources"]["openalex_api"] + "/sources"
    issns = sorted(set(issns))
    for i in range(0, len(issns), OPENALEX_BATCH):
        batch = issns[i:i + OPENALEX_BATCH]
        data = openalex_get(cfg, manifest, session, base,
                            f"openalex_issn_batch_{i}",
                            {"filter": "issn:" + "|".join(batch),
                             "per-page": PER_PAGE})
        for src in data.get("results", []):
            rec = compact_openalex(src)
            if rec["issn_l"]:
                out[rec["issn_l"]] = rec
        time.sleep(0.15)
        if i % (OPENALEX_BATCH * 20) == 0:
            print(f"  openalex issn batches: {i}/{len(issns)} ({len(out)} sources)")
    return out


def resolve_publisher_ids(cfg, manifest, session, names: list[str]) -> dict[str, str]:
    """Allowlist publisher names → OpenAlex publisher IDs.

    Resolving to IDs first means the journal sweep can filter on
    host_organization_lineage, which returns exactly the allowlisted
    publishers' journals (imprints and subsidiaries included, via the lineage)
    instead of paging through every source that merely mentions the name.
    """
    base = cfg["sources"]["openalex_api"] + "/publishers"
    ids: dict[str, str] = {}
    for name in names:
        data = openalex_get(cfg, manifest, session, base,
                            f"openalex_publisher_{name[:24]}",
                            {"search": name, "per-page": 25})
        for p in data.get("results", []):
            display = p.get("display_name") or ""
            # Containment, not fuzzy relevance: a search for "IEEE" must not
            # drag in whatever OpenAlex ranks third for the term.
            if name.lower() in display.lower():
                ids[(p.get("id") or "").rsplit("/", 1)[-1]] = display
        time.sleep(0.15)
    ids.pop("", None)
    return ids


def fetch_openalex_by_publishers(cfg, manifest, session, patterns: list[str]) -> dict:
    """Every journal published by an allowlisted publisher."""
    out: dict[str, dict] = {}
    base = cfg["sources"]["openalex_api"] + "/sources"

    pub_ids = resolve_publisher_ids(cfg, manifest, session, patterns)
    print(f"  resolved {len(patterns)} allowlist names to "
          f"{len(pub_ids)} OpenAlex publishers")

    ordered = sorted(pub_ids)
    for i in range(0, len(ordered), PUBLISHER_BATCH):
        batch = ordered[i:i + PUBLISHER_BATCH]
        cursor = "*"
        page = 0
        while cursor:
            data = openalex_get(
                cfg, manifest, session, base,
                f"openalex_pub_batch_{i}_p{page}",
                {"filter": ("host_organization_lineage:" + "|".join(batch)
                            + ",type:journal"),
                 "per-page": PER_PAGE,
                 "cursor": cursor})
            for src in data.get("results", []):
                rec = compact_openalex(src)
                if rec["issn_l"]:
                    out.setdefault(rec["issn_l"], rec)
            cursor = (data.get("meta") or {}).get("next_cursor")
            page += 1
            time.sleep(0.15)
        print(f"  publisher batch {i // PUBLISHER_BATCH + 1}: {len(out)} journals so far")
    return out


def fetch_doaj(cfg, manifest, session) -> dict:
    """All DOAJ journals via paged API → {issn: doaj record} (keyed on both
    print and electronic ISSN)."""
    out: dict[str, dict] = {}
    base = cfg["sources"]["doaj_api"] + "/search/journals/%2A"
    page = 1
    while True:
        params = {"page": page, "pageSize": 100}
        data = fetch_json(base, manifest, f"doaj_page_{page}", session, params)
        results = data.get("results", [])
        if not results:
            break
        for r in results:
            bib = r.get("bibjson", {})
            apc = bib.get("apc") or {}
            apc_max = (apc.get("max") or [{}])[0] if apc.get("has_apc") else {}
            rec = {
                "doaj_id": r.get("id"),
                "title": bib.get("title"),
                "publisher": (bib.get("publisher") or {}).get("name"),
                "apc": {"has_apc": bool(apc.get("has_apc")),
                        "price": apc_max.get("price"),
                        "currency": apc_max.get("currency")},
                "waiver": bool((bib.get("waiver") or {}).get("has_waiver")),
                "keywords": bib.get("keywords") or [],
                "subjects": [s.get("term") for s in (bib.get("subject") or [])],
                "license": [l.get("type") for l in (bib.get("license") or [])],
                "aims_scope_url": (bib.get("ref") or {}).get("aims_scope"),
                "journal_url": (bib.get("ref") or {}).get("journal"),
            }
            for k in ("pissn", "eissn"):
                issn = normalise_issn(bib.get(k))
                if issn:
                    out[issn] = rec
        total = data.get("total", 0)
        print(f"  doaj page {page} ({len(out)} issns; total journals {total})")
        if page * 100 >= total:
            break
        page += 1
        time.sleep(0.2)
    return out


def fetch_doaj_withdrawn(cfg, manifest, session) -> dict:
    """DOAJ withdrawal changelog → {issn: {date, reason}}"""
    url = cfg["sources"]["doaj_withdrawn_sheet_csv"]
    resp = http_get(url, session=session)
    resp.raise_for_status()
    manifest.record("doaj_withdrawn", url, resp.content)
    out = {}
    reader = csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig")))
    for row in reader:
        issn = normalise_issn(row.get("ISSN"))
        if issn:
            out[issn] = {"date": (row.get("Date Removed") or "").strip(),
                         "reason": (row.get("Reason") or "").strip()}
    return out


def main() -> None:
    cfg = load_config()
    out_path = OUT / "metadata.json"

    if FIXTURES_MODE:
        meta = read_json(FIXTURES / "metadata.json")
        print(f"[fixtures] loaded metadata for {len(meta['openalex'])} journals")
        write_json(out_path, meta)
        return

    manifest = Manifest()
    session = requests.Session()

    deals = read_json(OUT / "deals.json")
    deal_issns = [i for a in deals["agreements"] for j in a["journals"] for i in j["issns"]]
    print(f"{len(set(deal_issns))} distinct deal ISSNs")

    allow = yaml.safe_load((CURATED / "publisher_allowlist.yaml").read_text())["publishers"]

    print("Fetching DOAJ journals …")
    doaj = fetch_doaj(cfg, manifest, session)
    print("Fetching DOAJ withdrawal changelog …")
    withdrawn = fetch_doaj_withdrawn(cfg, manifest, session)

    print("Fetching OpenAlex records for deal + DOAJ ISSNs …")
    openalex = fetch_openalex_by_issns(cfg, manifest, session,
                                       deal_issns + list(doaj.keys()))
    print("Fetching OpenAlex records for allowlisted publishers …")
    pub_recs = fetch_openalex_by_publishers(cfg, manifest, session, allow)
    for k, v in pub_recs.items():
        openalex.setdefault(k, v)

    meta = {
        "generated": utcnow(),
        "sources": {
            "openalex": {"url": cfg["sources"]["openalex_api"], "license": "CC0"},
            "doaj": {"url": cfg["sources"]["doaj_api"], "license": "CC0 (metadata)"},
            "doaj_withdrawn": {"url": cfg["sources"]["doaj_withdrawn_sheet_csv"],
                                "license": "CC BY-SA 4.0"},
        },
        "openalex": openalex,
        "doaj": doaj,
        "doaj_withdrawn": withdrawn,
    }
    write_json(out_path, meta)
    print(f"Done: {len(openalex)} OpenAlex records, {len(doaj)} DOAJ ISSNs, "
          f"{len(withdrawn)} withdrawal entries")

    # The free daily allowance is $1.00 with an API key, $0.10 anonymously.
    budget = 1.00 if os.environ.get("OPENALEX_API_KEY") else 0.10
    print(f"OpenAlex spend: ${_openalex_cost:.4f} over {_openalex_calls} calls "
          f"({_openalex_cost / budget:.0%} of the ${budget:.2f} daily free allowance)")
    if _openalex_cost > budget * 0.8:
        print("  WARNING: within 20% of the daily OpenAlex allowance. Set the "
              "OPENALEX_API_KEY secret, or reduce the publisher allowlist.")


if __name__ == "__main__":
    main()
