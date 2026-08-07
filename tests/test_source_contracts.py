"""Contract tests: do the upstream sources still look the way we parse them?

Every serious bug this project has hit was an upstream shape assumption that
was wrong or went stale — a paginated API that caps out at 1,000 records, a
header cell with a trailing space, an API that signals "not found" with HTTP
200 and a bare integer. Unit tests cannot catch those, because they test our
parsing of data we made up.

These run against the live sources on a schedule, so a source change surfaces
as a failing check rather than as a broken weekly build (or worse, a build that
succeeds with silently empty data).

Deselected by default. Run with:  pytest -m network
"""
from __future__ import annotations

import csv
import io
import os

import pytest
import requests

from common import load_config, normalise_issn
from fetch_metadata import _find_column, parse_apc_amount

pytestmark = pytest.mark.network

CFG = load_config()
TIMEOUT = 90
UA = {"User-Agent": "oxford-apc-finder/contract-tests"}


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update(UA)
    return s


def _csv(session, url):
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))


# ------------------------------------------------------------------- JCT
def test_jct_index_has_the_columns_we_read(session):
    rows = _csv(session, CFG["sources"]["jct_ta_index_csv"])
    header = rows[0]
    for col in ("ESAC ID", "End Date", "C/A Only", "Data URL"):
        assert col in header, f"JCT index lost column {col!r}: {header}"
    assert len(rows) > 100, "JCT index suspiciously short"


def test_jct_agreement_csv_has_journal_and_institution_blocks(session):
    """One agreement CSV must still interleave journal rows and institution
    rows, with the 'Last Seen' columns we use to drop departed entries."""
    index = _csv(session, CFG["sources"]["jct_ta_index_csv"])
    header, rows = index[0], index[1:]
    url_col = header.index("Data URL")
    data_url = next(r[url_col] for r in rows
                    if len(r) > url_col and r[url_col].startswith("http"))

    agreement = _csv(session, data_url)
    cols = agreement[0]
    for col in ("Journal Name", "ISSN (Print)", "ISSN (Online)",
                "Journal Last Seen", "ROR ID", "Institution Last Seen"):
        assert col in cols, f"agreement CSV lost column {col!r}: {cols}"


def test_jct_api_signals_no_agreement_with_a_bare_integer(session):
    """Regression: the API answers 'no agreement' with HTTP 200 and a body of
    `404`, not a list and not an HTTP error. validate.py must keep handling
    both shapes."""
    api = CFG["sources"]["jct_api"]
    ror = CFG["institution_ror"]

    covered = session.get(f"{api}/ta", params={"issn": "0028-0836", "ror": ror},
                          timeout=TIMEOUT)
    assert covered.status_code == 200
    assert isinstance(covered.json(), list)
    assert covered.json()[0]["result"]["compliant"] == "yes"

    missing = session.get(f"{api}/ta", params={"issn": "0000-0000", "ror": ror},
                          timeout=TIMEOUT)
    assert missing.status_code == 200
    assert not isinstance(missing.json(), list), (
        "JCT now returns a list for unknown ISSNs — validate.py can be simplified")


# ------------------------------------------------------------------ DOAJ
def test_doaj_bulk_csv_is_reachable_and_complete(session):
    """The paged API caps at 1,000 records, so the bulk CSV is the only way to
    see all of DOAJ. If it breaks, the pipeline must not quietly fall back."""
    rows = _csv(session, CFG["sources"]["doaj_journal_csv"])
    assert len(rows) > 15000, f"DOAJ CSV has only {len(rows)} rows"
    header = rows[0]
    for col in ("Journal title", "Journal ISSN (print version)",
                "Journal EISSN (online version)", "APC", "APC amount",
                "Keywords", "Publisher", "URL in DOAJ",
                "Journal waiver policy (for developing country authors etc)",
                "URL for journal's aims & scope"):
        assert col in header, f"DOAJ CSV lost column {col!r}"


def test_doaj_apc_amounts_still_parse(session):
    rows = _csv(session, CFG["sources"]["doaj_journal_csv"])
    header, body = rows[0], rows[1:]
    amount_col = header.index("APC amount")
    amounts = [r[amount_col] for r in body
               if len(r) > amount_col and r[amount_col].strip()]
    assert len(amounts) > 1000
    parsed = [parse_apc_amount(a) for a in amounts]
    failed = [a for a, p in zip(amounts, parsed) if p is None]
    assert len(failed) < len(amounts) * 0.02, f"APC format changed: {failed[:5]}"


def test_doaj_paged_api_still_caps_at_1000(session):
    """Documents why the bulk CSV is used. If DOAJ ever lifts this, the API
    path becomes viable again and this test tells us."""
    base = CFG["sources"]["doaj_api"] + "/search/journals/%2A"
    r = session.get(base, params={"page": 11, "pageSize": 100}, timeout=TIMEOUT)
    assert r.status_code == 400, (
        "DOAJ now paginates past 1,000 records — the API path could replace "
        "the 25MB CSV download")


def test_doaj_withdrawal_changelog_columns_resolve(session):
    """Regression: the header is prose, and the tidy cells carry trailing
    spaces ('ISSN '). Column 0 contains the words 'issn' and 'reasons', so an
    exact-match-first lookup is required."""
    rows = _csv(session, CFG["sources"]["doaj_withdrawn_sheet_csv"])
    header = rows[0]
    issn_col = _find_column(header, "issn")
    reason_col = _find_column(header, "reason")
    assert issn_col is not None and reason_col is not None
    assert issn_col != reason_col

    parsed = [r for r in rows[1:]
              if len(r) > issn_col and normalise_issn(r[issn_col])]
    assert len(parsed) > 500, f"only {len(parsed)} withdrawal rows parsed"

    reasons = {r[reason_col].strip().lower() for r in parsed
               if len(r) > reason_col}
    assert any("best practice" in x for x in reasons), (
        "no misconduct-type reasons found — the exclusion would do nothing")


# -------------------------------------------------------------- OpenAlex
def test_openalex_source_record_has_the_fields_we_use(session):
    params = {"filter": "issn:0028-0836"}
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    r = session.get(CFG["sources"]["openalex_api"] + "/sources",
                    params=params, timeout=TIMEOUT)
    r.raise_for_status()
    src = r.json()["results"][0]
    for field in ("id", "issn_l", "issn", "display_name",
                  "host_organization_name", "is_in_doaj", "type",
                  "works_count", "apc_prices", "topics"):
        assert field in src, f"OpenAlex source lost field {field!r}"


def test_openalex_still_bills_per_request_not_per_record(session):
    """The whole fetch strategy (100 ISSNs per query, 200 per page) rests on
    cost being flat per request."""
    params = {"filter": "issn:0028-0836|1476-4687|2041-1723", "per-page": 200}
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    r = session.get(CFG["sources"]["openalex_api"] + "/sources",
                    params=params, timeout=TIMEOUT)
    r.raise_for_status()
    assert r.json()["meta"].get("cost_usd", 0) <= 0.0002


def test_openalex_publisher_lineage_filter_works(session):
    """The publisher sweep depends on this filter; without it the fallback is
    a name search that blows the daily budget."""
    params = {"filter": "host_organization_lineage:P4310320990,type:journal",
              "per-page": 1}
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    r = session.get(CFG["sources"]["openalex_api"] + "/sources",
                    params=params, timeout=TIMEOUT)
    r.raise_for_status()
    assert r.json()["meta"]["count"] > 100


def test_openalex_issn_batch_of_100_is_accepted(session):
    """Batch size is tuned to the request budget; if the OR-filter limit drops,
    OPENALEX_BATCH must come down with it."""
    issns = "|".join(f"0028-08{i:02d}" for i in range(36, 136))
    params = {"filter": f"issn:{issns}", "per-page": 200}
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    r = session.get(CFG["sources"]["openalex_api"] + "/sources",
                    params=params, timeout=TIMEOUT)
    assert r.status_code == 200, r.text[:200]
    assert "error" not in r.json()


# -------------------------------------------------------------- Bodleian
def test_bodleian_pages_are_reachable(session):
    for key in ("bodleian_deals", "bodleian_apc", "bodleian_block_grants"):
        r = session.get(CFG["sources"][key], timeout=TIMEOUT)
        assert r.status_code == 200, f"{key} -> {r.status_code}"


def test_bodleian_deals_page_still_lists_publishers_we_curate(session):
    """The curated overlay is read off this page. If these publishers vanish
    from it, the overlay needs a human look."""
    r = session.get(CFG["sources"]["bodleian_deals"], timeout=TIMEOUT)
    r.raise_for_status()
    text = r.text.lower()
    for publisher in ("elsevier", "wiley", "springer nature", "sage",
                      "royal society of chemistry", "mdpi", "frontiers"):
        assert publisher in text, f"{publisher!r} no longer on the deals page"
