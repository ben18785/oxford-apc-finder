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

import re

import yaml

from common import CURATED, OUT, load_config, read_json, utcnow, write_json

MISCONDUCT_PAT = re.compile(
    r"misconduct|best practice|not adhering", re.I)


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
    issns = set(rec.get("issns") or [])
    if entry.get("match_issns") and issns & set(entry["match_issns"]):
        return True
    rx = entry.get("match_publisher_regex")
    if rx and rec.get("publisher") and re.search(rx, rec["publisher"]):
        return True
    return False


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
    openalex: dict = meta["openalex"]
    doaj: dict = meta["doaj"]
    withdrawn: dict = meta["doaj_withdrawn"]

    caveat_entries = [e for e in overrides["entries"] if e["kind"] == "caveat"]
    other_entries = [e for e in overrides["entries"] if e["kind"] != "caveat"]
    bodleian_url = overrides["meta"]["source"]

    journals = []
    excluded_misconduct = 0
    retrieved = meta.get("generated", utcnow())

    for issn_l, rec in openalex.items():
        if rec.get("type") not in (None, "journal"):
            continue
        issns = set(rec.get("issns") or [issn_l])
        doaj_rec = next((doaj[i] for i in issns if i in doaj), None)

        # --- exclusion: misconduct-type DOAJ withdrawal
        wd = next((withdrawn[i] for i in issns if i in withdrawn), None)
        if wd and MISCONDUCT_PAT.search(wd.get("reason", "")):
            excluded_misconduct += 1
            continue

        # --- deal resolution
        deal = next((deal_lookup[i] for i in issns if i in deal_lookup), None)
        status, discount_pct, caveats, deal_sources = "none", None, [], []
        esac_id = None
        if deal:
            status = "covered"
            esac_id = deal["esac_id"]
            deal_sources.append({"label": "JCT agreement data (CC BY 4.0)",
                                 "url": deal["data_url"]})
            if deal["corresponding_author_only"]:
                caveats.append("Corresponding author must be Oxford-affiliated.")
            if deal["end_date"]:
                caveats.append(f"Agreement runs to {deal['end_date']}.")
            for e in caveat_entries:
                if esac_id and esac_id.startswith(e["match_esac_prefix"]):
                    caveats.extend(e.get("caveats", []))
                    if e.get("source_extra"):
                        deal_sources.append({"label": f"{e['publisher_label']} details",
                                             "url": e["source_extra"]})
        else:
            for e in other_entries:
                if match_override(e, rec):
                    kind = e["kind"]
                    if kind == "discount":
                        status, discount_pct = "discount", e.get("pct")
                    elif kind == "diamond":
                        status = "diamond"
                    elif kind in ("green", "note"):
                        status = "none"
                    caveats.extend(e.get("caveats", []))
                    deal_sources.append({"label": "Bodleian publisher deals page",
                                         "url": e.get("source_extra") or bodleian_url})
                    break

        in_doaj = rec.get("is_in_doaj") or bool(doaj_rec)

        # --- inclusion policy
        included = (status != "none") or in_doaj or (
            rec.get("publisher") and allow_rx.search(rec["publisher"]))
        if not included:
            continue

        cost = effective_cost(status, discount_pct, rec, doaj_rec)

        provenance = {
            "metadata": {"label": "OpenAlex (CC0)",
                         "url": f"https://api.openalex.org/sources/issn:{issn_l}",
                         "retrieved": retrieved},
        }
        if doaj_rec:
            provenance["doaj"] = {"label": "DOAJ (metadata CC0)",
                                  "url": f"https://doaj.org/toc/{sorted(issns)[0]}",
                                  "retrieved": retrieved}
        if deal_sources:
            provenance["deal"] = deal_sources
        provenance["oxford"] = {"label": "Bodleian publisher deals page",
                                "url": bodleian_url}

        journals.append({
            "id": issn_l,
            "title": rec.get("title"),
            "alt_titles": rec.get("alternate_titles") or [],
            "issns": sorted(issns),
            "publisher": rec.get("publisher"),
            "homepage": rec.get("homepage"),
            "in_doaj": in_doaj,
            "doaj_withdrawn": wd,
            "deal": {"status": status, "esac_id": esac_id,
                     "discount_pct": discount_pct, "caveats": caveats},
            "cost": cost,
            "scope": {
                "sentence": scope_sentence(rec),
                "topics": [t["name"] for t in (rec.get("topics") or []) if t.get("name")],
                "subfields": sorted({t["subfield"] for t in (rec.get("topics") or [])
                                     if t.get("subfield")}),
                "keywords": (doaj_rec or {}).get("keywords", []),
                "aims_url": (doaj_rec or {}).get("aims_scope_url") or rec.get("homepage"),
            },
            "waiver": bool(doaj_rec and doaj_rec.get("waiver")),
            "provenance": provenance,
        })

    journals.sort(key=lambda j: (j["title"] or "").lower())
    out = {
        "generated": utcnow(),
        "sample_data": bool(deals.get("sample_data")),
        "institution": cfg["institution_name"],
        "counts": {
            "total": len(journals),
            "covered": sum(1 for j in journals if j["deal"]["status"] == "covered"),
            "discount": sum(1 for j in journals if j["deal"]["status"] == "discount"),
            "diamond": sum(1 for j in journals if j["deal"]["status"] == "diamond"),
            "in_doaj": sum(1 for j in journals if j["in_doaj"]),
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
