"""Stage 4 — Merge everything into the canonical journal dataset.

Inclusion policy (design doc §4): a journal enters the dataset iff
  (1) covered by an Oxford deal (JCT or curated overlay), or
  (2) listed in DOAJ, or
  (3) its publisher is on the curated allowlist,
EXCEPT journals withdrawn from DOAJ for misconduct-type reasons, which are
excluded outright.

Every record carries per-fact provenance (source name, URL, retrieved date).
Scope text is a deterministic template over OpenAlex topics — no LLM.

Output: data/out/journals.json
"""
from __future__ import annotations

import datetime
import html
import re

import yaml

from common import (CURATED, OUT, known_journal_issns, load_config,
                    normalise_issn, read_json, utcnow, write_json)

MISCONDUCT_PAT = re.compile(
    r"misconduct|best practice|not adhering", re.I)


def agreement_expired(end_date: str | None, today: datetime.date) -> dict | None:
    """Has the agreement's stated end date passed?

    JCT lists an agreement for as long as the institution is a participant, and
    renewals are recorded late — so an expired end date does not mean coverage
    has stopped. It does mean the site must stop presenting "£0" as settled.
    """
    if not end_date:
        return None
    try:
        end = datetime.date.fromisoformat(end_date.strip())
    except ValueError:
        return None
    if end >= today:
        return None
    return {"end_date": end_date.strip(), "days": (today - end).days}


def clean_text(s: str | None) -> str | None:
    """Undo HTML encoding in upstream text.

    A handful of OpenAlex titles store the encoded form ("ACS ES&amp;T Water",
    "Nature Clinical Practice Endocrinology &#38; Metabolism"). The site escapes
    for display, so without this the entity is escaped a second time and the
    user sees the raw '&amp;'.
    """
    if not s:
        return s
    return html.unescape(s).strip()


def resolve_publisher(rec: dict, doaj_rec: dict | None) -> str | None:
    """Best available publisher name, preferring OpenAlex and falling back to DOAJ.

    OpenAlex leaves host_organization_name empty for ~9,000 journals, including
    one of the four Lancet Regional Health titles. Because the overlay matches
    discounts on publisher name, that gap silently turned "15% Oxford discount"
    into "no Oxford deal" for a journal sitting beside three identical siblings.
    DOAJ knows the publisher for the great majority of them.
    """
    return clean_text(rec.get("publisher")) or clean_text((doaj_rec or {}).get("publisher"))


def build_deal_lookup(deals: dict) -> dict[str, dict]:
    """issn -> {agreement info}"""
    lookup: dict[str, dict] = {}
    for a in deals["agreements"]:
        for j in a["journals"]:
            for issn in j["issns"]:
                lookup[issn] = {
                    "esac_id": a["esac_id"],
                    "end_date": a["end_date"],
                    "corresponding_author_only": a["corresponding_author_only"],
                    "data_url": a["data_url"],
                }
    return lookup


def load_overrides() -> dict:
    return yaml.safe_load((CURATED / "oxford_overrides.yaml").read_text())


def match_override(entry: dict, rec: dict) -> bool:
    return override_match_reason(entry, rec) is not None


def override_match_reason(entry: dict, rec: dict) -> str | None:
    """How this overlay entry matched — or None if it didn't.

    Worth surfacing: an ISSN match is exact, a publisher-name match is a
    heuristic that can catch the wrong imprint. A reader disputing a result
    deserves to know which kind they are looking at.
    """
    issns = set(rec.get("issns") or [])
    if entry.get("match_issns") and issns & set(entry["match_issns"]):
        return "its ISSN is listed explicitly"
    rx = entry.get("match_publisher_regex")
    if rx and rec.get("publisher") and re.search(rx, rec["publisher"]):
        return f"its publisher is recorded as {rec['publisher']}"
    return None


def coverage_basis(status: str, *, esac_id: str | None = None,
                   agreement_count: int = 0, scheme: str | None = None,
                   match_reason: str | None = None,
                   discount_pct: int | None = None,
                   not_in_agreement: str | None = None) -> str:
    """One plain sentence saying *why* this journal got this answer.

    Without it the two commonest verdicts explain nothing: "covered" reduces to
    "a spreadsheet says so", and "no deal" is indistinguishable from "we have no
    data". A reader who disagrees needs something specific to dispute.
    """
    if status == "covered":
        return (f"This journal appears on the title list of transformative "
                f"agreement {esac_id}, which Oxford is a current participant in. "
                "Both facts come from the Journal Checker Tool's agreement data, "
                "linked below.")
    if status == "diamond":
        return (f"Oxford supports {scheme}, so publishing here is free to "
                f"authors. Matched because {match_reason}."
                if match_reason else
                f"Oxford supports {scheme}, so publishing here is free to authors.")
    if status == "discount" and not_in_agreement:
        return (f"This title is not on the {not_in_agreement} read-and-publish "
                f"agreement's list, so the APC is not covered outright — but "
                f"Oxford has a {discount_pct}% discount on that publisher's fully "
                "open access journals.")
    if status == "discount":
        return (f"Oxford has a {discount_pct}% discount arrangement with "
                f"{scheme}, listed on the Bodleian's publisher deals page. "
                f"Matched because {match_reason}."
                if match_reason else
                f"Oxford has a {discount_pct}% discount arrangement with {scheme}.")
    return (f"Checked against the title lists of all {agreement_count} "
            "agreements Oxford participates in, and against the discount and "
            "diamond schemes on the Bodleian's deals page — this journal and "
            "its publisher appear in none of them. That is not the same as the "
            "journal being ineligible for support: block grants or funder "
            "routes may still apply.")


def browse_links(rec: dict, doaj_rec: dict | None) -> list[dict]:
    """Where to go and read what this journal actually publishes.

    Deliberately links out rather than fetching article titles: listing three
    real titles per journal would cost one OpenAlex request each (~$4.29 for a
    full pass, against a $1/day free allowance), and these routes are already
    paid for by data we hold.
    """
    links = []
    if doaj_rec and doaj_rec.get("doaj_url"):
        links.append({"label": "Recent articles, in DOAJ",
                      "url": doaj_rec["doaj_url"]})
    # Every journal has an OpenAlex source id, so every journal gets at least
    # one route to its own output.
    source_id = (rec.get("openalex_id") or "").rsplit("/", 1)[-1]
    if source_id.startswith("S"):
        links.append({
            "label": "All articles indexed in OpenAlex",
            "url": ("https://openalex.org/works?filter=primary_location."
                    f"source.id:{source_id}")})
    homepage = rec.get("homepage")
    if homepage and homepage != (doaj_rec or {}).get("aims_scope_url"):
        links.append({"label": "The journal's own site", "url": homepage})
    return links


def oa_status(rec: dict, doaj_rec: dict | None, in_doaj: bool) -> str:
    """How you publish here, which decides what a price even means.

    Every APC figure the site holds is the cost of publishing *open access*,
    never the cost of publishing at all. In a fully open access journal those
    are the same thing. In a hybrid journal publishing is free behind the
    paywall and the APC buys openness — so showing the number unqualified
    reads as "it costs £8,490 to publish in Nature", which is wrong.
    """
    if in_doaj or rec.get("is_oa"):
        return "gold"
    if rec.get("apc_prices") or (doaj_rec or {}).get("apc", {}).get("price"):
        return "hybrid"
    return "subscription"


def superseded(rec: dict, today: datetime.date, quiet_years: int = 4) -> dict | None:
    """Has this record stopped publishing?

    OpenAlex keeps the predecessor as its own source when a journal is renamed
    or changes publisher, so the site lists both. The old one is not "a journal
    with no Oxford deal" — it is a journal you cannot submit to.
    """
    last = rec.get("last_active_year")
    if not last or last >= today.year - quiet_years:
        return None
    return {"last_active_year": last, "quiet_years": today.year - last}


def scope_sentence(rec: dict) -> str | None:
    topics = rec.get("topics") or []
    if not topics:
        return None
    names = [t["name"] for t in topics[:3] if t.get("name")]
    fields = sorted({t["field"] for t in topics[:3] if t.get("field")})
    if not names:
        return None
    s = "Publishes primarily in " + ", ".join(names)
    if fields:
        s += f" (field{'s' if len(fields) > 1 else ''}: {', '.join(fields)})"
    wc = rec.get("works_count")
    if wc:
        s += f". {wc:,} articles indexed in OpenAlex"
    return s + "."


def effective_cost(deal_status: str, discount_pct, rec: dict, doaj_rec) -> dict:
    """Deterministic cost summary. Never invents a number."""
    list_prices = rec.get("apc_prices") or []
    doaj_price = None
    if doaj_rec and doaj_rec["apc"]["has_apc"] and doaj_rec["apc"]["price"]:
        doaj_price = {"price": doaj_rec["apc"]["price"],
                      "currency": doaj_rec["apc"]["currency"]}
    if deal_status == "covered":
        return {"kind": "covered",
                "note": "APC covered by the Oxford agreement (subject to the caveats shown)."}
    if deal_status == "diamond":
        return {"kind": "diamond", "note": "Free to publish (diamond OA)."}
    if doaj_rec and not doaj_rec["apc"]["has_apc"]:
        return {"kind": "no_apc", "note": "No APC (per DOAJ)."}
    base = doaj_price or (
        {"price": list_prices[0]["price"], "currency": list_prices[0]["currency"]}
        if list_prices else None)
    if deal_status == "discount" and discount_pct and base:
        return {"kind": "discount", "pct": discount_pct, "list": base,
                "estimated": {"price": round(base["price"] * (100 - discount_pct) / 100),
                              "currency": base["currency"]},
                "note": f"{discount_pct}% Oxford discount applied to list price."}
    if deal_status == "discount" and discount_pct:
        return {"kind": "discount_unknown_base", "pct": discount_pct,
                "note": f"{discount_pct}% Oxford discount; list price not held — check the journal's page."}
    if base:
        return {"kind": "list_price", "list": base,
                "note": "List price — no Oxford deal applies."}
    return {"kind": "unknown", "note": "No published APC price held — check the journal's page."}


def main() -> None:
    cfg = load_config()
    deals = read_json(OUT / "deals.json")
    meta = read_json(OUT / "metadata.json")
    overrides = load_overrides()
    allow = yaml.safe_load((CURATED / "publisher_allowlist.yaml").read_text())["publishers"]
    allow_rx = re.compile("|".join(f"(?:{re.escape(p)})" for p in allow), re.I)

    deal_lookup = build_deal_lookup(deals)
    # Inclusion route 4: in a transformative agreement somewhere in the world.
    ta_worldwide = set(deals.get("agreement_issns_worldwide") or [])
    # Inclusion route 5: among the most-cited journals in the world.
    top_cited = set(meta.get("top_cited") or [])
    # Inclusion route 7: leading in its own subfield, however small that field.
    top_by_subfield = set(meta.get("top_by_subfield") or [])
    # Inclusion route 6: it was on the site before. Makes coverage monotonic —
    # the misconduct exclusion still overrides, since that runs first.
    remembered = known_journal_issns()
    openalex: dict = meta["openalex"]
    doaj: dict = meta["doaj"]
    withdrawn: dict = meta["doaj_withdrawn"]

    universal_criteria = overrides.get("universal_criteria") or []
    caveat_entries = [e for e in overrides["entries"] if e["kind"] == "caveat"]
    conflict_entries = [e for e in overrides["entries"] if e["kind"] == "conflict"]
    other_entries = [e for e in overrides["entries"]
                     if e["kind"] not in ("caveat", "conflict")]
    bodleian_url = overrides["meta"]["source"]

    today = datetime.date.today()
    journals = []
    excluded_misconduct = 0
    retrieved = meta.get("generated", utcnow())

    for issn_l, rec in openalex.items():
        if rec.get("type") not in (None, "journal"):
            continue
        # OpenAlex's issn array is not always clean — some records carry
        # strings like "ISSN-L: 2992-7862". Normalise and drop anything that
        # isn't ISSN-shaped, keeping issn_l (the key) regardless.
        issns = {i for i in (normalise_issn(x) for x in (rec.get("issns") or []))
                 if i}
        issns.add(issn_l)
        doaj_rec = next((doaj[i] for i in issns if i in doaj), None)

        # Resolve the publisher once, and shadow it onto the record so every
        # downstream decision — overlay matching, the allowlist, the displayed
        # name — sees the same answer rather than only OpenAlex's view.
        rec = {**rec, "publisher": resolve_publisher(rec, doaj_rec)}

        # --- exclusion: misconduct-type DOAJ withdrawal
        wd = next((withdrawn[i] for i in issns if i in withdrawn), None)
        if wd and MISCONDUCT_PAT.search(wd.get("reason", "")):
            excluded_misconduct += 1
            continue

        # --- deal resolution
        deal = next((deal_lookup[i] for i in issns if i in deal_lookup), None)
        status, discount_pct, caveats, deal_sources = "none", None, [], []
        esac_id, expired = None, None
        basis = coverage_basis("none", agreement_count=len(deals["agreements"]))
        if deal:
            status = "covered"
            esac_id = deal["esac_id"]
            basis = coverage_basis("covered", esac_id=esac_id)
            # Conditions the Bodleian page attaches to every deal.
            caveats.extend(universal_criteria)
            deal_sources.append({"label": "JCT agreement data (CC BY 4.0)",
                                 "url": deal["data_url"]})
            if deal["corresponding_author_only"]:
                caveats.append("Corresponding author must be Oxford-affiliated.")
            expired = agreement_expired(deal["end_date"], today)
            if expired:
                # JCT keeps listing an agreement after its end date — renewals
                # are recorded late — so this is a warning, not a reason to
                # drop the coverage claim. But "£0" must never be shown as
                # settled fact once the stated end date has passed.
                caveats.append(
                    f"This agreement's end date ({deal['end_date']}) has passed "
                    f"{expired['days']} days ago. It may have been renewed — the "
                    "Journal Checker Tool still lists Oxford as a participant — "
                    "but confirm before submitting.")
            elif deal["end_date"]:
                caveats.append(f"Agreement runs to {deal['end_date']}.")
            for e in caveat_entries:
                if esac_id and esac_id.startswith(e["match_esac_prefix"]):
                    caveats.extend(e.get("caveats", []))
                    if e.get("source_extra"):
                        deal_sources.append({"label": f"{e['publisher_label']} details",
                                             "url": e["source_extra"]})
        else:
            # Journal-or-publisher-specific overlay entries win: they are the
            # more precise statement about this title.
            for e in other_entries:
                reason = override_match_reason(e, rec)
                if reason:
                    kind = e["kind"]
                    label = e.get("publisher_label") or "a listed scheme"
                    if kind == "discount":
                        status, discount_pct = "discount", e.get("pct")
                        basis = coverage_basis("discount", scheme=label,
                                               discount_pct=discount_pct,
                                               match_reason=reason)
                    elif kind == "diamond":
                        status = "diamond"
                        basis = coverage_basis("diamond", scheme=label,
                                               match_reason=reason)
                    elif kind in ("green", "note"):
                        status = "none"
                        basis = (f"No APC deal, but Oxford has an arrangement with "
                                 f"{label} worth knowing about — see below. "
                                 f"Matched because {reason}.")
                    caveats.extend(e.get("caveats", []))
                    deal_sources.append({"label": "Bodleian publisher deals page",
                                         "url": e.get("source_extra") or bodleian_url})
                    break

            # A publisher can be in a hybrid agreement AND offer Oxford a
            # discount on its fully-gold titles. Those gold journals aren't in
            # the agreement's journal list, so they fall through to here —
            # apply the `also_discount` on that publisher's caveat entry.
            # The Bodleian's wording is specific: these discounts apply to the
            # publisher's "fully gold open access journals". Matching on
            # publisher alone handed a 15% discount to 3,637 subscription
            # journals — including Nature Protocols, where publishing is free
            # unless you choose OA, so the discount implied a cost that does
            # not exist.
            fully_oa = bool(rec.get("is_oa") or rec.get("is_in_doaj"))
            if status == "none" and not deal_sources and not fully_oa:
                for e in caveat_entries:
                    ad = e.get("also_discount") or {}
                    rx = ad.get("match_publisher_regex")
                    if rx and rec.get("publisher") and re.search(rx, rec["publisher"]):
                        basis = (
                            f"Not on the {e['publisher_label']} read-and-publish "
                            "agreement's title list. Oxford's discount with that "
                            f"publisher applies to its fully open access journals, "
                            "and this is a subscription title — so there may be no "
                            "open access charge at all unless you choose to pay one. "
                            "Check the journal's own publishing-model page.")
                        deal_sources.append({"label": "Bodleian publisher deals page",
                                             "url": bodleian_url})
                        break

            if status == "none" and not deal_sources and fully_oa:
                for e in caveat_entries:
                    ad = e.get("also_discount") or {}
                    rx = ad.get("match_publisher_regex")
                    if rx and rec.get("publisher") and re.search(rx, rec["publisher"]):
                        status, discount_pct = "discount", ad["pct"]
                        basis = coverage_basis(
                            "discount", discount_pct=ad["pct"],
                            not_in_agreement=e["publisher_label"])
                        caveats.append(
                            f"{ad['pct']}% Oxford discount on {ad['applies_to']} — "
                            f"this title is not in the {e['publisher_label']} "
                            f"read-and-publish agreement.")
                        deal_sources.append({"label": "Bodleian publisher deals page",
                                             "url": bodleian_url})
                        if e.get("source_extra"):
                            deal_sources.append({"label": f"{e['publisher_label']} details",
                                                 "url": e["source_extra"]})
                        break

        # --- source conflicts: our sources disagree about this journal, so
        # say so rather than silently presenting one of them as fact.
        disputed = None
        for e in conflict_entries:
            by_esac = (esac_id and e.get("match_esac_prefix")
                       and esac_id.startswith(e["match_esac_prefix"]))
            if by_esac or match_override(e, rec):
                disputed = {
                    "publisher": e.get("publisher_label"),
                    "note": (e.get("note") or "").strip(),
                    "jct_says": e.get("jct_says"),
                    "bodleian_says": e.get("bodleian_says"),
                }
                deal_sources.append({"label": "Bodleian publisher deals page",
                                     "url": e.get("source_extra") or bodleian_url})
                break

        in_doaj = rec.get("is_in_doaj") or bool(doaj_rec)

        # --- inclusion policy
        included = (status != "none") or in_doaj or issn_l in top_cited \
            or issn_l in remembered or issn_l in top_by_subfield \
            or bool(issns & ta_worldwide) or (
            rec.get("publisher") and allow_rx.search(rec["publisher"]))
        if not included:
            continue

        cost = effective_cost(status, discount_pct, rec, doaj_rec)
        access = oa_status(rec, doaj_rec, in_doaj)
        dormant = superseded(rec, today)

        provenance = {
            "metadata": {"label": "OpenAlex (CC0)",
                         "url": f"https://api.openalex.org/sources/issn:{issn_l}",
                         "retrieved": retrieved},
        }
        if doaj_rec:
            # DOAJ's own canonical URL for the journal, straight from the bulk
            # export — /toc/<issn> guesses are not always resolvable.
            provenance["doaj"] = {"label": "DOAJ (metadata CC0)",
                                  "url": doaj_rec.get("doaj_url")
                                         or f"https://doaj.org/toc/{sorted(issns)[0]}",
                                  "retrieved": retrieved}
        if deal_sources:
            provenance["deal"] = deal_sources
        provenance["oxford"] = {"label": "Bodleian publisher deals page",
                                "url": bodleian_url}

        journals.append({
            "id": issn_l,
            "title": clean_text(rec.get("title")),
            "alt_titles": [clean_text(t) for t in (rec.get("alternate_titles") or [])],
            "issns": sorted(issns),
            "publisher": rec.get("publisher"),
            "homepage": rec.get("homepage"),
            "in_doaj": in_doaj,
            "oa_status": access,
            "superseded": dormant,
            "doaj_withdrawn": wd,
            "deal": {"status": status, "esac_id": esac_id,
                     "discount_pct": discount_pct, "caveats": caveats,
                     "disputed": disputed, "expired": expired,
                     "basis": basis},
            "cost": cost,
            "scope": {
                "sentence": scope_sentence(rec),
                "topics": [t["name"] for t in (rec.get("topics") or []) if t.get("name")],
                "subfields": sorted({t["subfield"] for t in (rec.get("topics") or [])
                                     if t.get("subfield")}),
                "keywords": (doaj_rec or {}).get("keywords", []),
                "aims_url": (doaj_rec or {}).get("aims_scope_url") or rec.get("homepage"),
            },
            "browse": browse_links(rec, doaj_rec),
            "waiver": bool(doaj_rec and doaj_rec.get("waiver")),
            "provenance": provenance,
        })

    journals.sort(key=lambda j: (j["title"] or "").lower())
    out = {
        "generated": utcnow(),
        "sample_data": bool(deals.get("sample_data")),
        "institution": cfg["institution_name"],
        "source_counts": {**(meta.get("counts") or {}),
                          "agreements": len(deals["agreements"])},
        "counts": {
            "total": len(journals),
            "covered": sum(1 for j in journals if j["deal"]["status"] == "covered"),
            "discount": sum(1 for j in journals if j["deal"]["status"] == "discount"),
            "diamond": sum(1 for j in journals if j["deal"]["status"] == "diamond"),
            "in_doaj": sum(1 for j in journals if j["in_doaj"]),
            "disputed": sum(1 for j in journals if j["deal"].get("disputed")),
            "expired": sum(1 for j in journals if j["deal"].get("expired")),
            "excluded_misconduct": excluded_misconduct,
        },
        "journals": journals,
    }
    write_json(OUT / "journals.json", out)
    print(f"Merged {len(journals)} journals "
          f"({out['counts']['covered']} covered, {out['counts']['discount']} discount, "
          f"{out['counts']['diamond']} diamond; {excluded_misconduct} excluded for "
          f"misconduct-type DOAJ withdrawal)")


if __name__ == "__main__":
    main()
