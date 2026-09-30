"""The PR half of link-check.yml: which URLs does a pull request add?

Only links written into the repo can be changed by a PR, so only those are
checked there; the weekly run checks the live data's links."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "added_links", ROOT / ".github" / "scripts" / "added_links.py")
al = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(al)


def urls(diff: str) -> list[str]:
    return al.urls_in(al.added_lines(diff))


def test_only_added_lines_count():
    diff = ("+++ b/site/app.js\n"
            "-  <a href=\"https://removed.example.net/x\">\n"
            "   context https://context.example.net/\n"
            "+  <a href=\"https://journalcheckertool.org/\" target=\"_blank\">\n")
    assert urls(diff) == ["https://journalcheckertool.org/"]


def test_template_placeholders_are_not_addresses():
    diff = ("+ window.open(`https://github.com/${STATE.config.github_repo}/issues/new?title=${t}`)\n"
            "+ placeholder=\"https://…\"\n")
    assert urls(diff) == []


def test_local_and_example_hosts_are_skipped():
    diff = ("+ http://localhost:8765/\n"
            "+ https://example.org/authors\n"
            "+ fetch(\"http://127.0.0.1/api\")\n")
    assert urls(diff) == []


def test_trailing_punctuation_and_markup_are_trimmed():
    diff = ('+ See https://doaj.org/. Or <a href="https://ror.org/052gg0110">\n'
            "+ (https://openalex.org/)\n")
    assert urls(diff) == ["https://doaj.org/", "https://openalex.org/",
                          "https://ror.org/052gg0110"]


def test_query_strings_survive():
    diff = "+  url: \"https://docs.google.com/forms/d/e/abc/viewform?usp=pp_url\"\n"
    assert urls(diff) == ["https://docs.google.com/forms/d/e/abc/viewform?usp=pp_url"]
