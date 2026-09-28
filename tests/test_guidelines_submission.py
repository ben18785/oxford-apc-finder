"""The issue → guidelines-file step behind guidelines-submission.yml.

Its input is written by anyone with a GitHub account and its output is a file
in the repository, so the rejections matter as much as the happy path.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "guidelines_from_issue", ROOT / ".github" / "scripts" / "guidelines_from_issue.py")
gfi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gfi)


def body(sub) -> str:
    payload = sub if isinstance(sub, str) else json.dumps(sub)
    return f"<!-- guidelines-submission -->\nWord limits for X.\n\n```json\n{payload}\n```\n"


GOOD = {
    "id": "9990-0001", "journal": "Fixture Journal", "publisher": "Frontiers",
    "issns": ["9990-0001", "9999-0001"], "url": "https://example.org/authors",
    "article_types": [
        {"type": "Original Research", "min": None, "max": 8000, "notes": "Excludes abstract"},
        {"type": "Letter", "min": 500, "max": 1500, "notes": None},
    ],
}


def test_a_submission_merges_without_losing_unshown_fields():
    existing = {
        "journal": "Old name", "LLM": True, "validated": True, "remarks": "kept",
        "article_types": [{
            "type": "Original Research", "description": "kept",
            "total_word_limit": {"min": 12000, "max": 12000, "unit": "words",
                                 "excludes": ["references"], "notes": None},
            "figure_limits": [{"max": 8}],
        }],
    }
    rec = gfi.merge(gfi.parse(body(GOOD)), existing, "https://github.com/o/r/issues/7")
    research, letter = rec["article_types"]
    assert research["description"] == "kept" and research["figure_limits"] == [{"max": 8}]
    assert research["total_word_limit"]["excludes"] == ["references"]
    assert (research["total_word_limit"]["min"], research["total_word_limit"]["max"]) == (None, 8000)
    assert research["notes"] == "Excludes abstract"
    assert letter["total_word_limit"]["max"] == 1500
    assert rec["remarks"] == "kept"
    # A reader's submission is never recorded as checked, whatever it replaced.
    assert rec["LLM"] is False and rec["validated"] is False
    assert rec["submitted_in"] == "https://github.com/o/r/issues/7"


def test_a_new_journal_gets_its_issns_from_the_submission():
    rec = gfi.merge(gfi.parse(body(GOOD)), {}, None)
    assert rec["issn"] == {"print": "9990-0001", "electronic": "9999-0001"}


def test_structure_and_figures_are_set_cleared_or_kept():
    existing = {"article_types": [
        {"type": t, "structure": "old", "figures_tables": "old"} for t in ("A", "B", "C")]}
    sub = {**GOOD, "article_types": [
        {"type": "A", "structure": "Abstract, Methods", "figures_tables": "Up to 6"},
        {"type": "B", "structure": "", "figures_tables": None},
        {"type": "C"},                      # issue from before the form had the boxes
    ]}
    a, b, c = gfi.merge(gfi.parse(body(sub)), existing, None)["article_types"]
    assert (a["structure"], a["figures_tables"]) == ("Abstract, Methods", "Up to 6")
    assert (b["structure"], b["figures_tables"]) == (None, None)
    assert (c["structure"], c["figures_tables"]) == ("old", "old")


def test_blank_limits_clear_the_word_limit():
    sub = {**GOOD, "article_types": [{"type": "Editorial", "min": None, "max": None}]}
    rec = gfi.merge(gfi.parse(body(sub)), {}, None)
    assert rec["article_types"][0]["total_word_limit"] is None


@pytest.mark.parametrize("sub, reason", [
    ("not json", "does not parse"),
    ({**GOOD, "id": "../../site/app"}, "`id`"),
    ({**GOOD, "id": None}, "`id`"),
    ({**GOOD, "url": "javascript:alert(1)"}, "`url`"),
    ({**GOOD, "article_types": []}, "between 1"),
    ({**GOOD, "article_types": [{"type": "A", "min": 10, "max": 5}]}, "above max"),
    ({**GOOD, "article_types": [{"type": "A", "max": "lots"}]}, "whole number"),
    ({**GOOD, "article_types": [{"type": "A", "max": True}]}, "whole number"),
    ({**GOOD, "article_types": [{"type": "A"}, {"type": "A"}]}, "twice"),
    ({**GOOD, "article_types": [{"type": ""}]}, "missing"),
])
def test_bad_submissions_are_rejected_with_a_reason(sub, reason):
    with pytest.raises(gfi.Rejected) as e:
        gfi.parse(body(sub))
    assert reason in str(e.value)


def test_an_issue_without_a_json_block_is_rejected():
    with pytest.raises(gfi.Rejected, match="No ```json block"):
        gfi.parse("just some text")


def test_newlines_cannot_reach_the_workflow_outputs():
    """`journal` is written to $GITHUB_OUTPUT as key=value lines; a newline in
    it would let a submitter set other outputs."""
    sub = {**GOOD, "journal": "Legit\nid=../../evil"}
    assert "\n" not in gfi.parse(body(sub))["journal"]


def test_main_writes_the_file_and_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(gfi, "GUIDELINES", tmp_path)
    monkeypatch.setattr(gfi, "ROOT", tmp_path)
    out = tmp_path / "out.txt"
    monkeypatch.setenv("ISSUE_BODY", body(GOOD))
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert gfi.main() == 0
    assert json.loads((tmp_path / "9990-0001.json").read_text())["source"] == "user"
    assert out.read_text() == "id=9990-0001\njournal=Fixture Journal\n"


def test_main_explains_a_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(gfi, "GUIDELINES", tmp_path)
    monkeypatch.setenv("ISSUE_BODY", body({**GOOD, "id": "a/b"}))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    assert gfi.main() == 1
    assert "`id`" in (tmp_path / "guidelines-error.md").read_text()
    assert not list(tmp_path.glob("*.json"))
