"""Optional stage — pull usage statistics back from GoatCounter.

The site is static, so it cannot count its own readers. GoatCounter does the
counting; this stage reads the aggregate back through its API and writes
data/out/usage.json, which build_site ships to the site like any other data
file. Same shape as everything else here: fetched on a schedule, committed, and
served as flat JSON.

Two rules govern this file.

  * It must never break a build. Usage numbers are a nice-to-have; journal data
    is the product. Every failure path here exits 0 and leaves any previous
    usage.json in place, so a GoatCounter outage costs the site a chart, not a
    refresh.
  * It must never publish a number that identifies a person. Oxford is a small
    population and looking a journal up is close to saying "I am thinking of
    submitting here". Counts below the configured floor are aggregated into an
    "others" bucket rather than shown, and raw search text is only ever
    published once several different sessions have typed the same thing.

No LLM: this is arithmetic over counts.
"""
from __future__ import annotations

import datetime
import os
import sys
import time

import requests

from common import OUT, load_config, read_json, utcnow, write_json

TIMEOUT = 30
# GoatCounter paginates; this is plenty for a site of this size and bounds the
# work if a misconfigured beacon ever starts emitting unbounded distinct paths.
PAGE_LIMIT = 500

# "All time" has to start somewhere. This predates the site, so nothing earlier
# can exist — and the date actually published is derived from the first day
# that recorded any traffic, so the label stays true without a config value
# anyone has to remember to update.
ALL_TIME_START = "2026-01-01"


# GoatCounter allows 4 requests a second. Five calls fired in a burst earned a
# 429 on the last one, which degraded to "0 journal lookups" — a wrong number
# rather than a missing one. Pacing is cheaper than handling that.
RATE_LIMIT_PAUSE = 0.3


def _api(base: str, path: str, token: str, params: dict) -> dict | None:
    """One API call. Returns None on any failure — callers degrade, never raise.

    None means "no answer", and callers must not read it as zero: a rate limit
    that silently becomes a count of nothing is worse than no figure at all.
    """
    for attempt in range(2):
        time.sleep(RATE_LIMIT_PAUSE)
        try:
            resp = requests.get(
                f"{base}/api/v0{path}",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                params=params, timeout=TIMEOUT)
            if resp.status_code == 429 and attempt == 0:
                wait = float(resp.headers.get("retry-after") or 2)
                print(f"  {path}: rate limited, waiting {wait:.0f}s")
                time.sleep(min(wait, 10))
                continue
            if resp.status_code != 200:
                print(f"  {path}: HTTP {resp.status_code} — skipping")
                return None
            return resp.json()
        except Exception as exc:                    # noqa: BLE001
            print(f"  {path}: {exc} — skipping")
            return None
    print(f"  {path}: still rate limited — skipping")
    return None


def _first_int(d: dict, *names: str) -> int:
    """Read whichever of these keys the API version in use actually returns.

    GoatCounter has changed how it reports totals across releases. Guessing one
    field name and getting a zero would be reported to readers as "0 visitors",
    which is worse than reporting nothing.
    """
    for n in names:
        v = d.get(n)
        if isinstance(v, int):
            return v
    return 0


def split_journal_path(path: str) -> tuple[str, str, str] | None:
    """'/j/covered/all/1234-5678' -> ('covered', 'all', '1234-5678').

    The deal status and the state of the deal-only filter are both carried in
    the path rather than sent as extra events, so one beacon call yields the
    per-journal count, the coverage split and the scope, and none of them can
    disagree with the others.

    The three-segment form is what the site posted before the scope was added.
    Those views are real and still count towards the chart; they just cannot
    contribute to the coverage share, so they are read as scope 'deals' — the
    filter was on by default, which is what they mostly were.
    """
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts or parts[0] != "j":
        return None
    if len(parts) == 4:
        return parts[1], parts[2], parts[3]
    if len(parts) == 3:
        return parts[1], "deals", parts[2]
    return None


def first_traffic_day(totals: dict) -> str | None:
    """The earliest day that recorded anything, from /stats/total's day array.

    Derived rather than configured: "since we started counting" is a claim
    about the data, so it should come from the data. A hard-coded launch date
    would quietly become a lie the first time the counter was reset or moved.
    """
    for day in (totals.get("stats") or []):
        if not isinstance(day, dict):
            continue
        if (day.get("daily") or sum(day.get("hourly") or [])) > 0:
            return day.get("day")
    return None


def totals_block(hits: list[dict] | None, totals: dict,
                 since: str | None = None) -> dict:
    """Headline counts for one time window.

    `hits` of None means the call for them did not come back. The journal
    figures are then omitted rather than reported as zero — a rate limit that
    turns into "0 journal lookups" is a confident wrong answer, and the page
    would print it in bold.
    """
    if hits is None:
        return {
            "since": since,
            "page_loads": max(0, _first_int(totals, "total", "total_utc")
                              - _first_int(totals, "total_events")),
            "interactions": _first_int(totals, "total_events"),
        }
    journals: dict[str, int] = {}
    for h in hits:
        parsed = split_journal_path(h.get("path", ""))
        if parsed:
            journals[parsed[2]] = journals.get(parsed[2], 0) + (h.get("count", 0) or 0)
    return {
        "since": since,
        # `total` counts events too, so page loads are the difference — every
        # journal opened and every missed search would otherwise inflate it.
        "page_loads": max(0, _first_int(totals, "total", "total_utc")
                          - _first_int(totals, "total_events")),
        "interactions": _first_int(totals, "total_events"),
        "journal_views": sum(journals.values()),
        "distinct_journals_viewed": len(journals),
    }


def summarise(hits: list[dict], locations: list[dict], totals: dict,
              corpus: dict, cfg: dict) -> dict:
    min_views = cfg.get("min_views_to_publish", 5)
    min_searches = cfg.get("min_searches_to_publish", 3)

    journals: dict[str, dict] = {}
    by_status: dict[str, int] = {}
    missing: list[dict] = []
    # Views taken with the deal-only filter OFF. These are the only ones that
    # can support a coverage share: with the filter on — its default — the
    # reader is choosing from a list that is already 100% covered, so counting
    # those would measure the default setting, not researchers' behaviour.
    unfiltered: dict[str, dict] = {}

    for h in hits:
        path, count = h.get("path", ""), h.get("count", 0) or 0
        title = (h.get("title") or "").strip()
        parsed = split_journal_path(path)
        if parsed:
            status, scope, issn = parsed
            by_status[status] = by_status.get(status, 0) + count
            rec = journals.setdefault(
                issn, {"issn_l": issn, "title": title, "status": status, "views": 0})
            rec["views"] += count
            # Titles can change between builds; prefer a non-empty one.
            if title and not rec["title"]:
                rec["title"] = title
            if scope == "all":
                u = unfiltered.setdefault(
                    issn, {"status": status, "views": 0})
                u["views"] += count
        elif path.startswith("/missing/"):
            missing.append({"query": path[len("/missing/"):], "searches": count})

    ranked = sorted(journals.values(), key=lambda r: -r["views"])
    shown = [r for r in ranked if r["views"] >= min_views]
    withheld = [r for r in ranked if r["views"] < min_views]

    total_views = sum(r["views"] for r in ranked)
    free_views = sum(r["views"] for r in unfiltered.values())
    covered_views = sum(r["views"] for r in unfiltered.values()
                        if r["status"] == "covered")
    distinct_covered = sum(1 for r in unfiltered.values() if r["status"] == "covered")

    # The interesting number is not the coverage rate on its own — it is the
    # coverage rate compared with the corpus. Above the baseline means the
    # deals are aimed at what people actually publish in; below it means there
    # is a gap, and that is worth someone knowing.
    corpus_total = corpus.get("total") or 0
    corpus_covered = corpus.get("covered") or 0
    baseline = (corpus_covered / corpus_total) if corpus_total else None

    return {
        "generated": utcnow(),
        "window_days": cfg.get("window_days", 90),
        # GoatCounter's /stats/total returns `total`, `total_events` and
        # `total_utc` — and nothing else. There is no unique-visitor figure in
        # the API, so none is published: a lookup that fell through to 0 printed
        # "0 visitors" next to "18 sessions", which is worse than saying nothing.
        #
        # `total` also COUNTS the events, so it is not a page-load count either:
        # every journal opened and every missed search inflates it. Page loads
        # are the difference.
        "totals": {
            "page_loads": max(0, _first_int(totals, "total", "total_utc")
                              - _first_int(totals, "total_events")),
            "interactions": _first_int(totals, "total_events"),
            "journal_views": total_views,
            "distinct_journals_viewed": len(ranked),
            "countries": len(locations),
        },
        "coverage": {
            "by_status_views": by_status,
            # Measured only over views taken with the deal filter off, and the
            # sample size is published alongside so the figure can be read with
            # the confidence it actually deserves.
            "basis": "views with the deal-only filter off",
            "sample_views": free_views,
            "sample_journals": len(unfiltered),
            "covered_view_share": (covered_views / free_views) if free_views else None,
            "covered_journal_share": ((distinct_covered / len(unfiltered))
                                      if unfiltered else None),
            "corpus_share": baseline,
        },
        "top_journals": [
            {"issn_l": r["issn_l"], "title": r["title"],
             "status": r["status"], "views": r["views"]}
            for r in shown[:25]
        ],
        # Say what was left out rather than silently truncating: a chart that
        # quietly drops the tail reads as "this is everything".
        "withheld": {
            "journals": len(withheld),
            "views": sum(r["views"] for r in withheld),
            "min_views_to_publish": min_views,
        },
        "most_wanted": [
            m for m in sorted(missing, key=lambda m: -m["searches"])
            if m["searches"] >= min_searches
        ][:15],
        "top_countries": [
            {"code": l.get("id") or l.get("code") or "",
             "name": l.get("name") or "", "count": l.get("count", 0)}
            for l in locations[:10]
        ],
    }


def main() -> None:
    cfg_all = load_config()
    cfg = cfg_all.get("analytics") or {}
    code = cfg.get("goatcounter_code")
    token = os.environ.get("GOATCOUNTER_TOKEN", "").strip()

    if not code:
        print("analytics.goatcounter_code is not set — usage monitoring is off.")
        return
    if not token:
        # Deliberately not an error. A fork, or a local run, has no token and
        # should still build the whole site.
        print("GOATCOUNTER_TOKEN is not set — skipping usage stats.")
        return

    base = f"https://{code}.goatcounter.com"
    end = datetime.date.today()
    start = end - datetime.timedelta(days=int(cfg.get("window_days", 90)))
    window = {"start": start.isoformat(), "end": end.isoformat()}

    print(f"Reading usage from {base} ({window['start']} → {window['end']})")
    totals = _api(base, "/stats/total", token, window) or {}
    hits_resp = _api(base, "/stats/hits", token, {**window, "limit": PAGE_LIMIT}) or {}
    loc_resp = _api(base, "/stats/locations", token, {**window, "limit": PAGE_LIMIT}) or {}

    # The rolling window drives the charts, but "how much has this been used at
    # all" is a different and more interesting question, and a 90-day figure
    # answers it wrongly the moment the tool is older than 90 days.
    all_window = {"start": ALL_TIME_START, "end": end.isoformat()}
    all_totals = _api(base, "/stats/total", token, all_window) or {}
    all_hits_resp = _api(base, "/stats/hits", token,
                         {**all_window, "limit": PAGE_LIMIT})
    all_hits = all_hits_resp.get("hits") or [] if all_hits_resp is not None else None

    hits = hits_resp.get("hits") or []
    locations = loc_resp.get("stats") or []

    if not hits and not totals:
        # Nothing came back at all. Leave any previous usage.json alone rather
        # than overwriting real history with an empty file.
        print("No usage data returned — leaving the previous usage.json in place.")
        return

    journals_path = OUT / "journals.json"
    corpus = {}
    if journals_path.exists():
        counts = read_json(journals_path).get("counts") or {}
        corpus = {"total": counts.get("journals"), "covered": counts.get("covered")}

    usage = summarise(hits, locations, totals, corpus, cfg)
    usage["all_time"] = totals_block(all_hits, all_totals,
                                     since=first_traffic_day(all_totals))

    # A site that has counted nothing yet has nothing to say. Publishing zeros
    # would put a "How this site is used" link in the footer leading to a page
    # of noughts, which reads as a broken feature rather than a new one.
    if (not usage["all_time"]["page_loads"]
            and not usage["all_time"]["journal_views"]):
        print("No traffic counted yet — not publishing a usage page.")
        return

    write_json(OUT / "usage.json", usage)
    t = usage["totals"]
    a = usage["all_time"]
    lookups = (f"{a['journal_views']:,} journal lookups across "
               f"{a['distinct_journals_viewed']:,} journals"
               if "journal_views" in a else "journal counts unavailable")
    print(f"Usage since {a['since'] or '?'}: {a['page_loads']:,} page loads, {lookups}")
    print(f"  last {usage['window_days']} days: {t['page_loads']:,} page loads, "
          f"{t['distinct_journals_viewed']:,} journals looked up "
          f"({usage['withheld']['journals']} below the publication floor)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                        # noqa: BLE001
        # The last line of defence. Nothing in this file is worth failing a
        # refresh over.
        print(f"Usage stage failed ({exc}) — continuing without usage data.",
              file=sys.stderr)
    sys.exit(0)
