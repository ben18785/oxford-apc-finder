"""Collect every URL the site can show a user, for the link checker.

Two classes of link:

  * Infrastructure — the Bodleian pages, JCT agreement CSVs, publisher-deal
    pages named in the curated overlay, the API endpoints behind the "learn
    more" links. There are only a few hundred, and a dead one breaks the same
    link for thousands of journals, so every one is checked every run.

  * Per-journal — homepages and aims-&-scope URLs. There are tens of thousands
    and they belong to third parties, so checking all of them every week would
    be both slow and rude. Instead a deterministic hash-bucket sample is
    checked, and the bucket rotates weekly so the whole set is covered over
    time. Nothing random: same bucket + same data always yields the same list.

Usage:
  python collect_links.py --out links.txt [--buckets 26] [--bucket N]
"""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import yaml

from common import CURATED, OUT, load_config, read_json


def bucket_of(url: str, buckets: int) -> int:
    """Stable bucket for a URL — independent of run order and dataset size."""
    return int(hashlib.sha256(url.encode()).hexdigest()[:8], 16) % buckets


def infrastructure_links(cfg: dict, data: dict) -> set[str]:
    urls: set[str] = set()

    for key in ("bodleian_deals", "bodleian_apc", "bodleian_block_grants",
                "jct_ta_docs", "jct_api", "openalex_api", "doaj_api",
                "doaj_withdrawn_changelog"):
        urls.add(cfg["sources"][key])

    # Licence links shown next to quoted CC BY-SA text — a dead licence link
    # undermines the attribution it exists to provide.
    urls.add("https://creativecommons.org/licenses/by-sa/4.0/")

    overrides = yaml.safe_load((CURATED / "oxford_overrides.yaml").read_text())
    urls.add(overrides["meta"]["source"])
    for entry in overrides["entries"]:
        if entry.get("source_extra"):
            urls.add(entry["source_extra"])

    # Deal provenance: the JCT agreement CSVs, one per Oxford agreement.
    deals = read_json(OUT / "deals.json")
    for agreement in deals["agreements"]:
        urls.add(agreement["data_url"])

    # One live example of each templated API link, so a changed API shape is
    # caught even though the per-journal URLs are built from a pattern.
    for journal in data["journals"][:1]:
        for prov in journal.get("provenance", {}).values():
            for item in (prov if isinstance(prov, list) else [prov]):
                if item.get("url"):
                    urls.add(item["url"])

    return urls


def journal_links(data: dict) -> set[str]:
    urls: set[str] = set()
    for journal in data["journals"]:
        for url in (journal.get("homepage"),
                    (journal.get("scope") or {}).get("aims_url")):
            if url and url.startswith("http"):
                urls.add(url)
        for prov in journal.get("provenance", {}).values():
            for item in (prov if isinstance(prov, list) else [prov]):
                url = item.get("url")
                if url and url.startswith("http"):
                    urls.add(url)
    return urls


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="links.txt", type=Path)
    parser.add_argument("--buckets", type=int, default=26,
                        help="how many weeks to spread the per-journal links over")
    parser.add_argument("--bucket", type=int, default=None,
                        help="which bucket to check (default: derived from the ISO week)")
    args = parser.parse_args()

    cfg = load_config()
    data = read_json(OUT / "journals.json")

    infra = infrastructure_links(cfg, data)
    bucket = (args.bucket if args.bucket is not None
              else datetime.now(timezone.utc).isocalendar().week % args.buckets)
    sampled = {u for u in journal_links(data) - infra
               if bucket_of(u, args.buckets) == bucket}

    all_urls = sorted(infra | sampled)
    args.out.write_text("\n".join(all_urls) + "\n")
    print(f"{len(all_urls)} links to check "
          f"({len(infra)} infrastructure, {len(sampled)} journal links "
          f"from bucket {bucket}/{args.buckets})")


if __name__ == "__main__":
    main()
