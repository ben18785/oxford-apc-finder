# Developer guide

How the pipeline is built, run and tested. For what the site *is* and where its
information comes from, see [README.md](README.md).

## How it works

```
JCT transformative-agreement CSVs ─┐
(CC BY 4.0, filtered to Oxford ROR)│
                                   ├─► merge.py ─► journals.json ─► static site
OpenAlex sources (CC0) ────────────┤     ▲            │
DOAJ metadata (CC0) + withdrawals ─┘     │            └─► search index + per-journal detail
                                         │
oxford_overrides.yaml  ──────────────────┘  (hand-curated: discounts, diamond,
(from the Bodleian deals page)               funder rules, caps — the bits JCT
                                             doesn't carry)
```

The pipeline is six numbered scripts in `pipeline/`:

| Script | Does |
|---|---|
| `fetch_jct.py` | Downloads the JCT agreements index + each agreement CSV, keeps those where Oxford's ROR is a current participant → `data/out/deals.json` |
| `watch_bodleian.py` | Diffs the Bodleian deals page vs the stored snapshot; on change, exits 3 so CI opens a `needs-review` issue |
| `fetch_metadata.py` | OpenAlex + DOAJ records for every in-scope ISSN → `data/out/metadata.json` |
| `merge.py` | Joins everything, applies inclusion policy, computes cost, attaches per-fact provenance → `data/out/journals.json` |
| `validate.py` | Schema checks, week-on-week drop threshold, and a live cross-check against the JCT API of a date-seeded sample stratified across covered/uncovered journals |
| `changelog.py` | Diffs this build against the last, writes `CHANGELOG-data.md` + `changes.json`, updates the committed baseline |
| `build_site.py` | Emits the static site + search index into `_site/` |
| `collect_links.py` | Gathers the URLs the site can show, for the link-check workflow (not part of `run_all.py`) |

Run the whole thing:

```bash
pip install -r pipeline/requirements.txt
python pipeline/run_all.py            # live data
APC_FIXTURES=1 python pipeline/run_all.py   # build from bundled sample data
```

Then open `_site/index.html` (via any static server, e.g. `python -m http.server -d _site`).

## Inclusion & anti-predatory policy

A journal is included if **any** of seven routes admits it: (1) an Oxford deal
covers it, (2) it is in DOAJ, (3) its publisher is on
`data/curated/publisher_allowlist.yaml`, (4) it appears in a transformative
agreement anywhere in the world, (5) it is among the most-cited journals
globally, (6) it leads its own subfield, or (7) the site listed it before and
has not yet retired it. The authoritative list is the docstring at the top of
`pipeline/merge.py`, next to the code that applies it.
Journals withdrawn from DOAJ for misconduct-type reasons are excluded outright.
The site shows verification badges (In DOAJ, covered by a Jisc agreement) rather
than ever asserting a journal is predatory. See the design doc for rationale.

## Error reporting (GitHub-only, no server)

Each journal has a "report" box. What the user types is folded into a
**pre-filled GitHub issue** (via `issues/new` query params against the
`data-error.yml` form). Reports are public and tracked in the repo. There is no
backend and no secret token — the only requirement is that the reporter has a
GitHub account to click "submit".

---

## Setup

1. **Point the site at your repo.** `config.yaml` → `github_repo` (used for the
   report links). Already set to `ben18785/oxford-apc-finder`.

2. **Enable GitHub Pages.** Repo → Settings → Pages → Build and deployment →
   Source: **GitHub Actions**.

3. **Allow Actions to push.** Repo → Settings → Actions → General → Workflow
   permissions → **Read and write permissions**. The refresh job commits the
   updated `last_counts.json` / `bodleian_snapshot.txt` baselines back to `main`;
   without this it fails at that step.

4. **Add the `OPENALEX_API_KEY` secret** (free from
   <https://openalex.org/settings/api>). OpenAlex bills $0.0001 per request and
   allows **$1.00/day with a key, $0.10/day anonymously**. A full refresh makes
   roughly 800 OpenAlex calls (~$0.08), which fits the free key comfortably but
   sits right on the anonymous ceiling — without a key a refresh may be
   truncated by rate limiting. `fetch_metadata.py` prints the run's spend and
   warns at 80% of the allowance.

5. **(Optional) Turn on usage monitoring.** Off by default and entirely
   skippable — see below.

Then push to `main`, or run the **Refresh data and deploy** workflow manually.

### Usage monitoring

Off unless you switch it on, and the site is functionally identical either way.
With `analytics.goatcounter_code` left `null`, no third-party script is fetched,
nothing is counted, and the "How this site is used" link does not appear.

To enable it:

1. Create a free site at <https://www.goatcounter.com> and note the site code
   (the `<code>.goatcounter.com` subdomain).
2. Set `analytics.goatcounter_code` in `config.yaml` to that code. This value is
   public by design — it is the subdomain readers' browsers post hits to.
3. Create an API token in GoatCounter (Settings → API) with **read-only**
   permissions, and add it as the repo secret `GOATCOUNTER_TOKEN`. This one is
   *not* public; it is used only by `pipeline/fetch_usage.py` to read the
   aggregate back, and is never written into the site.

GoatCounter was chosen because it fits the same constraints as everything else
here: no cookies, no IP addresses retained (it hashes IP + user-agent with a
salt that rotates daily to count uniques, then discards it), no cross-site
identifier, and so **no consent banner needed** under GDPR. It is open source,
so the same data can be self-hosted if the service ever goes away.

What is recorded:

| Event | Path posted | Why |
|---|---|---|
| Page load | `/` | Visitor and session counts |
| Journal opened | `/j/<deal-status>/<issn>` | Most-looked-up chart, and the deal-coverage share |
| Search returning **nothing** | `/missing/<normalised query>` | The coverage gap — a work queue for the publisher allowlist |

The deal status rides in the journal path rather than going as a second event,
so one call yields both the per-journal count and the coverage split and the
two cannot drift apart. Searches that *succeed* are never recorded.

Two publication floors guard against the aggregate becoming disclosive. Oxford
is a small population and looking a journal up is close to saying "I am
thinking of submitting here", so a journal joins the public chart only at
`min_views_to_publish` views, and a free-text query only at
`min_searches_to_publish` searches. Whatever is withheld is still *declared* on
the page as a count — a chart that silently drops its tail reads as complete.

`fetch_usage.py` always exits 0. A missing token, an API change, or a
GoatCounter outage costs the site a chart, never a refresh, and leaves the
previous `usage.json` in place rather than overwriting real history with zeros.

### The subfield sweep runs quarterly, not weekly

`inclusion.top_journals_per_subfield` finds the leading journals inside each of
OpenAlex's 252 subfields. It exists because a global citation ranking is
dominated by biomedicine and physics — ranking *within* a field is what makes
law and the humanities findable at all.

It costs ~500 requests and ~13 minutes, so it does **not** run every refresh.
It is a *discovery* step: it answers "which journals lead each discipline", and
that answer does not change from one Monday to the next. Once discovered, a
journal stays in scope through `known_journals.tsv` for a year, and its facts
are re-fetched every run like everything else. Scope is accumulated; facts never
are — so a quarterly sweep costs nothing in freshness.

`inclusion.subfield_sweep_days` (default 90) sets the cadence, tracked in the
committed `data/state/subfield_sweep.json`. Keep it comfortably below
`remember_journals_days` or journals will age out of scope between sweeps. It
re-runs early if you change the depth, since a larger number means journals the
stored set was never asked about, and it re-runs rather than skipping if the
marker is missing or unreadable — failing closed here would silently disable
discovery with no signal anywhere.

To run it now: tick **Re-run the per-subfield journal discovery** on the
workflow dispatch, or set `APC_FORCE_SUBFIELD_SWEEP=1` locally.

A sweep that fails does not fail the refresh. It costs the journals that sweep
would have added; everything discovered previously is still in scope. The
marker is deliberately not written on failure, so the next run retries rather
than waiting out the quarter.

### Two deploy paths

`data/out/` is not committed — `journals.json` alone is 128 MB and changes
weekly — so for a long time every deploy meant rebuilding the dataset from
scratch. A CSS fix and a data refresh cost the same hour and the same ~800
OpenAlex calls. There are now two workflows instead:

| | `refresh-and-deploy.yml` | `deploy-site.yml` |
|---|---|---|
| Fires on | `pipeline/**`, `config.yaml`, `data/curated/**`, the weekly cron | `site/**`, `pipeline/build_site.py` |
| Does | fetch → merge → validate → changelog → build → deploy | restore cached dataset → build → deploy |
| Takes | ~1 hour | ~2 minutes |
| Can change the data | yes | **no** |

The refresh caches the three files `build_site.py` reads (`journals.json`,
`changes.json`, `usage.json` — not `metadata.json`, another 122 MB that is
never opened after merge) under a `dataset-v1-` key. The site workflow restores
by that prefix, so it always renders the most recent refresh's output.

Three things about this are load-bearing, and `tests/test_workflows.py` asserts
all of them:

* **The path filters must stay disjoint.** Both publish to the same Pages
  environment and share a concurrency group, so a file matching both starts two
  runs and one silently cancels the other — which looks exactly like a deploy
  that never happened.
* **The cache must carry everything `build_site.py` reads.** Adding a read
  without adding it to the cache breaks every site-only deploy, and only the
  next time one runs.
* **The site path must never fetch.** Its whole premise is that it re-renders
  and nothing more; it holds `contents: read` and runs no pipeline stage but
  the build.

`config.yaml` deliberately routes to the *full* refresh even though most of it
is presentational, because it also holds the inclusion and validation
thresholds. Sending it down the fast path would let an inclusion change appear
to deploy while silently not being applied.

A site-only deploy publishes whatever the last refresh produced, so it can
publish stale data — but honestly: the age comes from `journals.json` itself,
so the staleness banner and "data freshness" page report the real figure, not
the deploy time. It also re-runs the frontend suite against the rebuilt `_site`
before deploying, which is what would catch a cached dataset that predates a
schema change.

GitHub evicts caches unused for 7 days. The weekly refresh writes a fresh one
and every site deploy reads it, so in normal use it stays warm; if it ever does
go, the workflow fails with an explicit message rather than deploying an empty
site.

### Schedule

`refresh-and-deploy.yml` runs weekly (Mon 04:17 UTC). To go daily — also free
on a public repo, and within the OpenAlex allowance — change the cron to
`17 4 * * *`. `link-check.yml` runs the
[lychee](https://github.com/lycheeverse/lychee) link checker the next day
(Tue 06:43 UTC) and opens a `broken-links` issue if anything rots.

Link checking is split in two (`pipeline/collect_links.py`): every
*infrastructure* link — the Bodleian pages, JCT agreement CSVs, publisher deal
pages, API endpoints — is checked every run, because one dead URL there breaks
the same link for thousands of journals. The tens of thousands of third-party
journal homepages are checked in a rotating deterministic hash bucket (1/26 per
run) so the whole set is covered roughly twice a year without hammering
publishers weekly.

## Tests

```bash
pip install -r pipeline/requirements.txt pytest
pytest                  # offline suite (unit + integration)
pytest -m network       # contract tests against the live sources
node tests/frontend/search.test.js      # search behaviour, needs a built _site/
```

Three tiers, chosen around how this project actually breaks. Every serious bug
so far has been an upstream shape assumption that was wrong or went stale — and
worse, most failed *silently*, producing empty or partial data that the pipeline
happily shipped.

| Tier | What it catches | Cost | When |
|---|---|---|---|
| **Unit** (`test_units.py`) | Parsing and cost-calculation logic. Tests named `test_regression_*` pin bugs that shipped. | ~0.1s | every push/PR |
| **Integration** (`test_pipeline_fixtures.py`) | Whole-pipeline behaviour and dataset invariants — misconduct exclusions applied, covered journals carry provenance and the universal criteria, index/keyword arrays aligned, every journal reachable in a shard. | ~1s | every push/PR |
| **Frontend** (`tests/frontend/search.test.js`) | `site/app.js` driven headlessly over a real build: ISSN lookup, AND semantics, the deal-only filter, lazy keyword loading. | ~2s | every push/PR |
| **Contracts** (`test_source_contracts.py`) | Whether JCT, DOAJ, OpenAlex and the Bodleian pages still look the way the parsers assume. | ~15s, live | daily + manual |

The contract tier is the important one. Unit tests cannot catch "DOAJ caps
pagination at 1,000 records" or "the changelog header cell has a trailing
space", because they test our parsing of data we invented. The contract tests
assert against the real sources on a schedule and open a `source-drift` issue
when one changes, so a break surfaces before the weekly refresh rather than as
a failed build — or a build that succeeded with empty data.

Fixtures are generated from a real run (`python tests/make_fixtures.py`), picking
one journal per branch — covered, discounted with and without a base price,
diamond, disputed, no-APC, withdrawn-for-misconduct — so the offline suite stays
representative instead of drifting from what the live path produces.

The frontend tests run under Node (CI) or JavaScriptCore (`jsc`, which ships
with macOS), so no browser or `npm install` is needed either way.

## Tracking what changed, and when

Deals start, change, hit annual caps and end; sources also just get things
wrong and later fix them. Three mechanisms record that, so "what did the site
say in March?" has an answer:

| Mechanism | Catches | Where it surfaces |
|---|---|---|
| `data/state/journal_state.tsv` | Per-journal deal status, cost, price, currency, disputed flag | Committed every refresh — `git log -p` on this file is the full history |
| `CHANGELOG-data.md` + `changes.json` | Journals added/removed, deal status moves, price moves | "What changed recently" on the site; the dated file in the repo |
| `watch_bodleian.py` | Any edit to the Bodleian deals page | Opens a `needs-review` issue with a text diff |

The state file is one sorted line per journal, so git stores small deltas and
`git log` is readable. `changelog.py` runs **after** `validate.py`, so a build
that fails validation never rewrites the baseline — the next successful run
still diffs against the last *good* dataset.

A sharp jump in the changelog usually means a source problem rather than a real
event: thousands of journals changing status at once is what a truncated fetch
looks like. `validate.py`'s drop threshold catches the worst of it, and the
changelog is how you see the rest.

## Disclaimer

The site carries a permanent disclaimer bar in the header (not dismissible) and
a fuller **Disclaimer** view: unofficial tool, no warranty, costs are estimates
not quotes, eligibility depends on facts the site cannot see, and the Bodleian
is the authoritative source. Journals where the sources contradict each other
carry a per-journal "sources disagree" warning shown *above* the cost figure.

## Data sources & licenses

- **Journal Checker Tool** transformative-agreement data — CC BY 4.0 — <https://journalcheckertool.org/transformative-agreements/>
- **OpenAlex** — CC0 — <https://openalex.org>
- **DOAJ** metadata — CC0; change-log — CC BY-SA 4.0 — <https://doaj.org>
- **Bodleian publisher deals page** (facts, for the curated overlay) — <https://www.bodleian.ox.ac.uk/open-research/open-access-publishing/journal-article/publisher-deals>

Project code is MIT (see `LICENSE`). This is an independent tool, not an
official Bodleian Libraries service.

## Limitations

- The overlay file (`oxford_overrides.yaml`) is hand-maintained from the
  Bodleian page; the watcher flags changes but a human applies them.
- APC list prices are refreshed, not real-time — every price carries its
  retrieval date and a "confirm with the journal" note.
- Deal eligibility depends on corresponding authorship, article type, funder
  and sometimes annual caps; the site surfaces caveats and points to
  oapayments@bodleian.ox.ac.uk for the authoritative answer.
