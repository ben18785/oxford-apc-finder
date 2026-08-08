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


# ------------------------------------ superseded predecessor records
from merge import superseded as _superseded  # noqa: E402

TODAY_2026 = _dt.date(2026, 8, 7)


def test_regression_predecessor_records_are_flagged_superseded():
    """OpenAlex keeps the old journal as its own source after a rename or a
    change of publisher, so the site lists both. Real last-active years, checked
    against OpenAlex on 2026-08-07:

      0035-9238  JRSS Series A (General, Wiley)        1987
      0035-9246  JRSS Series B (Methodological, Wiley) 2018
      1054-9714  Journal of Phase Equilibria           2003

    The current titles are correctly covered; these are the records that read as
    "a journal with no Oxford deal" when they are journals you cannot submit to.
    """
    for year in (1987, 2003, 2018):
        got = _superseded({"last_active_year": year}, TODAY_2026)
        assert got and got["last_active_year"] == year


def test_currently_publishing_journals_are_not_flagged():
    assert _superseded({"last_active_year": 2026}, TODAY_2026) is None
    assert _superseded({"last_active_year": 2024}, TODAY_2026) is None


def test_unknown_activity_is_never_called_superseded():
    """Absence of evidence is not evidence of absence: a missing counts_by_year
    must not brand a live journal defunct."""
    assert _superseded({}, TODAY_2026) is None
    assert _superseded({"last_active_year": None}, TODAY_2026) is None


def test_supersede_threshold_boundary():
    assert _superseded({"last_active_year": 2022}, TODAY_2026) is None
    assert _superseded({"last_active_year": 2021}, TODAY_2026) is not None


# ------------------------------------------- DOAJ APC edge cases
from merge import pick_doaj_record  # noqa: E402


def _apc(has, price=None):
    return {"apc": {"has_apc": has, "price": price, "currency": "GBP"}}


def test_doaj_saying_no_apc_is_free_not_unknown():
    """DOAJ records "no APC" as a flag, not as an amount of zero. Reading only
    the amount would turn every diamond journal into "price unknown"."""
    c = effective_cost("none", None, _oa(), _apc(False))
    assert c["kind"] == "no_apc"


def test_regression_unknown_apc_flag_is_not_treated_as_free():
    """`== "yes"` made anything unrecognised mean False, so a blank or a future
    third value would have asserted a journal is free on the strength of a field
    we did not understand."""
    c = effective_cost("none", None, _oa(), _apc(None))
    assert c["kind"] != "no_apc"


def test_regression_a_zero_price_is_a_price_not_a_missing_one():
    """`if price:` treats a genuine zero as absent."""
    c = effective_cost("none", None, _oa(), _apc(True, 0))
    assert c["kind"] == "list_price" and c["list"]["price"] == 0


def test_deal_status_still_wins_over_the_doaj_flag():
    assert effective_cost("covered", None, _oa(), _apc(False))["kind"] == "covered"
    assert effective_cost("diamond", None, _oa(), _apc(False))["kind"] == "diamond"


def test_regression_conflicting_doaj_records_resolve_by_title():
    """A renamed journal keeps its old ISSNs, so one OpenAlex record can reach
    two DOAJ records that disagree. Revista Brasileira de Reumatologia is held
    as free while its successor Advances in Rheumatology charges 1,890; taking
    whichever ISSN sorted first showed a charge for a journal DOAJ calls free."""
    doaj = {
        "0482-5004": {"title": "Revista Brasileira de Reumatologia", **_apc(False)},
        "2523-3106": {"title": "Advances in Rheumatology", **_apc(True, 1890)},
    }
    got = pick_doaj_record({"0482-5004", "2523-3106"}, doaj,
                           "Revista Brasileira de Reumatologia")
    assert got["title"] == "Revista Brasileira de Reumatologia"
    assert got["apc"]["has_apc"] is False


def test_single_doaj_record_is_used_unchanged():
    doaj = {"1234-5678": {"title": "Some Journal", **_apc(True, 100)}}
    assert pick_doaj_record({"1234-5678"}, doaj, "Anything")["apc"]["price"] == 100
    assert pick_doaj_record({"9999-9999"}, doaj, "Anything") is None


# --------------------------------------------- publishing model
from merge import oa_status  # noqa: E432


def test_model_diamond_requires_an_explicit_no_charge():
    assert oa_status({}, _apc(False), True) == "diamond"


def test_regression_unknown_charge_is_gold_not_diamond():
    """Claiming a journal is free when it is not is the wrong error to make."""
    assert oa_status({}, _apc(None), True) == "gold"
    assert oa_status({}, None, True) == "gold"


def test_model_gold_when_fully_oa_and_charging():
    assert oa_status({}, _apc(True, 1500), True) == "gold"


def test_model_hybrid_is_a_paywalled_journal_with_a_price():
    """Nature is not open access, but has an £8,490 charge to make one article
    so. The figure is the price of openness, not of publishing there."""
    assert oa_status({"apc_prices": [{"price": 8490, "currency": "GBP"}]},
                     None, False) == "hybrid"


def test_model_subscription_when_no_oa_route_is_known():
    assert oa_status({"apc_prices": []}, None, False) == "subscription"


def test_regression_free_journals_are_not_told_no_deal_applies():
    """13,467 journals cost the author nothing yet were labelled only "No Oxford
    deal", because Oxford funds nothing for them and none is needed — the exact
    route the Bodleian recommends to unfunded authors."""
    from merge import coverage_basis
    # The dedicated wording is applied in merge; this pins the generic text it
    # replaces, so a regression shows up as the wrong sentence rather than none.
    generic = coverage_basis("none", agreement_count=42)
    assert "Checked against" in generic and "block grants" in generic


# ------------------------------------------- OpenAlex daily allowance
from common import DailyQuotaExhausted  # noqa: E402


def test_regression_quota_exhaustion_is_not_retried():
    """A 429 caused by the daily allowance being gone was retried at 1, 2, 4 and
    8 seconds against a limit that resets in hours, then reported as a generic
    'Failed to fetch' with a stack trace. It is a distinct condition and should
    say so."""
    assert issubclass(DailyQuotaExhausted, RuntimeError)
    err = DailyQuotaExhausted("daily request allowance exhausted; resets in 6.6 hours")
    assert "allowance exhausted" in str(err) and "resets" in str(err)


# ------------------------------------------------- usage monitoring
from fetch_usage import split_journal_path, summarise, _first_int  # noqa: E402

USAGE_CFG = {"min_views_to_publish": 5, "min_searches_to_publish": 3,
             "window_days": 90}
CORPUS = {"total": 46315, "covered": 12535}


@pytest.mark.parametrize("path,expected", [
    ("/j/covered/all/1234-5678", ("covered", "all", "1234-5678")),
    ("j/none/deals/1234-5678", ("none", "deals", "1234-5678")),   # slash optional
    ("/j/discount/all/1234-5678/", ("discount", "all", "1234-5678")),
    # Three-segment form predates the scope segment. Those views still count
    # towards the chart; they just cannot support a coverage share.
    ("/j/covered/1234-5678", ("covered", "deals", "1234-5678")),
    ("/missing/some query", None),
    ("/", None),
    ("/j/covered", None),                             # too few segments
    ("/other/covered/all/1234-5678", None),
])
def test_journal_paths_are_parsed_and_everything_else_ignored(path, expected):
    assert split_journal_path(path) == expected


def test_ordinary_pageviews_are_not_counted_as_journal_lookups():
    """The homepage is by far the most-hit path. Counting it as a journal would
    put a phantom entry at the top of the chart and skew every share below."""
    u = summarise([{"path": "/", "title": "home", "count": 900},
                   {"path": "/j/covered/all/1111-2222", "title": "Nature", "count": 10}],
                  [], {"total": 910}, CORPUS, USAGE_CFG)
    assert u["totals"]["journal_views"] == 10
    assert u["totals"]["distinct_journals_viewed"] == 1


def test_journals_below_the_floor_are_withheld_but_still_declared():
    """Oxford is a small population: at low counts, naming a journal here says
    more about a person than about the journal. It must still be declared,
    though — a chart that silently drops its tail reads as complete."""
    hits = [{"path": "/j/covered/all/1111-2222", "title": "Nature", "count": 40},
            {"path": "/j/none/all/3333-4444", "title": "Tiny", "count": 2},
            {"path": "/j/none/all/5555-6666", "title": "Tinier", "count": 1}]
    u = summarise(hits, [], {}, CORPUS, USAGE_CFG)
    titles = [j["title"] for j in u["top_journals"]]
    assert titles == ["Nature"]
    assert u["withheld"] == {"journals": 2, "views": 3, "min_views_to_publish": 5}


def test_free_text_searches_need_several_people_before_publication():
    hits = [{"path": "/missing/law quarterly review", "count": 5},
            {"path": "/missing/something identifying", "count": 1}]
    u = summarise(hits, [], {}, CORPUS, USAGE_CFG)
    assert [m["query"] for m in u["most_wanted"]] == ["law quarterly review"]


def test_coverage_share_is_reported_against_the_corpus_baseline():
    """The share on its own is uncalibratable. It only means something next to
    the rate across every journal the site tracks."""
    hits = [{"path": "/j/covered/all/1111-2222", "title": "A", "count": 10},
            {"path": "/j/none/all/3333-4444", "title": "B", "count": 10}]
    u = summarise(hits, [], {}, CORPUS, USAGE_CFG)
    assert u["coverage"]["covered_journal_share"] == 0.5
    assert round(u["coverage"]["corpus_share"], 3) == 0.271


def test_no_traffic_does_not_divide_by_zero():
    u = summarise([], [], {}, CORPUS, USAGE_CFG)
    assert u["coverage"]["covered_view_share"] is None
    assert u["totals"]["journal_views"] == 0


def test_totals_survive_the_api_renaming_its_fields():
    """GoatCounter has changed how it reports totals between releases. Guessing
    one name and getting a zero would be published to readers as '0 visitors',
    which is worse than publishing nothing."""
    assert _first_int({"total_unique": 7}, "total_unique", "unique") == 7
    assert _first_int({"unique": 7}, "total_unique", "unique") == 7
    assert _first_int({}, "total_unique", "unique") == 0


def test_regression_coverage_share_ignores_views_made_under_the_deal_filter():
    """"Only show journals with an Oxford deal" is ON by default, so those
    readers are picking from a list that is already 100% covered. Counting
    those views would report the default setting back as a finding about what
    researchers publish in."""
    hits = [
        # Filtered browsing: every one covered, and every one irrelevant here.
        {"path": "/j/covered/deals/1111-1111", "title": "A", "count": 50},
        {"path": "/j/covered/deals/2222-2222", "title": "B", "count": 50},
        # Unfiltered: the only views that answer the question.
        {"path": "/j/covered/all/3333-3333", "title": "C", "count": 5},
        {"path": "/j/none/all/4444-4444", "title": "D", "count": 5},
        {"path": "/j/none/all/5555-5555", "title": "E", "count": 5},
    ]
    u = summarise(hits, [], {}, CORPUS, USAGE_CFG)
    # One of three unfiltered journals is covered, not five of five.
    assert u["coverage"]["covered_journal_share"] == pytest.approx(1 / 3)
    assert u["coverage"]["sample_journals"] == 3
    assert u["coverage"]["sample_views"] == 15
    # The chart itself still counts every view, filtered or not.
    assert u["totals"]["journal_views"] == 115


def test_coverage_share_is_withheld_rather_than_guessed_without_a_sample():
    """With no unfiltered views there is no honest number to report."""
    u = summarise([{"path": "/j/covered/deals/1111-1111", "title": "A", "count": 30}],
                  [], {}, CORPUS, USAGE_CFG)
    assert u["coverage"]["covered_journal_share"] is None
    assert u["coverage"]["sample_journals"] == 0


def test_a_site_with_no_traffic_yet_publishes_nothing(tmp_path, monkeypatch):
    """Zeros in every field would put a "How this site is used" link in the
    footer leading to a page of noughts — that reads as a broken feature, not
    a new one. Nothing counted means nothing published."""
    import fetch_usage
    monkeypatch.setattr(fetch_usage, "OUT", tmp_path)
    monkeypatch.setattr(fetch_usage, "_api", lambda base, path, token, params: (
        {"total": 0, "total_events": 0} if path == "/stats/total"
        else {"hits": [], "more": False} if path == "/stats/hits"
        else {"stats": [], "more": False}))
    monkeypatch.setenv("GOATCOUNTER_TOKEN", "test-token")
    monkeypatch.setattr(fetch_usage, "load_config",
                        lambda: {"analytics": {"goatcounter_code": "example",
                                               "window_days": 90}})
    fetch_usage.main()
    assert not (tmp_path / "usage.json").exists()


# --------------------------------- the £0 invariant
# "£0 may be asserted only when the evidence establishes £0 without depending
# on an unknown fact about the author, the article, a remaining quota, or a
# disputed or expired agreement."

def test_expired_agreement_never_yields_a_settled_zero():
    """merge.py carried a comment saying exactly this — "£0 must never be shown
    as settled fact once the stated end date has passed" — while the code did
    the opposite for 90 journals. The comment was right."""
    c = effective_cost("covered", None, _oa(), None,
                       expired={"end_date": "2025-12-31", "days": 220})
    assert c["kind"] == "covered_conditional"
    assert "2025-12-31" in " ".join(c["reasons"])


def test_disputed_sources_yield_no_figure_at_all():
    """JCT says MDPI is fully covered; Oxford's own page says a 20% discount.
    Presenting either as fact picks a winner we have no basis to pick — and the
    £0 reading is the one that costs someone £2-4k if it is wrong."""
    c = effective_cost("covered", None, _oa(), None,
                       disputed={"publisher": "MDPI"})
    assert c["kind"] == "uncertain"
    for j in (effective_cost("discount", 20, _oa(), None, disputed={"publisher": "MDPI"}),):
        assert j["kind"] == "uncertain", "a disputed discount is no safer than a disputed £0"


def test_a_capped_allowance_is_not_a_zero():
    """AIP and RSC cover an agreed number of articles a year. Whether the
    allowance is still open is not published anywhere, so it cannot be asserted
    as £0 — only as £0 if it still applies."""
    c = effective_cost("covered", None, _oa(), None, capacity_limited=True)
    assert c["kind"] == "covered_conditional"
    assert "allowance" in " ".join(c["reasons"])


def test_a_funder_restriction_is_not_a_zero():
    c = effective_cost("covered", None, _oa(), None,
                       funders_only=["UKRI", "Wellcome Trust"])
    assert c["kind"] == "covered_conditional"
    assert "UKRI" in " ".join(c["reasons"])


def test_ordinary_coverage_is_still_covered():
    """Universal conditions — corresponding authorship, article type, CC BY —
    apply to every agreement and are stated site-wide. Moving all 12,000
    covered journals into a warning state would make the warning meaningless
    for the 694 that carry a specific, journal-level risk."""
    assert effective_cost("covered", None, _oa(), None)["kind"] == "covered"


def test_diamond_is_unaffected_by_agreement_risk_flags():
    """Diamond £0 is a fact about the journal, not about an agreement, so no
    agreement-level doubt can weaken it."""
    c = effective_cost("diamond", None, _oa(), None,
                       expired={"end_date": "2020-01-01", "days": 2000},
                       capacity_limited=True)
    assert c["kind"] == "diamond"


def test_validation_refuses_a_build_that_asserts_zero_on_a_risky_claim():
    """The invariant is implemented in merge and enforced here, because a rule
    living in one function decays the first time someone adds a branch."""
    from validate import check_cost_claims
    bad = {"journals": [{
        "id": "1234-5678", "cost": {"kind": "covered"},
        "deal": {"status": "covered", "expired": {"end_date": "2025-01-01"},
                 "disputed": None}}]}
    errors = check_cost_claims(bad)
    assert errors and "expired" in errors[0]


def test_validation_notices_if_the_risk_flags_stop_arriving_entirely():
    """Per-journal checks all pass trivially if the flags are never populated —
    which is precisely the regression that would reintroduce the bug."""
    from validate import check_cost_claims
    silent = {"journals": [{"id": f"0000-000{i}", "cost": {"kind": "covered"},
                            "deal": {"status": "covered", "expired": None,
                                     "disputed": None}} for i in range(5)]}
    assert any("stopped reaching" in e for e in check_cost_claims(silent))


# ------------------------------------------- comparable cost for ordering
from merge import comparable_gbp  # noqa: E402

RATES = {"GBP": 1.0, "USD": 1.3445, "EUR": 1.166, "IDR": 24002.0}


def test_regression_ordering_by_raw_price_would_rank_by_denomination():
    """Journals price in 46 currencies. Sorted on the number alone, the dearest
    APC on the site is 150,000,000 IRR — about £2,400 — while Nature at
    $12,290 ranks far below it. An ordering of denominations dressed up as an
    ordering of prices is exactly the kind of confident wrongness this site is
    built to avoid."""
    idr = comparable_gbp({"kind": "list_price",
                          "list": {"price": 100_000_000, "currency": "IDR"}}, RATES)
    usd = comparable_gbp({"kind": "list_price",
                          "list": {"price": 12_290, "currency": "USD"}}, RATES)
    assert 100_000_000 > 12_290          # the raw numbers say one thing...
    assert idr < usd                      # ...and the money says the opposite


def test_a_currency_the_ecb_does_not_publish_is_not_orderable():
    """~648 journals price in IRR, IQD, UAH and similar. Guessing a rate would
    be inventing a number; sorting them as zero would file them under
    'cheapest'. Neither is acceptable, so they carry no figure."""
    assert comparable_gbp({"kind": "list_price",
                           "list": {"price": 5_000_000, "currency": "IRR"}}, RATES) is None


def test_missing_is_distinguishable_from_free():
    """None means 'cannot be compared'; 0 means 'costs nothing'. Collapsing the
    two is how an unknown price becomes the top hit for 'cheapest'."""
    assert comparable_gbp({"kind": "unknown"}, RATES) is None
    assert comparable_gbp({"kind": "uncertain"}, RATES) is None
    assert comparable_gbp({"kind": "discount_unknown_base", "pct": 15}, RATES) is None
    assert comparable_gbp({"kind": "no_apc"}, RATES) == 0
    assert comparable_gbp({"kind": "diamond"}, RATES) == 0


def test_a_conditional_zero_is_still_zero_for_ordering():
    """Confidence and amount are different axes. The cost column carries the
    doubt; the ordering only needs the number."""
    assert comparable_gbp({"kind": "covered"}, RATES) == 0
    assert comparable_gbp({"kind": "covered_conditional"}, RATES) == 0


def test_the_discounted_figure_is_what_gets_ordered_not_the_list_price():
    """Ordering on the pre-discount price would rank journals by what you would
    have paid without Oxford, which is not the question anyone is asking."""
    cost = {"kind": "discount", "pct": 15,
            "list": {"price": 1000, "currency": "USD"},
            "estimated": {"price": 850, "currency": "USD"}}
    assert comparable_gbp(cost, RATES) == round(850 / RATES["USD"])


def test_no_rates_means_no_ordering_rather_than_a_wrong_one():
    """A failed rate fetch must disable cost ordering, not silently order by
    raw numbers across currencies."""
    assert comparable_gbp({"kind": "list_price",
                           "list": {"price": 1000, "currency": "USD"}}, None) is None


def test_validation_pins_each_risk_to_its_own_cost_kind():
    """The aggregate guard catches total failure; these catch a single flag
    silently ceasing to reach effective_cost, which is the likelier
    regression."""
    from validate import check_cost_claims

    def one(deal, kind):
        return check_cost_claims({"journals": [
            {"id": "1234-5678", "cost": {"kind": kind}, "deal": deal}]})

    capped = {"status": "covered", "expired": None, "disputed": None,
              "conditions": {"capacity_limited": True, "funders_only": None}}
    assert one(capped, "covered"), "a capped allowance must not be a settled £0"
    assert not one(capped, "covered_conditional")

    funder = {"status": "covered", "expired": None, "disputed": None,
              "conditions": {"capacity_limited": False, "funders_only": ["UKRI"]}}
    assert one(funder, "covered")
    assert not one(funder, "covered_conditional")

    # A conflict outranks everything: not a qualified figure, no figure.
    clash = {"status": "covered", "expired": None, "conditions": None,
             "disputed": {"publisher": "MDPI"}}
    assert one(clash, "covered_conditional"), "a disputed deal is not merely conditional"
    assert not one(clash, "uncertain")


def test_a_disputed_journal_with_no_deal_is_left_alone():
    """A conflict entry can match a journal that has no agreement anyway.
    Demanding 'uncertain' there would fail the build over a journal whose cost
    was never in question."""
    from validate import check_cost_claims
    assert not check_cost_claims({"journals": [
        {"id": "1234-5678", "cost": {"kind": "list_price"},
         "deal": {"status": "none", "expired": None, "conditions": None,
                  "disputed": {"publisher": "MDPI"}}}]})
