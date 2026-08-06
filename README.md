# Oxford Journal APC Finder

A free, static website that tells an Oxford researcher whether the University's
open-access agreements cover a given journal, what publishing there will cost
once a deal is applied, a short description of the journal's scope, and — for
every fact shown — a link to exactly where it came from.

Search defaults to journals Oxford has deals with; switch the filter off to
search everything, with publication charges listed. It never labels journals
"predatory": it controls what gets in (DOAJ + vetted publishers + deal
coverage) and shows positive verification badges instead.

**Design principles:** deterministic scripts (no LLM in the refresh path),
provenance on every fact, and near-zero running cost (GitHub Pages + Actions).

---

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

A journal is included only if it is (1) covered by an Oxford deal, (2) listed
in DOAJ, or (3) from a publisher on `data/curated/publisher_allowlist.yaml`.
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

Then push to `main`, or run the **Refresh data and deploy** workflow manually.

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
