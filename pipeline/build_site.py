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
import re
import shutil
from collections import defaultdict
from pathlib import Path

from common import CACHE, OUT, ROOT, load_config, read_json, utcnow, write_json

SITE_SRC = ROOT / "site"
SITE_OUT = ROOT / "_site"

WORD_RX = re.compile(r"[a-z0-9]+")
STOPWORDS = {"and", "of", "the", "in", "for", "to", "a", "on", "with", "by",
             "its", "or", "an", "at", "from", "as", "is"}


def shard_key(issn_l: str) -> str:
    """Detail records are sharded on the ISSN-L prefix, and the browser fetches
    a whole shard to open one journal — so the shards have to stay small.
    Two characters gives 32 shards with the largest at ~9MB (ISSNs are not
    uniformly distributed); four gives ~2,500 shards with a median of a dozen
    records, which is the difference between a 9MB click and a 60KB one."""
    return issn_l[:4]


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
    #
    # Keywords are split into their own file. They are ~75% of the index by
    # size (43k journals x ~30 subject terms), and holding them back keeps the
    # eager load at ~1.7MB gzipped instead of ~5.3MB. The site loads them in
    # the background straight after first paint, so title/publisher/ISSN search
    # works immediately and field search lights up a moment later.
    index = []
    keyword_ids: list[list[int]] = []
    vocab: dict[str, int] = {}
    shards: dict[str, dict] = defaultdict(dict)

    for j in data["journals"]:
        shards[shard_key(j["id"])][j["id"]] = j
        index.append({
            "id": j["id"],
            "t": j["title"],
            "a": j["alt_titles"][:3],
            "p": j["publisher"],
            "i": j["issns"],
            "s": j["deal"]["status"],
            "d": j["in_doaj"],
            "x": bool(j["deal"].get("disputed")),   # sources disagree
            "c": cost_summary(j),
        })
        terms = " ".join((j["scope"]["topics"] or []) +
                         (j["scope"]["subfields"] or []) +
                         (j["scope"]["keywords"] or []))
        # Words already in the title or publisher are scored from those fields
        # anyway, so storing them again buys nothing.
        already = set(WORD_RX.findall(((j["title"] or "") + " " +
                                       (j["publisher"] or "")).lower()))
        words = sorted({w for w in WORD_RX.findall(terms.lower())
                        if len(w) > 2 and w not in STOPWORDS and w not in already})
        keyword_ids.append(sorted(vocab.setdefault(w, len(vocab)) for w in words))

    datadir = SITE_OUT / "data"
    write_json(datadir / "index.json", {"generated": data["generated"],
                                        "sample_data": bool(data.get("sample_data")),
                                        "counts": data["counts"],
                                        "journals": index})
    # Parallel to index.json's journal order.
    write_json(datadir / "keywords.json", {"vocab": list(vocab),
                                           "ids": keyword_ids})
    for key, records in shards.items():
        write_json(datadir / "details" / f"{key}.json", records)

    # ---- status page data
    manifest_path = CACHE / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    # A full refresh touches ~1,600 URLs. The status view shows the newest
    # fetches, so sort by retrieval time rather than by key — an alphabetical
    # slice would be an arbitrary sample labelled "most recent".
    recent = sorted(manifest.items(),
                    key=lambda kv: kv[1].get("retrieved", ""), reverse=True)[:50]
    write_json(datadir / "status.json", {
        "built": utcnow(),
        "dataset_generated": data["generated"],
        "counts": data["counts"],
        "sources_fetched_total": len(manifest),
        "sources_fetched": {k: {"url": v["url"], "retrieved": v["retrieved"]}
                            for k, v in recent},
    })

    changes_path = OUT / "changes.json"
    if changes_path.exists():
        write_json(datadir / "changes.json", read_json(changes_path))

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
