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

import requests

from common import (FIXTURES, FIXTURES_MODE, Manifest, OUT, fetch_csv,
                    load_config, read_json, utcnow, write_json, normalise_issn)


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

    agreements = []
    for row in index_rows:
        esac_id = (row.get("ESAC ID") or "").strip()
        data_url = (row.get("Data URL") or "").strip()
        if not esac_id or not data_url.startswith("http"):
            continue
        try:
            rows = fetch_csv(data_url, manifest, f"jct_ta_{esac_id}", session)
        except RuntimeError as exc:
            print(f"  WARN: agreement {esac_id} unfetchable: {exc}", file=sys.stderr)
            raise  # fail loudly — a silently missing agreement is wrong data

        # Institution block: is Oxford a current participant?
        oxford_current = any(
            (r.get("ROR ID") or "").strip().endswith(ror)
            and not (r.get("Institution Last Seen") or "").strip()
            for r in rows
        )
        if not oxford_current:
            continue

        journals = []
        for r in rows:
            name = (r.get("Journal Name") or "").strip()
            p_issn = normalise_issn(r.get("ISSN (Print)"))
            e_issn = normalise_issn(r.get("ISSN (Online)"))
            last_seen = (r.get("Journal Last Seen") or "").strip()
            if not name and not (p_issn or e_issn):
                continue  # institution-only row
            if last_seen:
                continue  # journal no longer in the agreement
            journals.append({"name": name,
                             "issns": [i for i in (p_issn, e_issn) if i]})
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

    deals = {
        "generated": utcnow(),
        "institution_ror": ror,
        "source": {"index_csv": index_url,
                   "documentation": cfg["sources"]["jct_ta_docs"],
                   "license": "CC BY 4.0"},
        "agreements": agreements,
    }
    write_json(out_path, deals)
    print(f"Done: {len(agreements)} Oxford agreements, "
          f"{sum(a['journal_count'] for a in agreements)} journal entries")


if __name__ == "__main__":
    main()
