"""Unit tests for the pure functions in the pipeline.

Weighted towards the parsing edges that have actually broken this codebase:
upstream fields that are almost-but-not-quite the shape the code assumed.
Every test named `test_regression_*` pins a bug that shipped.
"""
from __future__ import annotations

import pytest

from build_site import cost_summary, shard_key
from changelog import COLUMNS, state_row
from collect_links import bucket_of
from common import normalise_issn
from fetch_jct import agreement_journals, institution_is_current
from fetch_metadata import _find_column, parse_apc_amount
from merge import (clean_text, effective_cost, match_override, scope_sentence)

OXFORD = "052gg0110"


# --------------------------------------------------------------- ISSNs
@pytest.mark.parametrize("raw,expected", [
    ("0028-0836", "0028-0836"),
    ("00280836", "0028-0836"),          # hyphen inserted
    ("2049-632x", "2049-632X"),         # check digit upcased
    (" 0028-0836 ", "0028-0836"),       # trimmed
    ("0028–0836", "0028-0836"),         # en dash
    ("ISSN-L: 2992-7862", None),        # regression: OpenAlex dirty value
    ("not-an-issn", None),
    ("1234-567", None),                 # too short
    ("", None),
    (None, None),
])
def test_normalise_issn(raw, expected):
    assert normalise_issn(raw) == expected


# --------------------------------------------------------- DOAJ parsing
@pytest.mark.parametrize("raw,expected", [
    ("700 BRL", {"price": 700, "currency": "BRL"}),
    ("40 USD; 450000 IDR", {"price": 40, "currency": "USD"}),      # USD preferred
    ("450000 IDR; 40 USD", {"price": 40, "currency": "USD"}),      # order-independent
    ("2000 GBP; 40 USD", {"price": 2000, "currency": "GBP"}),      # GBP wins
    ("1500 EUR; 40 USD", {"price": 1500, "currency": "EUR"}),      # EUR beats USD
    ("850000 IDR", {"price": 850000, "currency": "IDR"}),          # no preferred cur
    ("", None),
    (None, None),
    ("free", None),
])
def test_parse_apc_amount(raw, expected):
    assert parse_apc_amount(raw) == expected


def test_regression_find_column_prefers_exact_over_substring():
    """The DOAJ withdrawal sheet's first column is a prose paragraph that
    contains the words 'ISSN' and 'reasons'. A substring-first search picks it
    and silently mislabels the journal title as the reason — which is exactly
    how the misconduct exclusion came to match nothing."""
    header = [
        "A change log showing journals withdrawn from DOAJ. A journal may be "
        "withdrawn for the following reasons: ... Journal Title ",
        "ISSN ",
        "This data is licensed CC BY-SA ... Date Removed (dd/mm/yyyy) ",
        "Reason ",
    ]
    assert _find_column(header, "issn") == 1
    assert _find_column(header, "reason") == 3
    assert _find_column(header, "date removed") == 2


def test_find_column_missing_returns_none():
    assert _find_column(["a", "b"], "issn") is None


# ------------------------------------------------------ JCT Oxford filter
def _inst(ror, last_seen=""):
    return {"ROR ID": f"https://ror.org/{ror}", "Institution Last Seen": last_seen}


def test_institution_current_when_no_last_seen():
    assert institution_is_current([_inst(OXFORD)], OXFORD)


def test_regression_institution_that_left_is_excluded():
    """A 'Last Seen' date means the institution has left the agreement."""
    assert not institution_is_current([_inst(OXFORD, "2025-12-31")], OXFORD)


def test_other_institutions_do_not_qualify_oxford():
    assert not institution_is_current([_inst("012345678"), _inst("987654321")],
                                      OXFORD)


def test_oxford_current_among_many_institutions():
    rows = [_inst("012345678"), _inst(OXFORD), _inst("999999999", "2024-01-01")]
    assert institution_is_current(rows, OXFORD)


def test_agreement_journals_drops_departed_and_institution_rows():
    rows = [
        {"Journal Name": "Live Journal", "ISSN (Print)": "0028-0836",
         "ISSN (Online)": "1476-4687", "Journal Last Seen": ""},
        {"Journal Name": "Departed Journal", "ISSN (Print)": "1234-5678",
         "ISSN (Online)": "", "Journal Last Seen": "2025-06-01"},
        _inst(OXFORD),                      # institution-only row
    ]
    out = agreement_journals(rows)
    assert [j["name"] for j in out] == ["Live Journal"]
    assert out[0]["issns"] == ["0028-0836", "1476-4687"]


# ------------------------------------------------------------- cost logic
def _oa(**kw):
    return {"apc_prices": [], **kw}


def _doaj(has_apc=True, price=None, currency=None):
    return {"apc": {"has_apc": has_apc, "price": price, "currency": currency}}


def test_cost_covered_is_free_regardless_of_list_price():
    c = effective_cost("covered", None, _oa(apc_prices=[{"price": 3000, "currency": "USD"}]), None)
    assert c["kind"] == "covered"


def test_cost_diamond():
    assert effective_cost("diamond", None, _oa(), None)["kind"] == "diamond"


def test_cost_no_apc_from_doaj():
    assert effective_cost("none", None, _oa(), _doaj(has_apc=False))["kind"] == "no_apc"


def test_cost_discount_applies_to_doaj_price():
    c = effective_cost("discount", 15, _oa(), _doaj(price=2000, currency="GBP"))
    assert c["kind"] == "discount"
    assert c["estimated"] == {"price": 1700, "currency": "GBP"}
    assert c["list"] == {"price": 2000, "currency": "GBP"}


def test_cost_discount_prefers_doaj_price_over_openalex():
    c = effective_cost("discount", 20,
                       _oa(apc_prices=[{"price": 5000, "currency": "USD"}]),
                       _doaj(price=1000, currency="GBP"))
    assert c["list"]["currency"] == "GBP"
    assert c["estimated"]["price"] == 800


def test_cost_discount_without_a_base_price_states_so():
    c = effective_cost("discount", 15, _oa(), None)
    assert c["kind"] == "discount_unknown_base"
    assert "15%" in c["note"]


def test_cost_list_price_when_no_deal():
    c = effective_cost("none", None,
                       _oa(apc_prices=[{"price": 2500, "currency": "EUR"}]), None)
    assert c["kind"] == "list_price"
    assert c["list"] == {"price": 2500, "currency": "EUR"}


def test_cost_unknown_when_nothing_held():
    assert effective_cost("none", None, _oa(), None)["kind"] == "unknown"


def test_cost_never_invents_a_number():
    """Whatever the branch, a price shown must trace to a source price."""
    for status, pct in [("none", None), ("discount", 15), ("covered", None)]:
        c = effective_cost(status, pct, _oa(), None)
        assert "estimated" not in c or c.get("list")


# --------------------------------------------------------- display strings
@pytest.mark.parametrize("kind,journal,expected_fragment", [
    ("covered", {"cost": {"kind": "covered"}}, "£0"),
    ("diamond", {"cost": {"kind": "diamond"}}, "£0"),
    ("no_apc", {"cost": {"kind": "no_apc"}}, "No APC"),
    ("unknown", {"cost": {"kind": "unknown"}}, "unknown"),
])
def test_cost_summary_strings(kind, journal, expected_fragment):
    assert expected_fragment in cost_summary(journal)


def test_cost_summary_discount_shows_estimate_and_pct():
    s = cost_summary({"cost": {"kind": "discount", "pct": 15,
                               "estimated": {"price": 1700, "currency": "GBP"}}})
    assert "1,700" in s and "15%" in s


def test_regression_clean_text_decodes_html_entities():
    """Some OpenAlex titles carry the encoded form; the site escapes for
    display, so leaving them encoded shows a literal '&amp;' to the user."""
    assert clean_text("ACS ES&amp;T Water") == "ACS ES&T Water"
    assert clean_text("Endocrinology &#38; Metabolism") == "Endocrinology & Metabolism"
    assert clean_text("  padded  ") == "padded"
    assert clean_text(None) is None


def test_clean_text_leaves_legitimate_ampersands_alone():
    assert clean_text("A&A Practice") == "A&A Practice"


# ------------------------------------------------------------- overlay
def test_match_override_by_issn():
    entry = {"match_issns": ["2375-2548"]}
    assert match_override(entry, {"issns": ["2375-2548"], "publisher": "AAAS"})
    assert not match_override(entry, {"issns": ["0028-0836"], "publisher": "AAAS"})


def test_match_override_by_publisher_regex():
    entry = {"match_publisher_regex": "(?i)^MDPI"}
    assert match_override(entry, {"issns": [], "publisher": "MDPI AG"})
    assert not match_override(entry, {"issns": [], "publisher": "Elsevier BV"})


def test_match_override_anchored_regex_does_not_match_midstring():
    entry = {"match_publisher_regex": "(?i)^Frontiers"}
    assert not match_override(entry, {"issns": [], "publisher": "New Frontiers Press"})


# --------------------------------------------------------------- scope
def test_scope_sentence_is_none_without_topics():
    assert scope_sentence({"topics": []}) is None


def test_scope_sentence_names_topics_and_fields():
    s = scope_sentence({"topics": [{"name": "Epidemiology", "field": "Medicine"}],
                        "works_count": 1234})
    assert "Epidemiology" in s and "Medicine" in s and "1,234" in s


# ---------------------------------------------------------- build/shards
def test_shard_key_is_four_chars():
    assert shard_key("0028-0836") == "0028"


def test_shard_key_spreads_load():
    """Two characters put 9% of 43k journals in one shard, which the browser
    downloads in full to open a single journal."""
    issns = [f"{p:04d}-0000" for p in range(1000, 3000)]
    assert len({shard_key(i) for i in issns}) == len(issns)


# ------------------------------------------------------------ link buckets
def test_bucket_of_is_stable_and_in_range():
    url = "https://example.org/journal"
    assert bucket_of(url, 26) == bucket_of(url, 26)
    assert 0 <= bucket_of(url, 26) < 26


def test_bucket_of_spreads_urls():
    urls = [f"https://example.org/{i}" for i in range(2000)]
    buckets = {bucket_of(u, 26) for u in urls}
    assert len(buckets) == 26, "every bucket should get work"


# -------------------------------------------------------------- changelog
def test_state_row_matches_column_count():
    j = {"id": "0028-0836", "title": "Nature",
         "deal": {"status": "covered", "disputed": None},
         "cost": {"kind": "covered"}}
    assert len(state_row(j)) == len(COLUMNS)


def test_state_row_captures_discount_price_and_dispute():
    j = {"id": "0028-0836", "title": "X",
         "deal": {"status": "discount", "disputed": {"note": "n"}},
         "cost": {"kind": "discount", "estimated": {"price": 1700, "currency": "GBP"}}}
    row = dict(zip(COLUMNS, state_row(j)))
    assert row["price"] == "1700" and row["currency"] == "GBP"
    assert row["disputed"] == "1"


def test_state_row_escapes_tabs_in_titles():
    j = {"id": "0028-0836", "title": "Has\ttab",
         "deal": {"status": "none", "disputed": None}, "cost": {"kind": "unknown"}}
    assert "\t" not in state_row(j)[1]


# ------------------------------------------------- agreement expiry
import datetime  # noqa: E402

from merge import agreement_expired  # noqa: E402
from validate import (check_overlay_is_live, check_source_minimums)  # noqa: E402

TODAY = datetime.date(2026, 8, 7)


@pytest.mark.parametrize("end_date", [None, "", "not-a-date", "2026-08-07",
                                      "2027-12-31"])
def test_agreement_not_expired(end_date):
    assert agreement_expired(end_date, TODAY) is None


def test_regression_expired_agreement_is_flagged():
    """apa2024jisc ended 2026-07-31, yet 90 journals were still shown as
    '£0 — covered by Oxford deal' a week later."""
    got = agreement_expired("2026-07-31", TODAY)
    assert got == {"end_date": "2026-07-31", "days": 7}


def test_expiry_is_inclusive_of_the_end_date():
    """An agreement running 'to 2026-08-07' still covers you on the 7th."""
    assert agreement_expired("2026-08-07", TODAY) is None
    assert agreement_expired("2026-08-06", TODAY)["days"] == 1


# --------------------------------------------- source count floors
def _cfg(**floors):
    return {"validation": {"min_source_counts": floors}}


def test_source_minimums_pass_when_counts_are_healthy():
    data = {"source_counts": {"agreements": 42, "openalex": 43839}}
    assert check_source_minimums(data, _cfg(agreements=30, openalex=30000)) == []


def test_source_minimums_catch_a_truncated_fetch():
    """A half-fetched source hides inside a whole-dataset drop threshold,
    because DOAJ is only one of three inclusion routes."""
    data = {"source_counts": {"doaj_journals": 1535}}   # the pagination bug
    errors = check_source_minimums(data, _cfg(doaj_journals=18000))
    assert len(errors) == 1 and "truncated" in errors[0]


def test_source_minimums_treat_a_missing_count_as_failure():
    errors = check_source_minimums({"source_counts": {}}, _cfg(openalex=30000))
    assert len(errors) == 1 and "missing" in errors[0]


# ------------------------------------------ overlay liveness check
def _journal(esac=None, publisher=None, issns=(), disputed=None):
    return {"deal": {"esac_id": esac, "disputed": disputed},
            "publisher": publisher, "issns": list(issns)}


def test_overlay_entry_matching_a_journal_passes():
    overrides = {"entries": [{"kind": "caveat", "match_esac_prefix": "compbio",
                              "publisher_label": "Company of Biologists"}]}
    data = {"journals": [_journal(esac="compbio2025jisc")]}
    assert check_overlay_is_live(data, overrides) == []


def test_regression_overlay_entry_matching_nothing_fails_the_build():
    """'cob' and 'wolterskluwer' matched no agreement id for the life of the
    project, so their caveats — including 'Disease Models & Mechanisms is NOT
    covered' — never once displayed. A dead entry is indistinguishable from a
    caveat that simply did not apply."""
    overrides = {"entries": [{"kind": "caveat", "match_esac_prefix": "cob",
                              "publisher_label": "Company of Biologists"}]}
    data = {"journals": [_journal(esac="compbio2025jisc")]}
    errors = check_overlay_is_live(data, overrides)
    assert len(errors) == 1
    assert "Company of Biologists" in errors[0]


def test_overlay_entry_may_declare_itself_unreachable():
    """MDPI's discount is shadowed by a JCT agreement; the exception has to be
    stated explicitly rather than passing silently."""
    overrides = {"entries": [{"kind": "discount", "publisher_label": "MDPI",
                              "match_publisher_regex": "(?i)^MDPI",
                              "expect_no_match": True}]}
    assert check_overlay_is_live({"journals": []}, overrides) == []


def test_overlay_discount_matches_by_publisher_or_issn():
    overrides = {"entries": [
        {"kind": "discount", "publisher_label": "Frontiers",
         "match_publisher_regex": "(?i)^Frontiers"},
        {"kind": "diamond", "publisher_label": "PCJ",
         "match_issns": ["2804-3871"]},
    ]}
    data = {"journals": [_journal(publisher="Frontiers Media SA"),
                         _journal(issns=["2804-3871"])]}
    assert check_overlay_is_live(data, overrides) == []


# ------------------------------------------------- why this verdict
from merge import coverage_basis, override_match_reason  # noqa: E402


def test_basis_for_covered_names_the_agreement():
    b = coverage_basis("covered", esac_id="els2026jisc")
    assert "els2026jisc" in b and "title list" in b


def test_basis_for_no_deal_says_it_was_checked_not_unknown():
    """The distinction a researcher needs: searched and genuinely absent,
    versus the tool simply not knowing."""
    b = coverage_basis("none", agreement_count=42)
    assert "42" in b and "Checked against" in b
    assert "block grants" in b, "must not imply no support is possible"


def test_basis_for_gold_discount_explains_the_exclusion():
    b = coverage_basis("discount", discount_pct=15, not_in_agreement="Elsevier")
    assert "not on the Elsevier read-and-publish agreement" in b and "15%" in b


def test_basis_for_scheme_discount_names_the_scheme_and_match():
    b = coverage_basis("discount", scheme="MDPI", discount_pct=20,
                       match_reason="its publisher is recorded as MDPI AG")
    assert "20%" in b and "MDPI AG" in b


def test_basis_for_diamond():
    assert "free to authors" in coverage_basis("diamond", scheme="SciPost")


def test_match_reason_distinguishes_issn_from_publisher_matching():
    """An ISSN match is exact; a publisher-name match is a heuristic that can
    catch the wrong imprint. The reader should be able to tell which."""
    issn_entry = {"match_issns": ["2375-2548"]}
    pub_entry = {"match_publisher_regex": "(?i)^MDPI"}
    assert "ISSN" in override_match_reason(
        issn_entry, {"issns": ["2375-2548"], "publisher": "AAAS"})
    assert "MDPI AG" in override_match_reason(
        pub_entry, {"issns": [], "publisher": "MDPI AG"})
    assert override_match_reason(pub_entry, {"issns": [], "publisher": "Elsevier"}) is None


# ------------------------------------------- publisher resolution
from merge import resolve_publisher  # noqa: E402


def test_publisher_prefers_openalex():
    assert resolve_publisher({"publisher": "Elsevier BV"},
                             {"publisher": "Elsevier"}) == "Elsevier BV"


def test_regression_publisher_falls_back_to_doaj():
    """OpenAlex has no publisher for ~9,000 journals; the overlay matches
    discounts on publisher name, so the gap silently removed deals."""
    assert resolve_publisher({"publisher": None},
                             {"publisher": "Elsevier"}) == "Elsevier"
    assert resolve_publisher({}, {"publisher": "  Wiley  "}) == "Wiley"


def test_publisher_none_when_neither_source_knows():
    assert resolve_publisher({"publisher": None}, None) is None
    assert resolve_publisher({"publisher": ""}, {"publisher": ""}) in (None, "")


# --------------------------------------------- must-include tripwire
from validate import check_must_include  # noqa: E402

MUST = {"journals": [{"issn": "0090-5364", "title": "The Annals of Statistics"},
                     {"issn": "0028-0836", "title": "Nature"}]}


def _j(issn_l, *others):
    return {"id": issn_l, "issns": [issn_l, *others]}


def test_must_include_passes_when_all_present():
    data = {"journals": [_j("0090-5364"), _j("0028-0836", "1476-4687")]}
    assert check_must_include(data, MUST) == []


def test_regression_must_include_catches_a_missing_flagship():
    """The Institute of Mathematical Statistics was absent from the publisher
    allowlist, so all four Annals titles were missing from the site entirely —
    not shown as 'no deal', but absent, which reads as a broken tool."""
    data = {"journals": [_j("0028-0836")]}
    errors = check_must_include(data, MUST)
    assert len(errors) == 1
    assert "The Annals of Statistics" in errors[0]
    assert "inclusion rules need widening" in errors[0]


def test_must_include_matches_on_any_issn_of_the_journal():
    """A journal listed under its alternate ISSN still counts as present."""
    data = {"journals": [_j("9999-9999", "0090-5364"), _j("0028-0836")]}
    assert check_must_include(data, MUST) == []


# ------------------------------------ tolerating flaky agreement fetches
from fetch_jct import unfetchable_verdict  # noqa: E402

OXFORD_AGREEMENTS = {"els2026jisc", "wiley2026jisc", "aps2026jisc"}


def test_no_failures_is_fine():
    assert unfetchable_verdict([], OXFORD_AGREEMENTS) is None


def test_regression_one_flaky_non_oxford_fetch_does_not_abort_the_run():
    """A single Google Docs timeout aborted a 50-minute build. Over 606
    sequential fetches at least one transient failure is close to inevitable,
    and 564 of those agreements only feed an additive inclusion net."""
    assert unfetchable_verdict(["some2025consortium"], OXFORD_AGREEMENTS) is None


def test_losing_an_oxford_agreement_always_aborts():
    """Dropping one silently removes coverage from real journals."""
    problem = unfetchable_verdict(["els2026jisc"], OXFORD_AGREEMENTS)
    assert problem and "els2026jisc" in problem and "silently drop" in problem


def test_oxford_agreement_aborts_even_among_tolerable_others():
    problem = unfetchable_verdict(["a2025x", "wiley2026jisc", "b2025y"],
                                  OXFORD_AGREEMENTS)
    assert problem and "wiley2026jisc" in problem


def test_many_failures_abort_even_when_none_are_oxfords():
    """Widespread failure is a sick source, not flakiness."""
    problem = unfetchable_verdict([f"x{i}" for i in range(20)], OXFORD_AGREEMENTS)
    assert problem and "systemic" in problem


def test_regression_no_baseline_means_no_guessing():
    """Membership is only inside the agreement CSV, so a failed fetch cannot be
    classified from the index. The first failure observed in practice was
    Oxford's Thieme agreement — tolerating it would have silently dropped that
    publisher's journals."""
    problem = unfetchable_verdict(["thie2025jisc"], set(), have_baseline=False)
    assert problem and "Refusing to guess" in problem


def test_baseline_present_allows_tolerating_a_stranger():
    assert unfetchable_verdict(["some2025consortium"], OXFORD_AGREEMENTS,
                               have_baseline=True) is None


def test_tolerance_boundary_is_respected():
    assert unfetchable_verdict([f"x{i}" for i in range(15)], set(), 15) is None
    assert unfetchable_verdict([f"x{i}" for i in range(16)], set(), 15) is not None


# ------------------------------------------- remembered journal scope
import datetime as _dt  # noqa: E402

import changelog as _changelog  # noqa: E402


def _known(tmp_path, monkeypatch):
    path = tmp_path / "known_journals.tsv"
    monkeypatch.setattr(_changelog, "KNOWN", path)
    return path


def _mk(*issns):
    return [{"id": i} for i in issns]


def test_known_set_records_first_and_last_seen(tmp_path, monkeypatch):
    path = _known(tmp_path, monkeypatch)
    _changelog.update_known(_mk("0028-0836"), "2026-08-07", 365)
    rows = path.read_text().splitlines()
    assert rows[0].split("\t") == _changelog.KNOWN_COLUMNS
    assert rows[1] == "0028-0836\t2026-08-07\t2026-08-07"


def test_regression_a_journal_missing_this_run_is_still_remembered(tmp_path, monkeypatch):
    """The whole point: a source having a bad day must not silently shrink the
    site. Coverage is monotonic; the facts are still refetched every run."""
    _known(tmp_path, monkeypatch)
    _changelog.update_known(_mk("0028-0836", "0036-8075"), "2026-08-01", 365)
    total, retired = _changelog.update_known(_mk("0028-0836"), "2026-08-08", 365)
    assert total == 2 and retired == 0


def test_first_seen_is_preserved_across_runs(tmp_path, monkeypatch):
    path = _known(tmp_path, monkeypatch)
    _changelog.update_known(_mk("0028-0836"), "2026-01-01", 365)
    _changelog.update_known(_mk("0028-0836"), "2026-08-07", 365)
    row = path.read_text().splitlines()[1].split("\t")
    assert row[1] == "2026-01-01" and row[2] == "2026-08-07"


def test_long_absent_journals_are_eventually_retired(tmp_path, monkeypatch):
    """Bounded, so genuinely dead titles do not accumulate for ever."""
    _known(tmp_path, monkeypatch)
    _changelog.update_known(_mk("0028-0836", "9999-9999"), "2025-01-01", 365)
    total, retired = _changelog.update_known(_mk("0028-0836"), "2026-08-07", 365)
    assert total == 1 and retired == 1


def test_retention_boundary(tmp_path, monkeypatch):
    _known(tmp_path, monkeypatch)
    _changelog.update_known(_mk("1111-1111"), "2026-08-01", 30)
    kept, _ = _changelog.update_known([], "2026-08-30", 30)
    assert kept == 1, "exactly at the retention limit it is kept"
    gone, retired = _changelog.update_known([], "2026-09-02", 30)
    assert gone == 0 and retired == 1


# --------------------------- gold-OA discounts only for gold-OA journals
def test_regression_gold_discount_requires_the_journal_to_be_open_access():
    """The Bodleian's discounts apply to a publisher's "fully gold open access
    journals". Matching on publisher alone gave 3,637 subscription journals a
    15% discount — including Nature Protocols, where publishing is free unless
    you choose OA, so the discount implied a cost that does not exist.

    Guards the condition merge.py applies; see the integration test for the
    end-to-end behaviour.
    """
    subscription = {"is_oa": False, "is_in_doaj": False}
    gold = {"is_oa": True, "is_in_doaj": False}
    in_doaj = {"is_oa": False, "is_in_doaj": True}
    eligible = lambda r: bool(r.get("is_oa") or r.get("is_in_doaj"))  # noqa: E731
    assert not eligible(subscription)
    assert eligible(gold) and eligible(in_doaj)


def test_regression_any_fetch_failure_is_tolerated_not_just_runtimeerror():
    """A 400 from the googleusercontent host these Google Docs links redirect
    to is not retried by http_get (a 4xx is normally permanent) and surfaces as
    requests.HTTPError. Catching only RuntimeError let it escape and abort an
    hour-long run. This pins the classifier's behaviour once such a failure is
    collected, whatever its type."""
    import requests
    for exc in (RuntimeError("timeout"), requests.HTTPError("400"),
                requests.ConnectionError("reset")):
        # The verdict works on esac ids, not exception types — what matters is
        # that the caller collects every failure kind and reaches this point.
        assert unfetchable_verdict(["some2025consortium"], OXFORD_AGREEMENTS,
                                   have_baseline=True) is None
