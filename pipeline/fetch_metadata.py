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
import os
import re
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


# "40 USD; 450000 IDR" — DOAJ lists one amount per accepted currency.
APC_AMOUNT_RX = re.compile(r"(\d+)\s*([A-Z]{3})")
# Which one to show an Oxford author, in order of usefulness to them.
CURRENCY_PREFERENCE = ("GBP", "EUR", "USD")


def parse_apc_amount(raw: str | None) -> dict | None:
    """'40 USD; 450000 IDR' -> {'price': 40, 'currency': 'USD'}"""
    options = [{"price": int(m.group(1)), "currency": m.group(2)}
               for m in APC_AMOUNT_RX.finditer(raw or "")]
    if not options:
        return None
    for currency in CURRENCY_PREFERENCE:
        for option in options:
            if option["currency"] == currency:
                return option
    return options[0]


def fetch_doaj(cfg, manifest, session) -> tuple[dict, list[str]]:
    """All DOAJ journals, from the bulk CSV export.

    The paged search API cannot be used for this: DOAJ caps pagination at 1,000
    records (page 11 at pageSize=100 returns HTTP 400), so it can only ever
    reach a fraction of the ~23,000 journals. The bulk CSV returns all of them
    in a single request.

    Returns ({issn: record} keyed on BOTH print and electronic ISSN, so merge
    can look a journal up by either) and a list of ONE preferred ISSN per
    journal. OpenAlex resolves either ISSN of a journal to the same source, so
    querying both would double the request count — and OpenAlex bills per
    request — for no extra coverage.
    """
    out: dict[str, dict] = {}
    primary: list[str] = []
    url = cfg["sources"]["doaj_journal_csv"]

    resp = http_get(url, session=session, timeout=180)
    resp.raise_for_status()
    manifest.record("doaj_csv", url, resp.content)
    reader = csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig")))

    for row in reader:
        has_apc = row.get("APC", "").strip().lower() == "yes"
        amount = parse_apc_amount(row.get("APC amount")) if has_apc else None
        rec = {
            # The DOAJ ID, taken from the canonical DOAJ URL for the journal.
            "doaj_id": (row.get("URL in DOAJ") or "").rstrip("/").rsplit("/", 1)[-1],
            "doaj_url": (row.get("URL in DOAJ") or "").strip() or None,
            "title": (row.get("Journal title") or "").strip(),
            "publisher": (row.get("Publisher") or "").strip(),
            "apc": {"has_apc": has_apc,
                    "price": (amount or {}).get("price"),
                    "currency": (amount or {}).get("currency")},
            "apc_url": (row.get("APC information URL") or "").strip() or None,
            "waiver": row.get(
                "Journal waiver policy (for developing country authors etc)",
                "").strip().lower() == "yes",
            "keywords": [k.strip() for k in (row.get("Keywords") or "").split(",")
                         if k.strip()],
            # "Language and Literature: English language | Law: Law in general"
            "subjects": [s.strip() for s in (row.get("Subjects") or "").split("|")
                         if s.strip()],
            "license": [l.strip() for l in (row.get("Journal license") or "").split(",")
                        if l.strip()],
            "aims_scope_url": (row.get("URL for journal's aims & scope") or "").strip() or None,
            "journal_url": (row.get("Journal URL") or "").strip() or None,
        }
        issns = [normalise_issn(row.get(k)) for k in
                 ("Journal EISSN (online version)", "Journal ISSN (print version)")]
        issns = [i for i in issns if i]
        for issn in issns:
            out[issn] = rec
        if issns:
            primary.append(issns[0])   # electronic ISSN when there is one

    print(f"  doaj: {len(primary)} journals, {len(out)} issns (bulk CSV)")
    return out, primary


def _find_column(header: list[str], *needles: str) -> int | None:
    """Index of the first column whose header contains any needle.

    The withdrawal sheet's header row is not made of tidy field names — the
    cells hold whole explanatory paragraphs, and the tidy ones carry trailing
    spaces ('ISSN '). Matching on substrings survives that, and survives DOAJ
    rewording the blurb.
    """
    cells = [(c or "").strip().lower() for c in header]

    # Exact match first. The blurb in column 0 contains the words "reason" and
    # "issn", so a substring search alone picks the journal-title column.
    for needle in needles:
        for i, cell in enumerate(cells):
            if cell == needle.lower():
                return i

    # Otherwise fall back to substring, preferring the tersest header — the
    # date column's header really is a whole licence paragraph.
    best = None
    for needle in needles:
        for i, cell in enumerate(cells):
            if needle.lower() in cell and (best is None or len(cell) < len(cells[best])):
                best = i
    return best


def fetch_doaj_withdrawn(cfg, manifest, session) -> dict:
    """DOAJ withdrawal changelog → {issn: {date, reason}}

    This feeds the misconduct exclusion in merge.py, so a silent parse failure
    would quietly disable the site's main quality filter. It therefore raises
    rather than returning an empty dict.
    """
    url = cfg["sources"]["doaj_withdrawn_sheet_csv"]
    resp = http_get(url, session=session)
    resp.raise_for_status()
    manifest.record("doaj_withdrawn", url, resp.content)

    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    if not rows:
        raise RuntimeError(f"DOAJ withdrawal changelog is empty: {url}")

    header = rows[0]
    issn_col = _find_column(header, "issn")
    reason_col = _find_column(header, "reason")
    date_col = _find_column(header, "date removed")
    if issn_col is None or reason_col is None:
        raise RuntimeError(
            f"Cannot find ISSN/Reason columns in the DOAJ withdrawal changelog "
            f"({url}). Header was: {header[:6]}")

    out = {}
    for row in rows[1:]:
        if issn_col >= len(row):
            continue
        issn = normalise_issn(row[issn_col])
        if not issn:
            continue
        out[issn] = {
            "date": row[date_col].strip() if date_col is not None and date_col < len(row) else "",
            "reason": row[reason_col].strip() if reason_col < len(row) else "",
        }
    if not out:
        raise RuntimeError(
            f"DOAJ withdrawal changelog parsed to zero entries ({url}) — the "
            "misconduct exclusion would silently do nothing.")
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
    doaj, doaj_primary = fetch_doaj(cfg, manifest, session)
    print("Fetching DOAJ withdrawal changelog …")
    withdrawn = fetch_doaj_withdrawn(cfg, manifest, session)

    print("Fetching OpenAlex records for deal + DOAJ ISSNs …")
    openalex = fetch_openalex_by_issns(cfg, manifest, session,
                                       deal_issns + doaj_primary)
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
