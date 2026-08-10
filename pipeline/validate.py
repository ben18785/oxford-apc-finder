"""Stage 5 — Validate the merged dataset before it can ship.

Checks:
  1. Schema — required fields, ISSN shapes, known enum values.
  2. Sanity thresholds — the deal-journal count may not fall more than
     config.validation.max_weekly_drop_pct vs the previously shipped dataset.
  3. Oracle — a random sample of journals is cross-checked against the live
     JCT API (api.journalcheckertool.org/ta?issn=&ror=): our "covered" verdict
     must match theirs. Any mismatch fails the build. (Skipped in fixture
     mode, where there is no network.)

Exit code != 0 blocks deploy; the site keeps serving last week's data.
"""
from __future__ import annotations

import os
import random
import re
import sys
from datetime import datetime, timezone

from common import (FIXTURES_MODE, OUT, ROOT, http_get, jct_verdict,
                    load_config, read_json)

# Re-exported: jct_verdict moved to common.py when fetch_jct.py began using it
# too, and tests and callers still reach for it here.
__all__ = ["jct_verdict"]

ISSN_RX = r"^\d{4}-\d{3}[\dX]$"


def check_schema(data: dict) -> list[str]:
    import re
    errors = []
    seen_ids = set()
    for j in data["journals"]:
        jid = j.get("id", "?")
        if jid in seen_ids:
            errors.append(f"duplicate id {jid}")
        seen_ids.add(jid)
        if not j.get("title"):
            errors.append(f"{jid}: missing title")
        for issn in j.get("issns", []):
            if not re.match(ISSN_RX, issn):
                errors.append(f"{jid}: malformed ISSN {issn}")
        if j["deal"]["status"] not in ("covered", "discount", "diamond", "none"):
            errors.append(f"{jid}: bad deal status {j['deal']['status']}")
        if j["cost"]["kind"] not in ("covered", "covered_conditional", "uncertain",
                                     "diamond", "discount",
                                     "discount_unknown_base", "list_price",
                                     "no_apc", "unknown"):
            errors.append(f"{jid}: bad cost kind {j['cost']['kind']}")
        if j["deal"]["status"] == "covered" and not j["provenance"].get("deal"):
            errors.append(f"{jid}: covered but no deal provenance")
    return errors[:50]


def check_cost_claims(data: dict) -> list[str]:
    """The site may assert a settled £0 only where nothing unknown stands in
    the way. This is the one rule whose violation costs a real person real
    money, so it is enforced here as well as implemented in merge.effective_cost
    — a convention held in one function decays the first time someone adds a
    branch; an invariant that fails the build does not.

    Deliberately checked against the *output*, not by reading merge.py: it
    catches the case where the flag is computed correctly and then lost on the
    way to the record, which is exactly how the expired-agreement bug survived
    a code comment saying it must never happen.
    """
    errors = []
    for j in data["journals"]:
        deal, kind = j["deal"], j["cost"]["kind"]
        cond = deal.get("conditions") or {}
        risks = []
        if deal.get("expired"):
            risks.append(f"the agreement expired on {deal['expired']['end_date']}")
        if cond.get("capacity_limited"):
            risks.append("the agreement has a capped annual allowance")
        if cond.get("funders_only"):
            risks.append("coverage is restricted by funder")

        # This mirrors the precedence in merge.effective_cost, deliberately and
        # independently: a conflict outranks every other doubt, because if we
        # cannot say whether the deal is full coverage or a discount there is
        # no figure to qualify. The two implementations agreeing is the point —
        # if they drift, the build stops.
        if deal.get("disputed") and deal["status"] in ("covered", "discount"):
            if kind != "uncertain":
                errors.append(f"{j['id']}: sources disagree about this publisher, "
                              f"so the cost must be 'uncertain', not '{kind}'")
            continue
        if deal["status"] == "covered" and risks:
            if kind != "covered_conditional":
                errors.append(f"{j['id']}: {risks[0]}, so the cost must be "
                              f"'covered_conditional', not '{kind}'")
            continue
        if kind == "covered" and (risks or deal.get("disputed")):
            errors.append(f"{j['id']}: cost is a settled £0 despite {risks[0]}")
    # A blanket count guard as well as the per-journal one: if a future change
    # stops populating the flags altogether, every journal silently passes the
    # checks above while the site goes back to over-claiming on all of them.
    conditional = sum(1 for j in data["journals"]
                      if j["cost"]["kind"] in ("covered_conditional", "uncertain"))
    covered = sum(1 for j in data["journals"] if j["deal"]["status"] == "covered")
    if covered and not conditional:
        errors.append(
            f"{covered:,} journals are covered by an agreement and not one carries "
            "a conditional or uncertain cost. Some always do (expired agreements, "
            "capped allowances, the MDPI conflict), so the risk flags have "
            "probably stopped reaching effective_cost().")
    return errors[:50]


LAST_COUNTS = OUT / "last_counts.json"


def check_thresholds(data: dict, cfg: dict) -> list[str]:
    """Compare against the counts from the last successfully-validated build
    (committed as data/out/last_counts.json), so a sudden drop in coverage
    blocks the deploy for human review rather than silently shipping."""
    if FIXTURES_MODE:
        # The committed baseline describes the real dataset (~43,000 journals).
        # A fixtures build is a dozen hand-picked journals, so the comparison is
        # meaningless and would fail every offline run.
        print("  [fixtures] threshold check skipped (baseline is real data)")
        return []
    if not LAST_COUNTS.exists():
        return []
    prev = read_json(LAST_COUNTS)
    errors = []
    for key in ("covered", "total"):
        old, new = prev.get(key, 0), data["counts"].get(key, 0)
        if old and new < old * (1 - cfg["validation"]["max_weekly_drop_pct"] / 100):
            errors.append(f"count '{key}' dropped {old} → {new} "
                          f"(> {cfg['validation']['max_weekly_drop_pct']}% fall); "
                          "refusing to ship without human review")
    return errors


def check_source_minimums(data: dict, cfg: dict) -> list[str]:
    """Every source must come back roughly the size we expect.

    The week-on-week threshold works on the merged totals, where a source that
    returned half its rows can hide — DOAJ journals are only one of three
    inclusion routes. These floors catch a truncated fetch directly.
    """
    if FIXTURES_MODE:
        # Floors describe the real sources (tens of thousands of records); a
        # fixtures build has a dozen journals and would fail every one.
        print("  [fixtures] source minimums skipped")
        return []
    counts = data.get("source_counts") or {}
    errors = []
    for key, floor in (cfg["validation"].get("min_source_counts") or {}).items():
        actual = counts.get(key)
        if actual is None:
            errors.append(f"source count '{key}' missing — cannot verify the "
                          "fetch was complete")
        elif actual < floor:
            errors.append(f"source '{key}' returned {actual:,}, below the floor "
                          f"of {floor:,}; the fetch was probably truncated")
    return errors


def check_overlay_is_live(data: dict, overrides: dict | None = None) -> list[str]:
    """Every curated overlay entry must reach at least one journal.

    Two entries silently matched nothing for the life of the project because
    their esac_id prefixes were wrong, so caveats like "Disease Models &
    Mechanisms is NOT covered" never appeared. A dead entry is invisible — it
    looks exactly like a caveat that simply did not apply.

    An entry that is legitimately unreachable must say so explicitly, with
    `expect_no_match: true` and a comment explaining why.
    """
    if FIXTURES_MODE and overrides is None:
        # Most overlay entries legitimately match nothing in a dozen fixture
        # journals. The logic is unit-tested directly instead.
        print("  [fixtures] overlay liveness skipped")
        return []

    if overrides is None:
        import yaml
        from common import CURATED
        overrides = yaml.safe_load((CURATED / "oxford_overrides.yaml").read_text())
    journals = data["journals"]
    errors = []

    for entry in overrides["entries"]:
        kind = entry["kind"]
        label = entry.get("publisher_label", "?")
        if entry.get("expect_no_match"):
            continue

        if kind == "caveat":
            prefix = entry["match_esac_prefix"]
            hits = sum(1 for j in journals
                       if (j["deal"].get("esac_id") or "").startswith(prefix))
        elif kind == "conflict":
            hits = sum(1 for j in journals if j["deal"].get("disputed"))
        elif kind == "correction":
            # A correction that matches nothing is the most dangerous dead
            # entry of the lot: it means JCT's coverage claim is standing
            # unchallenged, and the site is asserting a deal the library has
            # explicitly told us does not exist.
            prefix = entry["match_esac_prefix"]
            hits = sum(1 for j in journals if (j["deal"].get("correction") or {})
                       .get("publisher") == entry.get("publisher_label"))
        else:
            issns = set(entry.get("match_issns") or [])
            rx = entry.get("match_publisher_regex")
            hits = sum(
                1 for j in journals
                if (issns & set(j["issns"]))
                or (rx and j["publisher"] and re.search(rx, j["publisher"])))

        if hits == 0:
            errors.append(
                f"overlay entry {kind}/{label!r} matches no journal — it is "
                "doing nothing. Fix the matcher, or set expect_no_match: true "
                "with a comment saying why.")
    return errors


def check_must_include(data: dict, must: dict | None = None) -> list[str]:
    """Named journals that have to be present, whatever the automatic rules do.

    The rules are automatic and therefore have blind spots. The publisher
    allowlist omitted the Institute of Mathematical Statistics, so all four
    Annals titles were missing from the site entirely — a researcher searching
    for them saw nothing at all, which reads as a broken tool rather than a
    coverage limit. This is the tripwire for that class of gap.
    """
    if must is None:
        if FIXTURES_MODE:
            # The fixture set is a dozen journals; it cannot contain Nature.
            print("  [fixtures] must-include check skipped")
            return []
        import yaml
        from common import CURATED
        must = yaml.safe_load((CURATED / "must_include.yaml").read_text())

    present = set()
    for j in data["journals"]:
        present.add(j["id"])
        present.update(j.get("issns") or [])

    missing = [f"{e['title']} ({e['issn']})" for e in must["journals"]
               if e["issn"] not in present]
    if not missing:
        return []
    return [f"{len(missing)} journal(s) that must always be listed are absent: "
            + "; ".join(missing[:8])
            + ". The inclusion rules need widening — see "
              "data/curated/must_include.yaml."]


def check_oracle(data: dict, cfg: dict) -> list[str]:
    if FIXTURES_MODE:
        print("  [fixtures] oracle check skipped (no network)")
        return []
    if os.environ.get("APC_SKIP_ORACLE") == "1":
        print("  oracle check skipped (APC_SKIP_ORACLE=1)")
        return []
    ror = cfg["institution_ror"]
    api = cfg["sources"]["jct_api"]

    # Seeded on the date so a failing build can be re-run against the same
    # sample once the data is fixed, while still rotating week to week.
    rng = random.Random(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    n = cfg["validation"]["oracle_sample_size"]
    covered = [j for j in data["journals"] if j["deal"]["status"] == "covered"]
    uncovered = [j for j in data["journals"] if j["deal"]["status"] != "covered"]

    # Stratified BY AGREEMENT, one journal from each.
    #
    # A uniform sample of 25 from 12,537 covered journals gave BMJ's 36 titles
    # about a 3% chance of being looked at in any given week — so when every
    # one of them turned out to be an over-claim, the fault could have sat in
    # the data for months before a sample happened to land on it. Faults are
    # almost never per-journal: an agreement is parsed wrongly, or JCT changes
    # its mind about a whole deal. Sampling per agreement matches the shape of
    # the failure, and catches an agreement-wide fault on the very next run.
    #
    # Costs one request per agreement (~42) rather than ~12. The JCT API is
    # free and this runs once a week.
    by_agreement: dict[str, list] = {}
    for j in covered:
        by_agreement.setdefault(j["deal"].get("esac_id") or "(none)", []).append(j)
    sample = [rng.choice(sorted(group, key=lambda x: x["id"]))
              for _, group in sorted(by_agreement.items())]
    # Plus uncovered journals, which is where under-claims show up.
    sample += rng.sample(uncovered, min(n // 2, len(uncovered)))
    print(f"  oracle: 1 journal from each of {len(by_agreement)} agreements "
          f"+ {min(n // 2, len(uncovered))} uncovered")

    def jct_covers(issn: str):
        resp = http_get(f"{api}/ta", params={"issn": issn, "ror": ror}, retries=2)
        if resp.status_code != 200:
            return None
        try:
            return jct_verdict(resp.json())
        except ValueError:
            return None

    # The two directions of disagreement carry very different risk, so they are
    # judged differently:
    #
    #   we say covered, JCT says not  — we would tell someone publishing is free
    #                                   when it is not. Never acceptable.
    #   we say not, JCT says covered  — we under-claim. Unhelpful, but nobody is
    #                                   billed by surprise, and it is what a
    #                                   renamed journal produces: OpenAlex keeps
    #                                   the former title as its own source, the
    #                                   agreement lists only the current one,
    #                                   and JCT's API resolves the old ISSN to
    #                                   the new journal.
    over_claims, under_claims, inconclusive = [], [], []
    for j in sample:
        # What the SITE claims, not what the status field says. A journal whose
        # cost is `uncertain` shows no figure and both sources' claims side by
        # side — it is already telling the reader that JCT and Oxford disagree,
        # so JCT disagreeing is the thing it says, not an over-claim.
        #
        # `covered_conditional` still counts as claiming coverage: "£0 if
        # eligible, but confirm" is a hedged assertion, and if JCT says there
        # is no agreement at all then the hedge is not the problem.
        ours = (j["deal"]["status"] == "covered"
                and j["cost"]["kind"] != "uncertain")
        # JCT indexes an agreement's journals under the specific ISSN the
        # agreement lists — often the online one. Querying only the first ISSN
        # reports a false mismatch for any journal listed under another. Ask
        # about the rest only when the first answer disagrees with ours, so the
        # normal case still costs one request per sampled journal.
        theirs = jct_covers(j["issns"][0])
        if theirs != ours:
            for issn in j["issns"][1:]:
                if jct_covers(issn) is True:
                    theirs = True
                    break

        if theirs is None:
            inconclusive.append(j["id"])
            continue
        if ours == theirs:
            continue
        note = (f"{j['id']} {j['title']}: we say covered={ours}, "
                f"JCT API says {theirs} (checked {', '.join(j['issns'])})")
        (over_claims if ours else under_claims).append(note)

    print(f"  oracle: {len(sample)} sampled, {len(over_claims)} over-claim(s), "
          f"{len(under_claims)} under-claim(s), "
          f"{len(inconclusive)} inconclusive")
    for note in under_claims:
        print(f"    under-claim: {note}")

    errors = list(over_claims)
    allowed = cfg["validation"]["max_oracle_under_claims"]
    if len(under_claims) > allowed:
        errors.append(
            f"{len(under_claims)} of {len(sample)} sampled journals are covered "
            f"according to the live JCT API but not in our data (limit "
            f"{allowed}). One or two are usually renamed journals; this many "
            "suggests the agreement data is not being read correctly: "
            + "; ".join(under_claims[:3]))
    # An oracle that could not read most of its answers has not cross-checked
    # anything, and silently passing would be worse than the crash it replaced:
    # the build would look verified when nothing was verified.
    if len(inconclusive) > max(2, len(sample) // 4):
        errors.append(
            f"{len(inconclusive)} of {len(sample)} oracle checks returned no "
            "usable answer from the JCT API, so coverage was effectively not "
            "cross-checked this build. Either the API is unwell or its response "
            f"shape has changed again: {', '.join(inconclusive[:5])}")
    return errors


def check_api_contradictions_are_applied(data: dict, deals: dict | None = None) -> list[str]:
    """An agreement the JCT API contradicts must not still be quoting a price.

    The downgrade happens in merge.py, three files away from the fetch stage
    that discovers the contradiction. If the two ever drift apart the result is
    silent and expensive: the site keeps saying £0 for journals we have already
    established we cannot vouch for, and nothing anywhere reports an error.

    Also fails when the circuit breaker is stuck. A tripped breaker means the
    site is running on remembered verdicts, which is correct for one bad API
    day and not correct as a standing state.
    """
    if deals is None:
        deals = read_json(OUT / "deals.json")
    verdicts = (deals or {}).get("api_verdicts") or {}
    bad = {e for e, v in verdicts.items() if v.get("verdict") == "contradicts"}
    errors = []
    if not verdicts and not FIXTURES_MODE:
        return ["deals.json carries no api_verdicts block, so no agreement was "
                "cross-checked against the live JCT API this run."]
    for j in data["journals"]:
        deal = j.get("deal") or {}
        if deal.get("esac_id") in bad and j["cost"]["kind"] != "uncertain":
            errors.append(
                f"{j['id']} {j.get('title','')}: agreement {deal['esac_id']} is "
                f"contradicted by the JCT API but the journal still states "
                f"cost={j['cost']['kind']}. The downgrade in merge.py is not "
                "being applied.")
            if len(errors) >= 3:
                break
    if (deals or {}).get("api_circuit_breaker_tripped"):
        errors.append(
            "The JCT API cross-check circuit breaker tripped: too many "
            "agreements looked contradicted at once to be believable, so this "
            "build is running on the previous run's verdicts. Fine once; if it "
            "repeats, the API contract has changed and probe_agreement needs "
            "rewriting.")
    return errors


def main() -> None:
    cfg = load_config()
    data = read_json(OUT / "journals.json")
    failures = (check_schema(data)
                + check_cost_claims(data)
                + check_thresholds(data, cfg)
                + check_source_minimums(data, cfg)
                + check_overlay_is_live(data)
                + check_api_contradictions_are_applied(data)
                + check_must_include(data)
                + check_oracle(data, cfg))
    if failures:
        print("VALIDATION FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    # Record this run's counts as the baseline for next week's threshold check.
    from common import write_json
    write_json(LAST_COUNTS, data["counts"])
    print(f"Validation passed: {data['counts']['total']} journals, "
          f"{data['counts']['covered']} covered.")


if __name__ == "__main__":
    main()
