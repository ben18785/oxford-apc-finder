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
import datetime
import io
import os
import re
import time

import requests
import yaml

from common import (CURATED, DATA, DailyQuotaExhausted, FIXTURES, FIXTURES_MODE,
                    Manifest, OUT, fetch_json, http_get, known_journal_issns,
                    load_config, normalise_issn, read_json, utcnow, write_json)

# OpenAlex bills a flat $0.0001 per request regardless of page size, and the
# free API key allows $1/day (~10,000 requests); anonymous callers get $0.10.
# So the pipeline is tuned to make few, large requests: 100 ISSNs per OR-filter
# and the maximum 200 records per page keeps a full refresh under ~800 calls.
OPENALEX_BATCH = 100
PER_PAGE = 200
PUBLISHER_BATCH = 25          # publisher IDs per host_organization_lineage filter
# OpenAlex's topic taxonomy has 252 subfields. Used only to forecast the sweep's
# cost before it runs; the real number comes from the API.
SUBFIELD_COUNT = 252

# Running total of OpenAlex spend, reported at the end of the run.
_openalex_cost = 0.0
_openalex_calls = 0


def openalex_params(cfg: dict, extra: dict) -> dict:
    params = dict(extra)
    key = os.environ.get("OPENALEX_API_KEY")
    if key:
        params["api_key"] = key
    return params


def check_openalex_budget(cfg, session, needed: int) -> None:
    """Refuse to start a refresh the daily allowance cannot finish.

    A partly-fetched run is worse than no run: it burns the rest of the budget,
    fails at an arbitrary point, and leaves the failure looking like a bug in
    whichever stage happened to be running. One request tells us instead.
    """
    resp = http_get(cfg["sources"]["openalex_api"] + "/sources",
                    session=session, retries=2,
                    params=openalex_params(cfg, {"filter": "issn:0028-0836",
                                                 "per-page": 1}))
    remaining = resp.headers.get("x-ratelimit-remaining")
    reset = resp.headers.get("x-ratelimit-reset")
    if remaining is None or not remaining.strip().isdigit():
        return                                   # no header: proceed and hope
    remaining = int(remaining)
    hours = (int(reset) / 3600) if (reset or "").isdigit() else None
    print(f"  OpenAlex allowance: {remaining:,} requests left"
          + (f", resets in {hours:.1f}h" if hours else ""))
    if remaining < needed:
        raise DailyQuotaExhausted(
            f"This refresh needs about {needed:,} OpenAlex requests but only "
            f"{remaining:,} remain today"
            + (f" (resets in about {hours:.1f} hours)" if hours else "")
            + ". Stopping before spending any of it: a half-fetched run wastes "
              "the remainder and fails somewhere arbitrary. Re-run after the "
              "reset, or lower inclusion.top_journals_* in config.yaml.")


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
        # Last year with any output. Free — it is already on the record — and
        # it is the only way to tell a live journal from the predecessor record
        # OpenAlex keeps after a rename or a change of publisher. JRSS Series A
        # appears twice: the current OUP title and the old Wiley one.
        "last_active_year": max(
            (c.get("year") for c in (src.get("counts_by_year") or [])
             if (c.get("works_count") or 0) > 0), default=None),
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


def fetch_openalex_top_journals(cfg, manifest, session, limit: int) -> dict:
    """The most-cited journals in the world, whatever their publisher.

    The allowlist is hand-written, so it will always be missing someone: the
    Institute of Mathematical Statistics was absent, which removed all four
    Annals titles from the site entirely — not "no deal", but no entry at all,
    which reads as a broken tool. Ranking by citations is objective and
    self-maintaining, and at 200 records per request it costs ~$0.003 to take
    several thousand. The Annals of Statistics sits at 637,000 citations, far
    inside any sensible cut.
    """
    out: dict[str, dict] = {}
    base = cfg["sources"]["openalex_api"] + "/sources"
    cursor, page = "*", 0
    while cursor and len(out) < limit:
        data = openalex_get(cfg, manifest, session, base, f"openalex_top_{page}",
                            {"filter": "type:journal",
                             "sort": "cited_by_count:desc",
                             "per-page": PER_PAGE, "cursor": cursor})
        for src in data.get("results", []):
            rec = compact_openalex(src)
            if rec["issn_l"]:
                out.setdefault(rec["issn_l"], rec)
        cursor = (data.get("meta") or {}).get("next_cursor")
        page += 1
        time.sleep(0.15)
    print(f"  top-cited journals: {len(out)} fetched over {page} pages")
    return out


SWEEP_MARKER = DATA / "state" / "subfield_sweep.json"


def subfield_sweep_due(cfg, today: datetime.date) -> tuple[bool, str]:
    """Should the subfield sweep run this time? Returns (due, why).

    The sweep costs ~500 requests and ~13 minutes, and it is a *discovery*
    step: it finds which journals lead each discipline, and the answer to that
    does not change from one Monday to the next. Once a journal has been
    discovered it stays in scope through known_journals.tsv for 365 days, and
    its facts are re-fetched every run like everything else — so running the
    sweep quarterly costs nothing in freshness. Scope is accumulated; facts
    never are.

    Re-runs early if the configured depth changed, since a larger number means
    journals the stored set was never asked about.
    """
    per_subfield = cfg["inclusion"]["top_journals_per_subfield"]
    if per_subfield <= 0:
        return False, "disabled (top_journals_per_subfield: 0)"
    if os.environ.get("APC_FORCE_SUBFIELD_SWEEP") == "1":
        return True, "forced by APC_FORCE_SUBFIELD_SWEEP"
    if not SWEEP_MARKER.exists():
        return True, "never run"
    marker = read_json(SWEEP_MARKER)
    if marker.get("per_subfield") != per_subfield:
        return True, (f"depth changed {marker.get('per_subfield')} -> {per_subfield}")
    every = int(cfg["inclusion"].get("subfield_sweep_days", 90))
    try:
        age = (today - datetime.date.fromisoformat(marker["last_run"])).days
    except (KeyError, ValueError):
        return True, "marker unreadable"
    if age >= every:
        return True, f"last run {age} days ago (every {every})"
    return False, (f"last run {age} days ago; next in {every - age} days "
                   f"({marker.get('journals', 0):,} journals still in scope "
                   "via known_journals.tsv)")


def fetch_openalex_top_by_subfield(cfg, manifest, session, per_subfield: int) -> dict:
    """The leading journals *within each subfield*, not just globally.

    A global citation ranking is dominated by biomedicine and physics: a top-15,000
    cut still reaches only the largest journals in small disciplines, and citation
    volume varies by an order of magnitude between fields. Ranking within each of
    OpenAlex's 252 subfields gives every discipline its own head of the
    distribution, which is what a historian or a statistician actually needs.

    Sources cannot be filtered by subfield directly, so topics are fetched once
    (a few paged requests), grouped by subfield locally, and each subfield's
    topic set becomes one OR-filter.

    Runs on its own slow cadence — see subfield_sweep_due.
    """
    out: dict[str, dict] = {}
    if per_subfield <= 0:
        print("  subfield sweep disabled (top_journals_per_subfield: 0)")
        return {"journals": out, "failed": [], "subfields": 0}
    base = cfg["sources"]["openalex_api"]

    # 1. every topic, grouped by its subfield
    subfields: dict[str, list[str]] = {}
    cursor, page = "*", 0
    while cursor:
        data = openalex_get(cfg, manifest, session, base + "/topics",
                            f"openalex_topics_{page}",
                            {"per-page": PER_PAGE, "cursor": cursor,
                             "select": "id,subfield"})
        for t in data.get("results", []):
            sub = (t.get("subfield") or {}).get("id")
            tid = (t.get("id") or "").rsplit("/", 1)[-1]
            if sub and tid:
                subfields.setdefault(sub.rsplit("/", 1)[-1], []).append(tid)
        cursor = (data.get("meta") or {}).get("next_cursor")
        page += 1
        time.sleep(0.15)
    print(f"  {len(subfields)} subfields over {sum(len(v) for v in subfields.values())} topics")

    # 2. the leading journals inside each.
    #
    # Errors are caught PER SUBFIELD, not for the sweep as a whole. The first
    # live run of this stage died 34 subfields in and the whole sweep was
    # discarded — 68 requests paid for and nothing kept, because one failure
    # threw away 251 other subfields' work. There is no reason a bad response
    # for organic chemistry should cost us law.
    failed: list[str] = []
    for n, (sub, topics) in enumerate(sorted(subfields.items()), 1):
        seen, cursor, page = 0, "*", 0
        try:
            while cursor and seen < per_subfield:
                data = openalex_get(
                    cfg, manifest, session, base + "/sources",
                    f"openalex_subfield_{sub}_p{page}",
                    {"filter": "topics.id:" + "|".join(topics) + ",type:journal",
                     "sort": "cited_by_count:desc",
                     "per-page": min(PER_PAGE, per_subfield - seen), "cursor": cursor})
                results = data.get("results", [])
                for src in results:
                    rec = compact_openalex(src)
                    if rec["issn_l"]:
                        out.setdefault(rec["issn_l"], rec)
                seen += len(results)
                cursor = (data.get("meta") or {}).get("next_cursor")
                page += 1
                time.sleep(0.15)
        except DailyQuotaExhausted:
            # Out of budget: every remaining subfield would fail the same way,
            # so stop sweeping and keep what we have rather than burning the
            # rest of the run discovering that 218 more times.
            print(f"  daily allowance exhausted at subfield {n}/{len(subfields)}; "
                  f"keeping the {len(out):,} journals found so far")
            failed.extend(sorted(subfields)[n - 1:])
            break
        except Exception as exc:                        # noqa: BLE001
            failed.append(sub)
            print(f"    subfield {sub} failed ({exc}); continuing")
        if n % 50 == 0:
            print(f"    subfields {n}/{len(subfields)}: {len(out)} journals so far")
    if failed:
        print(f"  {len(failed)} of {len(subfields)} subfields could not be fetched: "
              + ", ".join(failed[:8]) + ("…" if len(failed) > 8 else ""))
    print(f"  top-{per_subfield}-per-subfield: {len(out)} distinct journals")
    return {"journals": out, "failed": failed, "subfields": len(subfields)}


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
        # Tri-state, not boolean. `== "yes"` would turn a blank or any future
        # third value into "No APC (per DOAJ)" — asserting a journal is free on
        # the strength of a field we did not understand. Every row is currently
        # Yes or No, so this changes nothing today and prevents a false claim of
        # free if that ever stops being true.
        raw_apc = (row.get("APC") or "").strip().lower()
        has_apc = True if raw_apc == "yes" else False if raw_apc == "no" else None
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
            # What it is like to submit here. All present for every DOAJ
            # journal, and all already in this download — no extra requests.
            # Word limits and LaTeX policy are deliberately absent: they exist
            # only as prose in each journal's own guidelines, so the site links
            # there rather than pretending to hold them.
            "submission": {
                "author_instructions_url":
                    (row.get("URL for journal's instructions for authors") or "").strip() or None,
                "review_process": [r.strip() for r in
                                   (row.get("Review process") or "").split(",") if r.strip()],
                "weeks_to_publication":
                    (row.get("Average number of weeks between article submission and publication") or "").strip() or None,
                "plagiarism_screening":
                    (row.get("Journal plagiarism screening policy") or "").strip().lower() == "yes",
                "author_retains_copyright":
                    (row.get("Author holds copyright without restrictions") or "").strip().lower() == "yes",
                "persistent_ids": [x.strip() for x in
                                   (row.get("Persistent article identifiers") or "").split(",") if x.strip()],
                "deposit_policy": (row.get("Deposit policy directory") or "").strip() or None,
                "deposit_policy_url": (row.get("URL for deposit policy") or "").strip() or None,
            },
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
    worldwide = deals.get("agreement_issns_worldwide") or []
    remembered = known_journal_issns()
    print(f"{len(set(deal_issns))} distinct Oxford deal ISSNs; "
          f"{len(worldwide)} in agreements worldwide; "
          f"{len(remembered)} journals remembered from previous runs")

    allow = yaml.safe_load((CURATED / "publisher_allowlist.yaml").read_text())["publishers"]

    print("Fetching DOAJ journals …")
    doaj, doaj_primary = fetch_doaj(cfg, manifest, session)
    print("Fetching DOAJ withdrawal changelog …")
    withdrawn = fetch_doaj_withdrawn(cfg, manifest, session)

    today = datetime.date.today()
    sweep_due, sweep_why = subfield_sweep_due(cfg, today)
    print(f"Subfield sweep: {'running' if sweep_due else 'skipped'} — {sweep_why}")

    # A rough forecast is enough to catch "nowhere near enough left today".
    # The sweep term was a flat 300 whether or not it ran, and whatever depth
    # was configured; at 250 per subfield it really costs ~2 pages for each of
    # ~252 subfields, so the guard under-forecast by 40% exactly when it
    # mattered most.
    per_subfield = cfg["inclusion"]["top_journals_per_subfield"]
    sweep_cost = (SUBFIELD_COUNT * -(-per_subfield // PER_PAGE) + 10) if sweep_due else 0
    estimated = (len(set(deal_issns + doaj_primary + worldwide)) // OPENALEX_BATCH
                 + len(allow) + 40                       # publisher sweep
                 + cfg["inclusion"]["top_journals_by_citations"] // PER_PAGE
                 + sweep_cost
                 + 60)                                   # headroom
    check_openalex_budget(cfg, session, estimated)

    # Order matters for cost. The sweeps below return whole publisher and
    # ranking lists in ~200-record pages, and most remembered journals arrived
    # that way originally — so looking them up individually first meant paying
    # for 31,220 ISSNs a second time. Sweep first, then fetch only what is
    # genuinely still missing.
    print("Fetching OpenAlex records for deal + DOAJ ISSNs …")
    openalex = fetch_openalex_by_issns(cfg, manifest, session,
                                       deal_issns + doaj_primary + worldwide)

    print("Fetching OpenAlex records for allowlisted publishers …")
    pub_recs = fetch_openalex_by_publishers(cfg, manifest, session, allow)
    for k, v in pub_recs.items():
        openalex.setdefault(k, v)

    by_subfield: dict = {}
    if sweep_due:
        print("Fetching the leading journals in each subfield …")
        try:
            sweep = fetch_openalex_top_by_subfield(
                cfg, manifest, session, per_subfield)
            by_subfield = sweep["journals"]
            # Record the sweep only if it substantially completed. A run that
            # covered a tenth of the disciplines has not answered the question
            # "which journals lead each field", and marking it done would sit
            # on that partial answer for a quarter. Whatever it did find is
            # still kept and still enters known_journals.tsv.
            covered = sweep["subfields"] - len(sweep["failed"])
            if sweep["subfields"] and covered >= 0.9 * sweep["subfields"]:
                write_json(SWEEP_MARKER, {
                    "last_run": today.isoformat(),
                    "per_subfield": per_subfield,
                    "journals": len(by_subfield),
                    "subfields": sweep["subfields"],
                    "failed_subfields": len(sweep["failed"]),
                })
            else:
                print(f"  only {covered}/{sweep['subfields']} subfields completed — "
                      "not recording this as the quarterly sweep; the next run "
                      "will try again.")
        except DailyQuotaExhausted:
            raise            # a real budget problem; the run should stop
        except Exception as exc:                        # noqa: BLE001
            # Discovery, not facts. Losing it costs the journals this sweep
            # would have added — and the previously discovered ones are still
            # in scope via known_journals.tsv — so it must not cost the whole
            # refresh. The marker is deliberately NOT written, so the next run
            # tries again rather than waiting out the quarter.
            print(f"  subfield sweep failed ({exc}); continuing without it. "
                  "Previously discovered journals remain in scope.")
    for k, v in by_subfield.items():
        openalex.setdefault(k, v)

    print("Fetching the most-cited journals worldwide …")
    top = fetch_openalex_top_journals(
        cfg, manifest, session, cfg["inclusion"]["top_journals_by_citations"])
    for k, v in top.items():
        openalex.setdefault(k, v)

    # Whatever the sweeps did not already return. This is the guarantee that a
    # journal cannot vanish because a source had a bad day; taking it last
    # makes it cheap.
    still_missing = sorted(remembered - set(openalex))
    print(f"Re-checking {len(still_missing)} remembered journals the sweeps "
          f"did not return (of {len(remembered)} remembered) …")
    if still_missing:
        for k, v in fetch_openalex_by_issns(cfg, manifest, session,
                                            still_missing).items():
            openalex.setdefault(k, v)

    meta = {
        "generated": utcnow(),
        "sources": {
            "openalex": {"url": cfg["sources"]["openalex_api"], "license": "CC0"},
            "doaj": {"url": cfg["sources"]["doaj_api"], "license": "CC0 (metadata)"},
            "doaj_withdrawn": {"url": cfg["sources"]["doaj_withdrawn_sheet_csv"],
                                "license": "CC BY-SA 4.0"},
        },
        # Carried through to validate.py, which fails the build if any source
        # comes back far smaller than expected. A half-fetched source hides
        # inside a whole-dataset drop threshold.
        "counts": {
            "openalex": len(openalex),
            "doaj_journals": len(doaj_primary),
            "doaj_issns": len(doaj),
            "withdrawn": len(withdrawn),
        },
        # Inclusion route: present because it is among the most-cited journals,
        # regardless of publisher, DOAJ status or any agreement.
        "top_cited": sorted(top),
        "top_by_subfield": sorted(by_subfield),
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
