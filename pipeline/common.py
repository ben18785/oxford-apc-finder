"""Shared helpers for the data pipeline.

Every fetch is recorded in a provenance manifest (data/cache/manifest.json)
with URL, retrieval timestamp (UTC) and SHA-256 of the payload, so every fact
on the site can point at exactly where it came from and when.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Read KEY=value lines from .env into the environment.

    Lets a local run pick up OPENALEX_API_KEY the same way CI picks it up from
    repo secrets, without exporting anything by hand. Real environment
    variables always win, so CI is unaffected. .env is gitignored.
    """
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DATA = ROOT / "data"
CACHE = DATA / "cache"
CURATED = DATA / "curated"
FIXTURES = DATA / "fixtures"
OUT = DATA / "out"

USER_AGENT = "oxford-apc-finder/0.1 (https://github.com/{repo}; data pipeline)"

# When FIXTURES_MODE is on, fetch stages read from data/fixtures/ instead of
# the network. Used for local development and demo builds; CI runs live.
FIXTURES_MODE = os.environ.get("APC_FIXTURES", "") == "1"


def load_config() -> dict:
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Manifest:
    """Append-only record of everything fetched in a pipeline run."""

    def __init__(self, path: Path = CACHE / "manifest.json"):
        self.path = path
        self.entries: dict[str, dict] = {}
        if path.exists():
            self.entries = json.loads(path.read_text())

    def record(self, key: str, url: str, payload: bytes) -> None:
        self.entries[key] = {
            "url": url,
            "retrieved": utcnow(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=1, sort_keys=True))


def http_get(url: str, *, session: requests.Session | None = None,
             retries: int = 4, timeout: int = 60, params: dict | None = None) -> requests.Response:
    """GET with retry/backoff. Raises on final failure — the pipeline must
    fail loudly, never ship partial data silently."""
    sess = session or requests.Session()
    cfg = load_config()
    headers = {"User-Agent": USER_AGENT.format(repo=cfg["github_repo"])}
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = sess.get(url, headers=headers, timeout=timeout, params=params,
                            allow_redirects=True)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            return resp
        except Exception as exc:  # noqa: BLE001 — retry any transport error
            last_exc = exc
            sleep = 2 ** attempt
            print(f"  retry {attempt + 1}/{retries} for {url} in {sleep}s ({exc})")
            time.sleep(sleep)
    raise RuntimeError(f"Failed to fetch {url}") from last_exc


def fetch_csv(url: str, manifest: Manifest, key: str,
              session: requests.Session | None = None) -> list[dict]:
    resp = http_get(url, session=session)
    resp.raise_for_status()
    manifest.record(key, url, resp.content)
    text = resp.content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def fetch_json(url: str, manifest: Manifest, key: str,
               session: requests.Session | None = None, params: dict | None = None):
    resp = http_get(url, session=session, params=params)
    resp.raise_for_status()
    manifest.record(key, resp.url, resp.content)
    return resp.json()


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=False))
    print(f"  wrote {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")


def read_json(path: Path):
    return json.loads(path.read_text())


def normalise_issn(raw: str | None) -> str | None:
    """Uppercase, insert hyphen if missing, or None if not ISSN-shaped."""
    if not raw:
        return None
    s = raw.strip().upper().replace("–", "-")
    if len(s) == 8 and "-" not in s:
        s = f"{s[:4]}-{s[4:]}"
    if len(s) == 9 and s[4] == "-":
        core = s.replace("-", "")
        if core[:7].isdigit() and (core[7].isdigit() or core[7] == "X"):
            return s
    return None
