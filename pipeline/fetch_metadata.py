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

OPENALEX_BATCH = 50
PER_PAGE = 200


def openalex_params(cfg: dict, extra: dict) -> dict:
    params = dict(extra)
    key = os.environ.get("OPENALEX_API_KEY")
    if key:
        params["api_key"] = key
    return params


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
        params = openalex_params(cfg, {
            "filter": "issn:" + "|".join(batch),
            "per-page": PER_PAGE,
        })
        data = fetch_json(base, manifest, f"openalex_issn_batch_{i}", session, params)
        for src in data.get("results", []):
            rec = compact_openalex(src)
            if rec["issn_l"]:
                out[rec["issn_l"]] = rec
        time.sleep(0.15)
        if i % 1000 == 0:
            print(f"  openalex issn batches: {i}/{len(issns)}")
    return out


def fetch_openalex_by_publishers(cfg, manifest, session, patterns: list[str]) -> dict:
    """All journal sources whose host organization matches the allowlist.
    Uses OpenAlex search on host_organization name, then regex-filters."""
    import re
    out: dict[str, dict] = {}
    base = cfg["sources"]["openalex_api"] + "/sources"
    rx = re.compile("|".join(f"(?:{p})" for p in patterns), re.I)
    # Page through all journal-type sources with works in the last 5 years is
    # too broad; instead search per-publisher name.
    for pat in patterns:
        cursor = "*"
        while cursor:
            params = openalex_params(cfg, {
                "search": pat,
                "filter": "type:journal",
                "per-page": PER_PAGE,
                "cursor": cursor,
            })
            data = fetch_json(base, manifest, f"openalex_pub_{pat[:20]}_{cursor[:8]}",
                              session, params)
            for src in data.get("results", []):
                name = src.get("host_organization_name") or ""
                if rx.search(name):
                    rec = compact_openalex(src)
                    if rec["issn_l"]:
                        out.setdefault(rec["issn_l"], rec)
            cursor = (data.get("meta") or {}).get("next_cursor")
            time.sleep(0.15)
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


if __name__ == "__main__":
    main()
