"""Checks on the CI workflows themselves.

Two deploy paths now exist — a full refresh and a render-only rebuild — and
the ways they can go wrong are all silent:

  * overlapping path filters start two runs from one push, which then race at
    the Pages environment;
  * a file build_site.py reads but the refresh does not cache makes every
    site-only deploy fail, but only once the cache is next used;
  * a fetch stage creeping into the site-only workflow would let a "front-end"
    deploy quietly change the data.

None of these show up in a normal test run, so they are asserted here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
REFRESH = WORKFLOWS / "refresh-and-deploy.yml"
DEPLOY = WORKFLOWS / "deploy-site.yml"


def load(path: Path) -> dict:
    # `on:` is parsed by PyYAML as the boolean True (YAML 1.1 treats on/off as
    # booleans), so the trigger block is keyed by True, not "on".
    return yaml.safe_load(path.read_text())


def triggers(wf: dict) -> dict:
    return wf.get("on") or wf.get(True) or {}


def glob_to_re(pattern: str) -> re.Pattern:
    """GitHub path-filter glob → regex. `**` spans directories, `*` does not."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(path: str, patterns: list[str]) -> bool:
    """GitHub semantics: last matching pattern wins, `!` negates."""
    verdict = False
    for pat in patterns:
        negate = pat.startswith("!")
        if glob_to_re(pat.lstrip("!")).match(path):
            verdict = not negate
    return verdict


def push_paths(path: Path) -> list[str]:
    return triggers(load(path))["push"]["paths"]


# ----------------------------------------------------- the two must not overlap
REPO_FILES = [
    "site/app.js", "site/index.html", "site/style.css",
    "pipeline/build_site.py", "pipeline/merge.py", "pipeline/fetch_jct.py",
    "pipeline/fetch_usage.py", "config.yaml",
    "data/curated/oxford_overrides.yaml", "data/curated/must_include.yaml",
    ".github/workflows/refresh-and-deploy.yml",
    ".github/workflows/deploy-site.yml",
    "README.md", "tests/test_units.py",
]


@pytest.mark.parametrize("path", REPO_FILES)
def test_no_push_starts_both_deploy_workflows(path):
    """One push, one deploy. Both workflows publish to the same Pages
    environment, so a file matching both filters starts two runs that race —
    and because they share a concurrency group, one silently cancels the
    other, which looks exactly like a deploy that never happened."""
    both = matches(path, push_paths(REFRESH)) and matches(path, push_paths(DEPLOY))
    assert not both, f"{path} triggers both workflows"


@pytest.mark.parametrize("path,workflow", [
    ("site/app.js", "deploy"),
    ("site/style.css", "deploy"),
    ("pipeline/build_site.py", "deploy"),      # renders; never fetches
    ("pipeline/merge.py", "refresh"),
    ("pipeline/fetch_jct.py", "refresh"),
    ("config.yaml", "refresh"),                # holds inclusion thresholds
    ("data/curated/must_include.yaml", "refresh"),
])
def test_each_change_reaches_the_workflow_that_should_handle_it(path, workflow):
    """Disjointness alone would be satisfied by neither workflow firing."""
    want_refresh = workflow == "refresh"
    assert matches(path, push_paths(REFRESH)) is want_refresh
    assert matches(path, push_paths(DEPLOY)) is not want_refresh


# --------------------------------------------- the cache must carry what is read
def cache_step(wf: dict, action_prefix: str) -> dict:
    for job in wf["jobs"].values():
        for step in job["steps"]:
            if step.get("uses", "").startswith(action_prefix):
                return step
    raise AssertionError(f"no step using {action_prefix}")


def test_the_refresh_caches_every_file_the_site_build_reads():
    """build_site.py reading a data file the refresh does not cache would make
    every site-only deploy fail — and only the next time one runs, long after
    the change that caused it."""
    source = (ROOT / "pipeline" / "build_site.py").read_text()
    # The lookbehind matters: without it this also matches SITE_OUT, which is
    # the *output* directory, and the test then demands the build's own
    # products be restored from cache before they are written.
    read = set(re.findall(r'(?<![A-Z_])OUT / "([^"]+)"', source))
    assert read, "found no OUT reads — has build_site.py been restructured?"
    cached = {line.strip().removeprefix("data/out/")
              for line in cache_step(load(REFRESH), "actions/cache/save")
              ["with"]["path"].splitlines() if line.strip()}
    assert read <= cached, f"build_site reads {read - cached}, which is not cached"


def test_restore_uses_the_same_cache_prefix_as_save():
    save = cache_step(load(REFRESH), "actions/cache/save")["with"]["key"]
    restore = cache_step(load(DEPLOY), "actions/cache/restore")["with"]["restore-keys"]
    assert save.startswith(restore.strip()), f"{save!r} does not match {restore!r}"


# ------------------------------------------------ the site path must not fetch
def test_a_site_deploy_cannot_change_the_data():
    """The whole premise of the fast path is that it only re-renders. A fetch
    stage here would let a change advertised as front-end-only silently alter
    what the site claims about a journal.

    fetch_usage.py is deliberately NOT on this list: it reads visitor counters
    and writes usage.json, touching no journal fact, and it is three seconds of
    work that has no business waiting behind an hour of fetching."""
    body = DEPLOY.read_text()
    for forbidden in ("run_all.py", "fetch_jct.py", "fetch_metadata.py",
                      "merge.py", "changelog.py", "validate.py"):
        assert forbidden not in body, f"deploy-site.yml runs {forbidden}"


def test_the_usage_stage_never_fails_a_deploy():
    """fetch_usage.py exits 0 on every path by design, so a GoatCounter outage
    cannot block a site deploy. If that ever changes, this workflow becomes a
    way for a third-party API to take the site's deploys down with it."""
    source = (ROOT / "pipeline" / "fetch_usage.py").read_text()
    assert "sys.exit(0)" in source
    assert "except Exception" in source, "the top-level catch-all is gone"


def test_a_site_deploy_cannot_write_to_the_repo():
    """No baseline commit on this path, so it has no business holding write
    access to contents."""
    assert load(DEPLOY)["permissions"]["contents"] == "read"


def test_both_deploys_share_one_concurrency_group():
    """They publish to the same Pages environment; separate groups would let a
    site deploy overtake a refresh mid-flight and put a half-built site live."""
    assert load(REFRESH)["concurrency"]["group"] == load(DEPLOY)["concurrency"]["group"]


def test_a_site_deploy_never_cancels_a_running_refresh():
    """GitHub lets the incoming run decide whether to cancel the running one.
    With cancel-in-progress true here, a two-minute stylesheet deploy would
    kill a refresh fifty minutes into its fetch — an hour of work and ~800 API
    calls, for a CSS change. Queuing costs only a wait."""
    assert load(DEPLOY)["concurrency"]["cancel-in-progress"] is False
    # The refresh keeps `true`: a newer refresh genuinely does supersede an
    # older one, and a run wedged on the Pages environment must not hold the
    # group for ever (that outage is why it was set).
    assert load(REFRESH)["concurrency"]["cancel-in-progress"] is True


def test_the_site_deploy_verifies_what_it_built():
    """A cached dataset predating a schema change renders a broken site with no
    error. The frontend suite is the only thing that would notice."""
    assert "tests/frontend/search.test.js" in DEPLOY.read_text()


def test_unshipped_groundwork_is_still_tested():
    """alerts/ is not wired into the site, but it is in the repository and it
    decides what would land in somebody's inbox. Untested dormant code is how
    you get a nasty surprise on the day it is switched on. It runs in the test
    workflow only — deploy-site.yml renders the site, and digest.js is not in
    the site."""
    assert "digest.test.js" in (WORKFLOWS / "tests.yml").read_text()
    assert "digest.test.js" not in DEPLOY.read_text(), \
        "the site deploy runs a test for code the site does not contain"


def test_alerts_hold_no_secret_in_this_repo():
    """The whole point of putting alerts in a Worker is that email addresses
    and the sending key never enter this repository or its Actions."""
    for wf in WORKFLOWS.glob("*.yml"):
        body = wf.read_text()
        assert "RESEND_API_KEY" not in body, f"{wf.name} references the mail key"
        assert "ALERTS" not in body or "alerts/" in body, f"{wf.name} touches the alert store"
