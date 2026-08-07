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
    (work / "data" / "out").mkdir(parents=True, exist_ok=True)

    # Copy the committed baselines too. Without them this test ran against a
    # repo state that never exists in practice, and so missed that a fixtures
    # build was being compared against real-dataset counts — which failed in
    # CI, where the whole repo is checked out.
    for baseline in (Path("data/out/last_counts.json"),
                     Path("data/state/journal_state.tsv")):
        if (ROOT / baseline).exists():
            (work / baseline).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / baseline, work / baseline)

    env = {**os.environ, "APC_FIXTURES": "1"}
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
        "journals": read("data/out/journals.json"),
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
        assert d.get("jct_says") and d.get("bodleian_says")


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

    changes = json.loads((work / "data" / "out" / "changes.json").read_text())
    assert changes["summary"]["added"] == 1
    assert dropped in [a["issn_l"] for a in changes["added"]]
    assert (work / "CHANGELOG-data.fixtures.md").exists()


def test_regression_fixture_build_ignores_the_real_baseline(built):
    """A fixtures build has ~12 journals; the committed baseline describes
    ~43,000. Comparing them tripped the week-on-week drop threshold and failed
    every offline run in CI (the test suite missed it by not copying the
    baseline into its work dir)."""
    assert (built["work"] / "data" / "out" / "last_counts.json").exists(), \
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
            assert j["deal"]["esac_id"] in j["deal"]["basis"]


def test_undealt_journals_say_they_were_checked(built):
    """The distinction that matters: searched and genuinely absent, versus
    unknown to the tool."""
    for j in built["journals"]["journals"]:
        if j["deal"]["status"] == "none":
            assert "Checked against" in j["deal"]["basis"] or "arrangement with" in j["deal"]["basis"]


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
