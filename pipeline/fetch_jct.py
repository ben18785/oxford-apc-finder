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

Output: data/out/deals.json
Fixture mode (APC_FIXTURES=1): reads data/fixtures/deals.json instead.
"""
from __future__ import annotations

import sys
import time

import requests

from common import (DATA, FIXTURES, FIXTURES_MODE, Manifest, OUT, fetch_csv,
                    load_config, read_json, utcnow, write_json, normalise_issn)

# Agreements Oxford was in at the last successful run.
KNOWN_OXFORD = DATA / "state" / "oxford_agreements.json"
# Transient Google Docs timeouts are normal at this volume; a large number of
# them is not, and means the source itself is unwell.
MAX_TOLERATED_FAILURES = 15


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

    deals = {
        "generated": utcnow(),
        "institution_ror": ror,
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
