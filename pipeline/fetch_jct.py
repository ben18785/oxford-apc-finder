"""Stage 1 — Which journals are covered by an Oxford transformative agreement?

Deterministic recipe (data: cOAlition S Journal Checker Tool public TA data,
CC BY 4.0, https://journalcheckertool.org/transformative-agreements/):

  1. Download the agreements index CSV (ESAC ID, End Date, C/A Only, Data URL).
  2. Download every agreement's own CSV. Its rows carry BOTH a journal block
     (cols: Journal Name, ISSN (Print), ISSN (Online), Journal First Seen,
     Journal Last Seen) and an institution block (Institution Name, ROR ID,
     Institution First Seen, Institution Last Seen).
  3. An agreement applies to Oxford iff an institution row has our ROR and no
     "Institution Last Seen" date (a Last Seen date means it left the list).
  4. Collect that agreement's journals the same way (no "Journal Last Seen").
  5. Ask JCT's own live API whether it agrees. It has been observed not to:
     for BMJ and Thieme the agreement CSV lists Oxford as a current
     participant while the API reports no coverage for any journal in the
     agreement. Neither side can be preferred, so merge states no price for
     those. See verify_against_api.

Output: data/out/deals.json (with an api_verdicts block)
        data/state/jct_api_verdicts.json
Fixture mode (APC_FIXTURES=1): reads data/fixtures/deals.json instead.
"""
from __future__ import annotations

import sys
import time

import requests

from common import (DATA, FIXTURES, FIXTURES_MODE, Manifest, OUT, fetch_csv,
                    http_get, jct_verdict, load_config, read_json, utcnow,
                    write_json, normalise_issn)

# Agreements Oxford was in at the last successful run.
KNOWN_OXFORD = DATA / "state" / "oxford_agreements.json"
# Last run's answers from the JCT API, kept so a bad API day cannot rewrite the
# site's claims (see verify_against_api).
API_VERDICTS = DATA / "state" / "jct_api_verdicts.json"
# Transient Google Docs timeouts are normal at this volume; a large number of
# them is not, and means the source itself is unwell.
MAX_TOLERATED_FAILURES = 15

# How many journals to ask about before concluding an agreement is contradicted.
# The first "no" is usually enough to be interesting but not enough to be sure:
# a single journal can be renamed, or split, or simply absent from the API's
# index, and condemning a whole agreement on one answer would be reckless.
# Three consecutive noes is a property of the agreement, not of a journal.
PROBES_PER_AGREEMENT = 3
# Above this share of contradicted agreements, believe the API is unwell rather
# than believing Oxford lost most of its deals overnight. Mass-downgrading the
# whole site to "we cannot tell" on the strength of an outage would be its own
# kind of wrong answer, and a far more visible one.
CIRCUIT_BREAKER_SHARE = 0.25


def unfetchable_verdict(failed: list[str], known_oxford: set[str],
                        max_tolerated: int = MAX_TOLERATED_FAILURES,
                        have_baseline: bool = True) -> str | None:
    """What to do about agreements that could not be fetched.

    Returns an error message if the run must abort, or None to continue with a
    warning. The distinction that matters: losing an agreement Oxford is in
    silently removes coverage from real journals, while losing one of the other
    ~560 only thins the worldwide inclusion net, which is additive.

    Membership only appears inside the agreement CSV, so a failed fetch cannot
    be classified from the index alone. Without a baseline from a previous run
    we genuinely do not know which it was, and must assume the worst — one of
    the first observed failures was in fact Oxford's Thieme agreement.
    """
    if not failed:
        return None
    if not have_baseline:
        return ("Could not fetch agreement(s) " + ", ".join(sorted(failed)) +
                ", and there is no record of which agreements Oxford is in "
                "(data/state/oxford_agreements.json is missing). Refusing to "
                "guess: one of these may be an Oxford agreement whose journals "
                "would silently lose coverage.")
    oxford_hits = sorted(set(failed) & known_oxford)
    if oxford_hits:
        return ("Could not fetch agreement(s) Oxford participates in: "
                f"{', '.join(oxford_hits)}. Refusing to ship a dataset that "
                "would silently drop their journals.")
    if len(failed) > max_tolerated:
        return (f"{len(failed)} agreements unfetchable ({', '.join(sorted(failed))}) "
                "— beyond the tolerance for transient failures; this looks like a "
                "systemic problem with the source.")
    return None


def institution_is_current(rows: list[dict], ror: str) -> bool:
    """Is this institution a *current* participant in the agreement?

    An agreement CSV interleaves journal rows and institution rows. An
    institution counts only if its ROR appears with no "Institution Last Seen"
    date — a date there means it has left the agreement.
    """
    return any(
        (r.get("ROR ID") or "").strip().endswith(ror)
        and not (r.get("Institution Last Seen") or "").strip()
        for r in rows
    )


def agreement_journals(rows: list[dict]) -> list[dict]:
    """Journals currently in the agreement (a "Journal Last Seen" date means
    the journal has been dropped from it)."""
    journals = []
    for r in rows:
        name = (r.get("Journal Name") or "").strip()
        p_issn = normalise_issn(r.get("ISSN (Print)"))
        e_issn = normalise_issn(r.get("ISSN (Online)"))
        if not name and not (p_issn or e_issn):
            continue  # institution-only row
        if (r.get("Journal Last Seen") or "").strip():
            continue  # journal no longer in the agreement
        journals.append({"name": name,
                         "issns": [i for i in (p_issn, e_issn) if i]})
    return journals


def probe_agreement(journals, api, ror, session=None, probes=PROBES_PER_AGREEMENT):
    """Does the JCT API agree that this agreement covers Oxford?

    Returns "agrees", "contradicts", or "unknown", plus the evidence.

    JCT publishes two things that can disagree: the agreement CSVs, which are
    the input to this pipeline, and the live API, which is what a researcher
    consults. They have been observed to contradict each other for an entire
    agreement at once — Oxford listed as a current participant in the CSV,
    while the API reports no coverage for every journal in it. Both cannot be
    right, and we cannot tell which is, so the honest output is neither.

    One "yes" ends the probe: coverage anywhere in the agreement means the API
    knows about it, and the disagreement is then per-journal, which is normal
    (renamed titles, ISSN changes) and not what this is looking for.
    """
    seen, checked = [], []
    for issn in journals:
        if len(checked) >= probes:
            break
        if not issn or issn in seen:
            continue
        seen.append(issn)
        try:
            resp = http_get(f"{api}/ta", params={"issn": issn, "ror": ror},
                            session=session, retries=2)
            verdict = jct_verdict(resp.json()) if resp.status_code == 200 else None
        except Exception:                      # noqa: BLE001 - never fatal here
            verdict = None
        checked.append({"issn": issn, "covered": verdict})
        if verdict is True:
            return "agrees", checked
    if not checked or all(c["covered"] is None for c in checked):
        return "unknown", checked           # asked, got no usable answer
    if any(c["covered"] is None for c in checked):
        return "unknown", checked           # a partial no is not a verdict
    return "contradicts", checked


def verify_against_api(agreements, cfg, session=None):
    """Cross-check every Oxford agreement against the live JCT API.

    This is deliberately a FETCH stage and not a validation one. A
    contradiction is not a reason to refuse to publish: the previous build
    carries the same claim, so halting freezes a site that is already wrong
    while blocking every unrelated improvement in the run. It is a reason to
    publish something weaker — merge turns a contradicted agreement's journals
    into `uncertain`, which shows both claims and asserts no price.

    Costs one call per agreement in the normal case (~42), which is less than
    the sampled oracle in validate.py already spends.
    """
    api = cfg["sources"]["jct_api"]
    ror = cfg["institution_ror"]
    previous = (read_json(API_VERDICTS) or {}).get("agreements", {}) \
        if API_VERDICTS.exists() else {}

    verdicts, calls = {}, 0
    print("Cross-checking agreements against the live JCT API …")
    for a in agreements:
        issns = [i for j in a["journals"] for i in (j.get("issns") or [])]
        verdict, evidence = probe_agreement(issns, api, ror, session)
        calls += len(evidence)
        verdicts[a["esac_id"]] = {"verdict": verdict, "checked": utcnow(),
                                  "evidence": evidence}
        if verdict != "agrees":
            print(f"  {verdict.upper():12} {a['esac_id']} "
                  f"({a['journal_count']} journals)")
        time.sleep(0.2)

    decided = [v for v in verdicts.values() if v["verdict"] != "unknown"]
    bad = [v for v in decided if v["verdict"] == "contradicts"]
    share = len(bad) / len(decided) if decided else 0.0
    tripped = share > CIRCUIT_BREAKER_SHARE

    if tripped:
        # Keep last run's answers rather than acting on this run's. Falling
        # back to "agrees" would republish every over-claim; acting on the
        # readings would blank the site. The previous verdicts are the only
        # option that is wrong in neither direction.
        print(f"  CIRCUIT BREAKER: {len(bad)} of {len(decided)} agreements look "
              f"contradicted ({share:.0%}), which is not credible. Treating this "
              "as a JCT API fault and keeping the previous run's verdicts.",
              file=sys.stderr)
        for esac, prev in previous.items():
            if esac in verdicts:
                verdicts[esac] = dict(prev, stale=True)
        for v in verdicts.values():
            v.setdefault("stale", True)

    write_json(API_VERDICTS, {"generated": utcnow(),
                              "circuit_breaker_tripped": tripped,
                              "agreements": verdicts})
    counts = {k: sum(1 for v in verdicts.values() if v["verdict"] == k)
              for k in ("agrees", "contradicts", "unknown")}
    print(f"  {counts['agrees']} agree, {counts['contradicts']} contradict, "
          f"{counts['unknown']} no answer ({calls} API calls)")
    return verdicts, tripped


def main() -> None:
    cfg = load_config()
    out_path = OUT / "deals.json"

    if FIXTURES_MODE:
        deals = read_json(FIXTURES / "deals.json")
        print(f"[fixtures] loaded {len(deals['agreements'])} agreements")
        write_json(out_path, deals)
        return

    manifest = Manifest()
    session = requests.Session()
    ror = cfg["institution_ror"]
    index_url = cfg["sources"]["jct_ta_index_csv"]

    print("Fetching JCT agreements index …")
    index_rows = fetch_csv(index_url, manifest, "jct_index", session)
    print(f"  {len(index_rows)} agreements in index")

    # Which agreements Oxford was in last time. A transient fetch failure on an
    # agreement Oxford participates in must stop the build — silently dropping
    # one removes coverage from real journals. A failure on any of the other
    # ~560 only thins the worldwide inclusion net, which is additive, so it is
    # not worth discarding a 50-minute run for.
    known_oxford = set()
    if KNOWN_OXFORD.exists():
        known_oxford = set(read_json(KNOWN_OXFORD).get("esac_ids") or [])

    agreements = []
    failed: list[tuple[str, str]] = []
    # Journals in ANY transformative agreement worldwide. Oxford's own
    # agreements are a small slice of these, but a journal that a national
    # consortium has negotiated over is by definition from an established
    # publisher — which makes this a self-maintaining inclusion signal, and it
    # costs nothing extra because every agreement CSV is downloaded anyway to
    # check whether Oxford is a participant.
    worldwide: set[str] = set()

    index_by_id = {(r.get("ESAC ID") or "").strip(): r for r in index_rows}

    for row in index_rows:
        esac_id = (row.get("ESAC ID") or "").strip()
        data_url = (row.get("Data URL") or "").strip()
        if not esac_id or not data_url.startswith("http"):
            continue
        try:
            rows = fetch_csv(data_url, manifest, f"jct_ta_{esac_id}", session)
        except Exception:  # noqa: BLE001
            # Any failure at all: a read timeout, or a 400 from the
            # googleusercontent host these links redirect to, which http_get
            # does not retry because a 4xx is normally permanent. Here it is
            # not — the same URL succeeds moments later. Catching only
            # RuntimeError let that 400 escape and kill an hour-long run.
            failed.append((esac_id, data_url))
            continue

        for journal in agreement_journals(rows):
            worldwide.update(journal["issns"])

        if not institution_is_current(rows, ror):
            continue

        journals = agreement_journals(rows)
        agreements.append({
            "esac_id": esac_id,
            "end_date": (row.get("End Date") or "").strip(),
            "corresponding_author_only": (row.get("C/A Only") or "").strip().lower() in ("yes", "y", "true"),
            "last_reviewed": (row.get("Last Reviewed") or "").strip(),
            "data_url": data_url,
            "journal_count": len(journals),
            "journals": journals,
        })
        print(f"  Oxford ✓ {esac_id}: {len(journals)} journals")

    # --- second pass: transient failures usually succeed after a pause
    if failed:
        print(f"\n{len(failed)} agreement(s) failed on the first pass; retrying …")
        time.sleep(20)
        still_failing = []
        for esac_id, data_url in failed:
            try:
                rows = fetch_csv(data_url, manifest, f"jct_ta_{esac_id}", session)
            except Exception as exc:  # noqa: BLE001
                still_failing.append((esac_id, exc))
                continue
            for journal in agreement_journals(rows):
                worldwide.update(journal["issns"])
            if institution_is_current(rows, ror):
                journals = agreement_journals(rows)
                agreements.append({
                    "esac_id": esac_id,
                    "end_date": (index_by_id[esac_id].get("End Date") or "").strip(),
                    "corresponding_author_only":
                        (index_by_id[esac_id].get("C/A Only") or "").strip().lower()
                        in ("yes", "y", "true"),
                    "last_reviewed": (index_by_id[esac_id].get("Last Reviewed") or "").strip(),
                    "data_url": data_url,
                    "journal_count": len(journals),
                    "journals": journals,
                })
                print(f"  Oxford ✓ {esac_id}: {len(journals)} journals (on retry)")

        if still_failing:
            names = [e for e, _ in still_failing]
            problem = unfetchable_verdict(names, known_oxford,
                                          have_baseline=KNOWN_OXFORD.exists())
            if problem:
                raise RuntimeError(problem)
            print(f"  WARNING: {len(names)} non-Oxford agreement(s) unfetchable "
                  f"({', '.join(names)}). Their journals are missing from the "
                  "worldwide inclusion net for this run only.", file=sys.stderr)

    # Record which agreements are Oxford's, so the next run knows which fetch
    # failures it must not tolerate.
    write_json(KNOWN_OXFORD, {"generated": utcnow(),
                              "esac_ids": sorted({a["esac_id"] for a in agreements})})

    api_verdicts, breaker = verify_against_api(agreements, cfg, session)

    deals = {
        "generated": utcnow(),
        "institution_ror": ror,
        "api_verdicts": api_verdicts,
        "api_circuit_breaker_tripped": breaker,
        "source": {"index_csv": index_url,
                   "documentation": cfg["sources"]["jct_ta_docs"],
                   "license": "CC BY 4.0"},
        "agreements": agreements,
        "agreement_issns_worldwide": sorted(worldwide),
    }
    write_json(out_path, deals)
    print(f"Done: {len(agreements)} Oxford agreements, "
          f"{sum(a['journal_count'] for a in agreements)} journal entries; "
          f"{len(worldwide)} distinct ISSNs across all {len(index_rows)} agreements worldwide")


if __name__ == "__main__":
    main()
