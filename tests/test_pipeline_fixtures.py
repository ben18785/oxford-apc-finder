"""End-to-end pipeline run on fixtures, plus whole-dataset invariants.

This is the aggregate tier: rather than checking one function, it runs
merge → validate → changelog → build_site over a fixture set chosen to hit
every branch, then asserts properties that must hold for the *whole* output.

It runs in a copy of the repo under tmp_path. Running in place would overwrite
data/out/deals.json, which takes ~50 minutes of live fetching to rebuild.

Offline: APC_FIXTURES=1 makes the fetch stages read data/fixtures/ and makes
validate skip the live JCT oracle.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "golden" / "oxford_verdicts.json"
# The day the fixture build is taken to run on. Fixed, because whether an
# agreement has expired depends on the date and the golden file pins the answer.
BUILD_DATE = "2026-10-13"
ISSN_RX = re.compile(r"^\d{4}-\d{3}[\dX]$")


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> dict:
    """Run the full offline pipeline once; return the built artefacts."""
    work = tmp_path_factory.mktemp("repo")
    for item in ("pipeline", "site", "config.yaml"):
        src = ROOT / item
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, work / item)
    for sub in ("fixtures", "curated"):
        shutil.copytree(ROOT / "data" / sub, work / "data" / sub)
    # APC_FIXTURES=1 writes to data/out-fixtures/, never data/out/, so a
    # demo build cannot overwrite the real dataset or the committed
    # last_counts.json baseline. See pipeline/common.py.
    (work / "data" / "out-fixtures").mkdir(parents=True, exist_ok=True)

    # Copy the committed baselines too. Without them this test ran against a
    # repo state that never exists in practice, and so missed that a fixtures
    # build was being compared against real-dataset counts — which failed in
    # CI, where the whole repo is checked out.
    for baseline in (Path("data/out-fixtures/last_counts.json"),
                     Path("data/state/journal_state.tsv")):
        if (ROOT / baseline).exists():
            (work / baseline).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / baseline, work / baseline)

    env = {**os.environ, "APC_FIXTURES": "1", "APC_TODAY": BUILD_DATE}
    env.pop("OPENALEX_API_KEY", None)
    result = subprocess.run(
        [sys.executable, str(work / "pipeline" / "run_all.py")],
        cwd=work, env=env, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"pipeline failed:\nSTDOUT\n{result.stdout}\nSTDERR\n{result.stderr}")

    read = lambda p: json.loads((work / p).read_text())  # noqa: E731
    return {
        "work": work,
        "stdout": result.stdout,
        "journals": read("data/out-fixtures/journals.json"),
        "index": read("_site/data/index.json"),
        "keywords": read("_site/data/keywords.json"),
        "status": read("_site/data/status.json"),
        "config": read("_site/config.json"),
    }


# ------------------------------------------------------------ it runs at all
def test_pipeline_completes(built):
    assert "Pipeline complete" in built["stdout"]


def test_every_stage_ran(built):
    for stage in ("merge.py", "validate.py", "changelog.py", "build_site.py"):
        assert stage in built["stdout"]


def test_validation_passed(built):
    assert "Validation passed" in built["stdout"]


# ------------------------------------------------- inclusion / exclusion
def test_misconduct_withdrawn_journal_is_excluded(built):
    """The anti-predatory filter. This silently matched nothing for the whole
    life of the project because the DOAJ changelog header was misparsed, so it
    gets an explicit end-to-end test."""
    titles = [j["title"] for j in built["journals"]["journals"]]
    assert not any("Withdrawn For Misconduct" in t for t in titles)
    assert built["journals"]["counts"]["excluded_misconduct"] >= 1


def test_all_deal_branches_present(built):
    """The fixture set is built to exercise each branch; if one disappears the
    fixtures have drifted and the suite has stopped testing that path."""
    statuses = {j["deal"]["status"] for j in built["journals"]["journals"]}
    assert {"covered", "discount", "diamond", "none"} <= statuses

    kinds = {j["cost"]["kind"] for j in built["journals"]["journals"]}
    assert {"covered", "discount", "list_price", "unknown"} <= kinds


# ------------------------------------------------------------- invariants
def test_every_journal_has_a_wellformed_id_and_issns(built):
    for j in built["journals"]["journals"]:
        assert ISSN_RX.match(j["id"]), f"bad id {j['id']}"
        for issn in j["issns"]:
            assert ISSN_RX.match(issn), f"{j['id']}: bad issn {issn}"


def test_no_duplicate_journals(built):
    ids = [j["id"] for j in built["journals"]["journals"]]
    assert len(ids) == len(set(ids))


def test_covered_journals_carry_deal_provenance(built):
    """A 'free to you' claim must always be traceable to the agreement data."""
    for j in built["journals"]["journals"]:
        if j["deal"]["status"] == "covered":
            assert j["provenance"].get("deal"), f"{j['id']} covered without a source"


def test_covered_journals_state_the_universal_criteria(built):
    """The Bodleian's blanket conditions (corresponding author, ox.ac.uk
    address, CC BY) apply to every deal and must reach every covered journal."""
    for j in built["journals"]["journals"]:
        if j["deal"]["status"] == "covered":
            blob = " ".join(j["deal"]["caveats"]).lower()
            assert "corresponding author" in blob
            assert "ox.ac.uk" in blob


def test_every_fact_links_somewhere_reachable(built):
    for j in built["journals"]["journals"]:
        for entry in j["provenance"].values():
            for item in (entry if isinstance(entry, list) else [entry]):
                assert item["url"].startswith("http"), f"{j['id']}: {item['url']}"


def test_costs_never_show_an_estimate_without_a_list_price(built):
    for j in built["journals"]["journals"]:
        cost = j["cost"]
        if "estimated" in cost:
            assert cost.get("list"), f"{j['id']} estimated a price from nothing"
            assert cost["estimated"]["currency"] == cost["list"]["currency"]


def test_discount_percentages_are_sane(built):
    for j in built["journals"]["journals"]:
        pct = j["deal"].get("discount_pct")
        if pct is not None:
            assert 0 < pct < 100, f"{j['id']}: implausible discount {pct}"


def test_disputed_journals_explain_themselves(built):
    """A warning with no explanation is worse than no warning."""
    disputed = [j for j in built["journals"]["journals"] if j["deal"].get("disputed")]
    assert disputed, "fixture set should contain a disputed journal"
    for j in disputed:
        d = j["deal"]["disputed"]
        assert d["note"], f"{j['id']} disputed with no explanation"
        # Both sides must be named. For a curated conflict the other side is
        # the Bodleian page; for one the JCT API found, both sides are JCT and
        # the counter-claim is api_says. A warning that quotes only one source
        # gives the reader nothing to act on either way.
        assert d.get("jct_says"), f"{j['id']} does not say what JCT claims"
        assert d.get("bodleian_says") or d.get("api_says"), \
            f"{j['id']} names no counter-claim"


def test_counts_match_the_journal_list(built):
    js = built["journals"]["journals"]
    counts = built["journals"]["counts"]
    assert counts["total"] == len(js)
    assert counts["covered"] == sum(1 for j in js if j["deal"]["status"] == "covered")
    assert counts["disputed"] == sum(1 for j in js if j["deal"].get("disputed"))


# ------------------------------------------------------------- built site
def test_index_and_keywords_stay_aligned(built):
    """keywords.json is a parallel array; a length mismatch silently attaches
    one journal's subjects to another."""
    assert len(built["keywords"]["ids"]) == len(built["index"]["journals"])


def test_keyword_ids_are_within_the_vocabulary(built):
    vocab_size = len(built["keywords"]["vocab"])
    for ids in built["keywords"]["ids"]:
        for i in ids:
            assert 0 <= i < vocab_size


def test_every_indexed_journal_has_a_detail_record(built):
    """The browser fetches data/details/<shard>.json to open a journal; a
    missing shard is a dead click.

    Uses the shard length the build published rather than a literal, so this
    tests the contract the client actually follows.
    """
    length = built["config"]["shard_key_length"]
    details = built["work"] / "_site" / "data" / "details"
    for rec in built["index"]["journals"]:
        shard = details / f"{rec['id'][:length]}.json"
        assert shard.exists(), f"no shard for {rec['id']}"
        assert rec["id"] in json.loads(shard.read_text())


def test_regression_build_publishes_the_shard_length_it_used(built):
    """app.js derives the detail path from config.shard_key_length. When the
    build moved from 2-character to 4-character shards and the client kept
    asking for 2, every journal click 404'd and did nothing — silently.

    The client side is covered by tests/frontend/search.test.js; this pins the
    build side of the same contract, cheaply and without a JS engine.
    """
    length = built["config"].get("shard_key_length")
    assert length, "config.json must publish shard_key_length for the client"

    names = [p.stem for p in
             (built["work"] / "_site" / "data" / "details").glob("*.json")]
    assert names, "no detail shards were written"
    bad = sorted({n for n in names if len(n) != length})
    assert not bad, (
        f"config says shard_key_length={length} but shards are named {bad[:5]} "
        "— the client would request a path that does not exist")


def test_index_is_sorted_by_title(built):
    """The site relies on this instead of sorting 43k records client-side."""
    titles = [(r["t"] or "").lower() for r in built["index"]["journals"]]
    assert titles == sorted(titles)


def test_index_carries_no_keyword_blob(built):
    """Keywords are ~75% of the index; leaving them in triples the eager load."""
    assert all("k" not in r for r in built["index"]["journals"])


def test_config_points_at_a_real_repo(built):
    assert "/" in built["config"]["github_repo"]
    assert "YOUR_GITHUB_USERNAME" not in built["config"]["github_repo"]


def test_status_reports_source_freshness(built):
    assert built["status"]["counts"]["total"] == built["journals"]["counts"]["total"]


# -------------------------------------------------------------- changelog
def test_changelog_writes_a_baseline_on_first_run(built):
    state = built["work"] / "data" / "state" / "journal_state.fixtures.tsv"
    assert state.exists()
    lines = state.read_text().splitlines()
    assert len(lines) == len(built["journals"]["journals"]) + 1   # + header


def test_changelog_detects_a_deal_disappearing(built, tmp_path):
    """Simulate next week's run where a journal loses its deal, and check the
    diff reports it — this is the early-warning signal for a broken source."""
    work = built["work"]
    state = work / "data" / "state" / "journal_state.fixtures.tsv"

    rows = state.read_text().splitlines()
    header, body = rows[0], rows[1:]
    # Rewrite one covered journal as uncovered, and drop another entirely.
    patched, dropped = [], None
    for line in body:
        cols = line.split("\t")
        if cols[2] == "covered" and dropped is None:
            dropped = cols[0]
            continue
        patched.append(line)
    state.write_text("\n".join([header] + patched) + "\n")

    result = subprocess.run(
        [sys.executable, str(work / "pipeline" / "changelog.py")],
        cwd=work, env={**os.environ, "APC_FIXTURES": "1"},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    changes = json.loads((work / "data" / "out-fixtures" / "changes.json").read_text())
    assert changes["summary"]["added"] == 1
    assert dropped in [a["issn_l"] for a in changes["added"]]
    assert (work / "CHANGELOG-data.fixtures.md").exists()


def test_regression_fixture_build_ignores_the_real_baseline(built):
    """A fixtures build has ~12 journals; the committed baseline describes
    ~43,000. Comparing them tripped the week-on-week drop threshold and failed
    every offline run in CI (the test suite missed it by not copying the
    baseline into its work dir)."""
    assert (built["work"] / "data" / "out-fixtures" / "last_counts.json").exists(), \
        "test must run against a repo that has the real baseline"
    assert "threshold check skipped" in built["stdout"]
    assert "VALIDATION FAILED" not in built["stdout"]


def test_regression_fixture_build_does_not_touch_the_real_state_file(built):
    """changelog.py must not overwrite the committed baseline with fixture
    data — that would destroy the history the file exists to preserve."""
    real = built["work"] / "data" / "state" / "journal_state.tsv"
    if real.exists():
        assert real.read_text() == (ROOT / "data/state/journal_state.tsv").read_text()
    assert (built["work"] / "data" / "state" / "journal_state.fixtures.tsv").exists()


def test_every_journal_explains_its_verdict(built):
    """"Covered" used to reduce to "a spreadsheet says so", and "no deal" was
    indistinguishable from "we hold no data". Every journal now carries one
    plain sentence saying why it got the answer it did."""
    for j in built["journals"]["journals"]:
        basis = j["deal"].get("basis")
        assert basis and len(basis) > 40, f"{j['id']}: no usable basis"


def test_covered_journals_name_the_agreement_that_covers_them(built):
    """So a reader who disagrees has something specific to dispute."""
    for j in built["journals"]["journals"]:
        if j["deal"]["status"] == "covered":
            if (j["deal"].get("conditions") or {}).get("unlisted_agreement"):
                # No identifier exists: the source is the Bodleian's page.
                assert "Bodleian's publisher deals page" in j["deal"]["basis"]
                continue
            assert j["deal"]["esac_id"] in j["deal"]["basis"]


def test_undealt_journals_say_they_were_checked(built):
    """The distinction that matters: searched and genuinely absent, versus
    unknown to the tool. Each recognised form of "no deal" must explain itself.
    """
    # The explanations a journal with no deal may legitimately carry.
    forms = (
        "Checked against",        # no rule matched at all
        "arrangement with",       # a green/note scheme applies
        "Not on the",             # publisher has an agreement, this title is not in it
        "No Oxford deal is needed",   # diamond: the journal charges nothing
    )
    for j in built["journals"]["journals"]:
        if j["deal"]["status"] == "none":
            basis = j["deal"]["basis"]
            assert any(f in basis for f in forms), f"{j['id']}: unexplained — {basis[:80]}"


def test_every_journal_offers_a_way_to_browse_its_articles(built):
    """Every journal has an OpenAlex source id, so every journal gets at least
    one route to its own output — no API calls needed to provide it."""
    for j in built["journals"]["journals"]:
        browse = j.get("browse") or []
        assert browse, f"{j['id']}: nothing to browse"
        for link in browse:
            assert link["url"].startswith("http"), f"{j['id']}: {link['url']}"
            assert link["label"]


def test_doaj_journals_link_to_their_doaj_page(built):
    """DOAJ's journal page lists recent articles, which is the closest thing to
    'a recent issue' that costs nothing to provide."""
    for j in built["journals"]["journals"]:
        if j["in_doaj"] and (j["provenance"].get("doaj") or {}).get("url"):
            labels = " ".join(b["label"] for b in j["browse"])
            assert "DOAJ" in labels, f"{j['id']} is in DOAJ but does not link there"


def test_regression_publisher_falls_back_to_doaj(built):
    """OpenAlex leaves host_organization_name empty for ~9,000 journals. Since
    the overlay matches discounts on publisher name, that gap turned one of the
    four Lancet Regional Health titles into "no Oxford deal" while its three
    identical siblings kept theirs."""
    meta = json.loads((built["work"] / "data/out-fixtures/metadata.json").read_text())
    doaj = meta["doaj"]

    recovered = 0
    for j in built["journals"]["journals"]:
        oa_pub = (meta["openalex"].get(j["id"]) or {}).get("publisher")
        if oa_pub:
            continue
        doaj_pub = next((doaj[i].get("publisher") for i in j["issns"]
                         if i in doaj and doaj[i].get("publisher")), None)
        if doaj_pub:
            # Where DOAJ knows, it must be used.
            assert j["publisher"] == doaj_pub, (
                f"{j['id']}: DOAJ says {doaj_pub!r}, record says {j['publisher']!r}")
            recovered += 1
        # Where neither source knows, unknown is the honest answer.
    assert recovered, "fixtures must include a journal whose publisher comes from DOAJ"


def test_regression_sibling_journals_resolve_consistently(built):
    """Journals from the same publisher must not get different verdicts because
    one source happens to have a field the other lacks."""
    from collections import defaultdict
    by_pub = defaultdict(set)
    for j in built["journals"]["journals"]:
        if j["publisher"]:
            by_pub[j["publisher"].lower()].add(j["deal"]["status"])
    # Not asserting uniformity — a publisher legitimately has both covered
    # (hybrid) and discount (gold) titles. Asserting the weaker, real property:
    # no journal is left publisher-less and therefore unmatchable.
    assert all(j["publisher"] or not j["in_doaj"]
               for j in built["journals"]["journals"]), \
        "a DOAJ journal with no resolved publisher cannot match any overlay rule"


def test_regression_no_gold_discount_on_subscription_journals(built):
    """A publisher-level gold-OA discount must never attach to a journal that is
    not open access. Nature Protocols was shown a "15% Oxford discount" while
    being a subscription title with no APC at all."""
    meta = json.loads((built["work"] / "data/out-fixtures/metadata.json").read_text())
    for j in built["journals"]["journals"]:
        basis = j["deal"].get("basis") or ""
        if "Oxford has a" in basis and "% discount on that publisher" in basis:
            rec = meta["openalex"].get(j["id"]) or {}
            assert rec.get("is_oa") or rec.get("is_in_doaj") or j["in_doaj"], (
                f"{j['id']} {j['title']}: given a gold-OA discount but is not "
                "an open access journal")


def test_regression_fixture_build_never_writes_to_the_real_output_directory(built):
    """`{"total": 14}` was committed to main from a local demo build, because a
    fixtures run wrote data/out/last_counts.json — the *committed* baseline the
    week-on-week drop check compares against. Until it was spotted, that check
    could not have caught a collapse in coverage: 46,000 measured against 14
    reads as an increase, not a drop.

    The state files were already routed away for exactly this reason; the
    output directory was not."""
    real_out = built["work"] / "data" / "out"
    assert not real_out.exists() or not any(real_out.iterdir()), \
        f"a fixtures build wrote to data/out/: {[p.name for p in real_out.iterdir()]}"
    assert (built["work"] / "data" / "out-fixtures" / "journals.json").exists()


# ------------------------------------------------ the £0 invariant, end to end
# The unit tests show effective_cost() behaves when *handed* a risk flag. These
# show the flag survives the whole journey: curated YAML -> esac prefix match ->
# merge -> deal.conditions -> cost kind -> journals.json -> the search index.
# Propagation is the step that has actually broken here before.

def _by_title(built, fragment):
    for j in built["journals"]["journals"]:
        if fragment.lower() in (j["title"] or "").lower():
            return j
    raise AssertionError(f"no fixture journal matching {fragment!r}")


def test_the_offline_build_produces_every_weakened_coverage_state(built):
    """If the fixture set stops containing these, the safety states are exercised
    only by unit tests on hand-built dicts, and the pipeline that assembles them
    goes unchecked."""
    kinds = {j["cost"]["kind"] for j in built["journals"]["journals"]}
    assert "covered_conditional" in kinds
    assert "uncertain" in kinds


def test_a_capped_allowance_survives_the_whole_pipeline(built):
    """capacity_limited is set in oxford_overrides.yaml, matched by esac prefix
    in merge, and must reach both the record and the cost kind."""
    j = _by_title(built, "Capped Allowance")
    assert j["deal"]["conditions"]["capacity_limited"] is True
    assert j["cost"]["kind"] == "covered_conditional"
    assert "allowance" in " ".join(j["cost"]["reasons"])


def test_a_conflict_outranks_a_funder_restriction(built):
    """This fixture rides the BMJ agreement, which now carries BOTH a funder
    restriction and a source conflict — JCT's agreement file lists Oxford as a
    participant while JCT's own API reports no coverage for any of its 36
    journals.

    The conflict must win. "£0 if eligible, but confirm your funder" still
    asserts there is a deal; when we cannot establish that there is one at all,
    the honest answer is no figure. merge.effective_cost encodes that
    precedence and this pins it end to end. (funders_only on its own is covered
    by test_a_funder_restriction_is_not_a_zero in the unit suite.)"""
    j = _by_title(built, "Restricted By Funder")
    assert j["deal"]["conditions"]["funders_only"], "the funder flag still reaches the record"
    assert j["deal"]["disputed"], "and so does the conflict"
    assert j["cost"]["kind"] == "uncertain"


def test_an_expired_agreement_survives_the_whole_pipeline(built):
    j = _by_title(built, "Expired Agreement")
    assert j["deal"]["expired"]["end_date"] == "2020-06-30"
    assert j["cost"]["kind"] == "covered_conditional"


def test_no_risky_journal_reaches_the_site_claiming_a_settled_zero(built):
    """The negative guarantee, asserted against the built artefacts rather than
    the logic that produced them. validate.py checks journals.json; this also
    checks the search index, because the index is what the browser reads and a
    change to build_site.py could reintroduce the over-claim there alone."""
    risky_ids = set()
    for j in built["journals"]["journals"]:
        cond = j["deal"].get("conditions") or {}
        if (j["deal"].get("expired") or j["deal"].get("disputed")
                or cond.get("capacity_limited") or cond.get("funders_only")):
            risky_ids.add(j["id"])
            assert j["cost"]["kind"] != "covered", \
                f"{j['title']} asserts a settled £0 despite a known risk"
    assert risky_ids, "no risky fixtures — this test would pass vacuously"

    for rec in built["index"]["journals"]:
        if rec["id"] in risky_ids:
            # The index carries the rendered summary string, which is what the
            # ledger prints. It must not read as an unqualified zero.
            assert not rec["c"].startswith("£0 if eligible — covered"), \
                f"{rec['t']} renders as ordinary coverage despite a known risk"
            assert rec["c"].startswith("£0 if eligible — but confirm") \
                or rec["c"].startswith("Not confirmed"), rec["c"]


def test_an_agreement_the_jct_api_disowns_states_no_price(built):
    """The end-to-end version of the BMJ and Thieme case.

    JCT's agreement file lists Oxford as a current participant; JCT's own live
    API reports no coverage. The pipeline must publish the journal — removing
    it would hide the disagreement — while stating no price and showing both
    claims. Getting this wrong tells someone publishing is free when nobody
    can establish that it is.
    """
    j = next(x for x in built["journals"]["journals"] if x["id"] == "9990-0004")
    assert j["cost"]["kind"] == "uncertain", \
        f"a contradicted agreement still quotes {j['cost']['kind']}"
    assert "gbp" not in j["cost"], "an unresolvable claim was given a sortable price"
    d = j["deal"]["disputed"]
    assert d["source"] == "jct_api_cross_check"
    assert d["jct_says"] and d["api_says"], "only one side of the disagreement is shown"
    # The reader is told what to do about it, not merely that something is odd.
    assert "open access team" in d["note"]


def test_agreements_the_api_confirms_are_left_alone(built):
    """The cross-check must not be a blunt instrument: 40 of 42 real agreements
    check out, and downgrading those would make the site useless."""
    covered = [x for x in built["journals"]["journals"]
               if (x.get("deal") or {}).get("esac_id") == "wiley2026jisc"]
    assert covered, "fixture drift: no wiley journals left"
    assert all(x["cost"]["kind"] == "covered" for x in covered)


def test_a_correction_replaces_the_agreement_with_what_oxford_actually_holds(built):
    """MDPI, end to end.

    JCT lists a read-and-publish agreement; Oxford's open access team confirmed
    on 2026-08-10 that what Oxford holds is a 20% discount. The journal must
    come out as a discount — not as covered (which would promise £0 the library
    says is not on offer) and not as uncertain (which would keep hedging about
    a question that has been answered).
    """
    j = next(x for x in built["journals"]["journals"] if x["id"] == "0017-0305")
    assert j["deal"]["status"] == "discount", j["deal"]["status"]
    assert j["deal"]["discount_pct"] == 20
    assert j["deal"]["esac_id"] is None, "the overruled agreement is still attached"
    assert j["cost"]["kind"] in ("discount", "discount_unknown_base")

    # A reader who checks JCT will see an agreement we are not honouring, so
    # the record has to say why, and cite who told us.
    c = j["deal"]["correction"]
    assert c and c["confirmed"] == "2026-08-10"
    assert c["jct_says"] and c["oxford_says"]
    assert any("open access team" in cav for cav in j["deal"]["caveats"])


def test_a_correction_never_silently_widens_a_claim(built):
    """A correction removes an agreement. It must never be able to invent one:
    the only statuses it can produce are whatever the overlay says on its own."""
    for j in built["journals"]["journals"]:
        if (j["deal"].get("correction")):
            assert j["deal"]["status"] != "covered", \
                f"{j['id']}: a correction produced a coverage claim"
            assert j["cost"]["kind"] not in ("covered", "covered_conditional")


# ------------------------------------------------- Oxford's answers are pinned
def oxford_verdicts(built) -> dict:
    """Everything the site tells an Oxford author about paying, per journal.

    Converted prices are left out because they move with the exchange rate;
    whether a journal sorts as free is kept, because that is a claim.
    """
    index = {r["id"]: r for r in built["index"]["journals"]}
    out = {}
    for j in built["journals"]["journals"]:
        cost = dict(j["cost"])
        gbp = cost.pop("gbp", None)
        row = index[j["id"]]
        out[j["id"]] = {
            "title": j["title"],
            "deal": j["deal"],
            "cost": cost,
            "sorts_as_free": gbp == 0,
            "waiver": j["waiver"],
            "shown": {"status": row["s"], "cost_label": row["c"],
                      "disputed": row["x"], "expired": row["e"]},
        }
    return out


def test_oxford_verdicts_match_the_golden_file(built):
    """The deal, caveats, cost and cost label for every fixture journal, held
    against a committed copy.

    This is the guard for work that is not supposed to touch Oxford at all —
    adding other universities, most of all. Such a change has no Oxford
    behaviour to assert, so nothing else would notice it altering one.

    A deliberate change to an Oxford answer fails here too. Regenerate with
    APC_UPDATE_GOLDEN=1 and read the diff of the golden file in review: that
    diff is the list of answers that changed.
    """
    got = oxford_verdicts(built)
    if os.environ.get("APC_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True,
                                     ensure_ascii=False) + "\n")
    want = json.loads(GOLDEN.read_text())
    assert set(got) == set(want), "the set of fixture journals changed"
    changed = {i: {"was": want[i], "now": got[i]} for i in got if got[i] != want[i]}
    assert not changed, ("Oxford answers changed:\n"
                         + json.dumps(changed, indent=1, ensure_ascii=False))


def test_the_golden_file_covers_every_kind_of_answer():
    """A golden file of twelve list prices would pin nothing that matters. It
    has to hold each verdict the site can give, including the weakened ones."""
    want = json.loads(GOLDEN.read_text()).values()
    assert {v["deal"]["status"] for v in want} >= {
        "covered", "discount", "diamond", "none"}
    assert {v["cost"]["kind"] for v in want} >= {
        "covered", "covered_conditional", "uncertain", "discount",
        "discount_unknown_base", "list_price", "no_apc", "diamond", "unknown"}
    assert any(v["deal"]["expired"] for v in want)
    assert any(v["deal"]["disputed"] for v in want)
    assert any(v["deal"]["correction"] for v in want)


def test_the_build_date_can_only_be_pinned_in_a_fixtures_build(monkeypatch):
    """APC_TODAY exists so the golden file does not rot. Left set in a live
    build it would quietly un-expire agreements, so a live build ignores it."""
    import datetime
    import merge
    monkeypatch.setenv("APC_TODAY", "2020-01-01")
    monkeypatch.setattr(merge, "FIXTURES_MODE", False)
    assert merge.build_date() == datetime.date.today()
    monkeypatch.setattr(merge, "FIXTURES_MODE", True)
    assert merge.build_date() == datetime.date(2020, 1, 1)


# ------------------------------------------------------- other universities
def test_other_universities_are_shipped_without_touching_a_journal_record(built):
    """The site files for other universities sit beside the journal data and
    refer to it by id. Nothing about them is written into a journal record or
    the index — the golden file above would catch a changed value, this
    catches an added one."""
    read = lambda p: json.loads((built["work"] / p).read_text())  # noqa: E731
    inst = read("_site/data/institutions.json")
    titles = read("_site/data/agreement_titles.json")
    assert built["config"]["institutions_available"] is True
    assert len(titles) == len(inst["agreements"])
    ids = {j["id"] for j in built["journals"]["journals"]}
    assert all(set(t) <= ids for t in titles), "a title that is not a site journal"
    assert all(0 <= n < len(titles) for i in inst["institutions"] for n in i["agreements"])
    assert not any(i["ror"] == "052gg0110" for i in inst["institutions"])
    blob = json.dumps(built["journals"]) + json.dumps(built["index"])
    for i in inst["institutions"]:
        assert i["name"] not in blob and i["ror"] not in blob


def test_the_site_builds_without_other_university_data(built, tmp_path):
    """A dataset cached before this feature has no institutions.json. The
    site-only deploy must still build from it, and offer no university list."""
    work = tmp_path / "repo"
    shutil.copytree(built["work"], work, ignore=shutil.ignore_patterns("_site"))
    (work / "data" / "out-fixtures" / "institutions.json").unlink()
    result = subprocess.run(
        [sys.executable, str(work / "pipeline" / "build_site.py")], cwd=work,
        env={**os.environ, "APC_FIXTURES": "1"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((work / "_site" / "config.json").read_text())[
        "institutions_available"] is False
    assert not (work / "_site" / "data" / "institutions.json").exists()


def test_a_bodleian_only_agreement_survives_the_whole_pipeline(built):
    """Covered on the Bodleian page's word, with no JCT agreement behind it.
    The journal has a list price, and none of it may reach the reader."""
    j = next(j for j in built["journals"]["journals"] if j["id"] == "9990-0005")
    assert j["deal"]["status"] == "covered" and j["deal"]["esac_id"] is None
    assert j["deal"]["conditions"]["unlisted_agreement"] is True
    assert j["cost"]["kind"] == "covered_conditional"
    assert "1500" not in json.dumps(j["cost"]) and "1,500" not in json.dumps(j["cost"])
    assert any(s["label"] == "Bodleian publisher deals page"
               for s in j["provenance"]["deal"])
    row = next(r for r in built["index"]["journals"] if r["id"] == "9990-0005")
    assert row["c"] == "£0 if eligible — but confirm first"
    assert row["f"] is False, "a conditional agreement is not 'free to publish'"
