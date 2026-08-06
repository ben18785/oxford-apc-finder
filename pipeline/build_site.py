"""Stage 6 — Build the static site.

Emits into _site/:
  * index.html / app.js / style.css   (copied from site/)
  * data/index.json     — compact search records (loaded eagerly)
  * data/details/XX.json — full journal records, sharded by first 2 chars of
                            ISSN-L (loaded on demand when a journal is opened)
  * data/status.json    — run freshness for the /status view
  * config.json         — site title + GitHub repo for report links
"""
from __future__ import annotations

import json
import shutil
from collections import defaultdict
from pathlib import Path

from common import CACHE, OUT, ROOT, load_config, read_json, utcnow, write_json

SITE_SRC = ROOT / "site"
SITE_OUT = ROOT / "_site"


def cost_summary(j: dict) -> str:
    c = j["cost"]
    kind = c["kind"]
    if kind == "covered":
        return "£0 — covered by Oxford deal"
    if kind == "diamond":
        return "£0 — diamond OA"
    if kind == "no_apc":
        return "No APC"
    if kind == "discount":
        est = c["estimated"]
        return f"~{est['price']:,} {est['currency']} after {c['pct']}% Oxford discount"
    if kind == "discount_unknown_base":
        return f"{c['pct']}% Oxford discount (list price not held)"
    if kind == "list_price":
        return f"{c['list']['price']:,} {c['list']['currency']} (no deal)"
    return "APC unknown"


def main() -> None:
    cfg = load_config()
    data = read_json(OUT / "journals.json")

    if SITE_OUT.exists():
        shutil.rmtree(SITE_OUT)
    shutil.copytree(SITE_SRC, SITE_OUT)

    # ---- compact search index
    index = []
    shards: dict[str, dict] = defaultdict(dict)
    for j in data["journals"]:
        shard_key = j["id"][:2]
        shards[shard_key][j["id"]] = j
        index.append({
            "id": j["id"],
            "t": j["title"],
            "a": j["alt_titles"][:3],
            "p": j["publisher"],
            "i": j["issns"],
            "s": j["deal"]["status"],
            "d": j["in_doaj"],
            "c": cost_summary(j),
            "k": " ".join((j["scope"]["topics"] or []) +
                           (j["scope"]["subfields"] or []) +
                           (j["scope"]["keywords"] or [])).lower(),
        })

    datadir = SITE_OUT / "data"
    write_json(datadir / "index.json", {"generated": data["generated"],
                                        "sample_data": bool(data.get("sample_data")),
                                        "counts": data["counts"],
                                        "journals": index})
    for key, records in shards.items():
        write_json(datadir / "details" / f"{key}.json", records)

    # ---- status page data
    manifest_path = CACHE / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    write_json(datadir / "status.json", {
        "built": utcnow(),
        "dataset_generated": data["generated"],
        "counts": data["counts"],
        "sources_fetched": {k: {"url": v["url"], "retrieved": v["retrieved"]}
                            for k, v in sorted(manifest.items())[:400]},
    })

    write_json(SITE_OUT / "config.json", {
        "title": cfg["site_title"],
        "tagline": cfg["site_tagline"],
        "github_repo": cfg["github_repo"],
        "bodleian_apc": cfg["sources"]["bodleian_apc"],
        "bodleian_deals": cfg["sources"]["bodleian_deals"],
        "bodleian_block_grants": cfg["sources"]["bodleian_block_grants"],
        "contact": "oapayments@bodleian.ox.ac.uk",
    })
    print(f"Site built into _site/ — {len(index)} journals, {len(shards)} detail shards")


if __name__ == "__main__":
    main()
