"""List the URLs a pull request adds, for link-check.yml.

A PR can only change the links written into the repo — the site's pages and
scripts, the curated overlay, config.yaml. The per-journal links come from the
weekly refresh and no PR can touch them, so checking those on a PR fails it
for rot on the live site that has nothing to do with the change.

    python .github/scripts/added_links.py BASE_REF > pr-links.txt

Prints one URL per line, sorted, from lines the PR adds under the checked
paths. Template placeholders (`${...}`) and local or example hosts are left
out: they are not addresses anyone can visit.
"""
from __future__ import annotations

import re
import subprocess
import sys

PATHS = ["site", "data/curated", "config.yaml"]
URL = re.compile(r"https?://[^\s\"'<>`()\[\]{}\\|^]+")
TRAILING = ".,;:!?*"
SKIP_HOSTS = re.compile(r"^https?://(localhost|127\.0\.0\.1|[^/]*\bexample\.(org|com))\b")


def added_lines(diff: str) -> list[str]:
    return [line[1:] for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")]


def urls_in(lines: list[str]) -> list[str]:
    found = set()
    for line in lines:
        for m in URL.finditer(line):
            # A URL cut short by a placeholder is not a real address.
            if line[m.end():m.end() + 2] == "${" or line[m.end():m.end() + 1] == "{":
                continue
            url = m.group(0).rstrip(TRAILING)
            if url.count("://") == 1 and not SKIP_HOSTS.match(url) and "." in url.split("/")[2]:
                found.add(url)
    return sorted(found)


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    r = subprocess.run(["git", "diff", "--unified=0", f"{base}...HEAD", "--", *PATHS],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # Usually a shallow clone: the base branch is not there to diff against.
        print(f"git diff against {base} failed (is the checkout shallow?):\n{r.stderr}",
              file=sys.stderr)
        return 1
    diff = r.stdout
    for url in urls_in(added_lines(diff)):
        print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
