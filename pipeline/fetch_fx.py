"""Stage 2b — exchange rates, so costs in 46 currencies can be compared.

Without this, "sort by cost" is worse than not having it. The most expensive
APC on the site by raw number is 150,000,000 IRR — about £2,400 — while Nature
at $12,290 would rank far below it. An ordering that is really an ordering of
denominations would be actively misleading, which is the one thing this site is
built not to be.

Rates are the European Central Bank's daily reference rates, via Frankfurter.
Chosen because they are published by a central bank rather than a broker, are
dated, are free of charge and keys, and can be cited on the page — the same
standard every other fact here is held to.

The ECB publishes ~30 currencies, which covers 6,800 of the 7,448 priced
journals. The remaining 648 (IRR, UAH, IQD, RUB, EGP and others) get no
comparable figure and are excluded from cost ordering rather than guessed at.

Output data/state/fx_rates.json is committed, so a failed fetch falls back to
the last good set rather than silently dropping the feature — and the date is
shown to the reader either way.

No LLM: this is a rate table and a division.
"""
from __future__ import annotations

import sys

from common import DATA, Manifest, http_get, read_json, utcnow, write_json

# Frankfurter serves the ECB reference rates. The .app host now 301s to .dev.
FX_URL = "https://api.frankfurter.dev/v1/latest?base=GBP"
FX_FILE = DATA / "state" / "fx_rates.json"
SOURCE_LABEL = "European Central Bank reference rates (via Frankfurter)"
SOURCE_URL = "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html"

# A rate table this far out of date is still far better than no ordering at
# all — APCs move by more in a year than any of these currencies do in a month
# — but the reader should be told, so the age is published rather than hidden.
STALE_AFTER_DAYS = 30


def main() -> None:
    previous = read_json(FX_FILE) if FX_FILE.exists() else None
    manifest = Manifest()
    try:
        resp = http_get(FX_URL)
        resp.raise_for_status()
        payload = resp.json()
        rates = payload["rates"]
        if not isinstance(rates, dict) or len(rates) < 20:
            raise ValueError(f"only {len(rates)} rates returned; expected ~30")
        # GBP is the base, so it is absent from the table the API returns.
        rates["GBP"] = 1.0
        manifest.record("fx_rates", FX_URL, resp.content)
        write_json(FX_FILE, {
            "base": "GBP",
            "date": payload["date"],
            "retrieved": utcnow(),
            "source": {"label": SOURCE_LABEL, "url": SOURCE_URL},
            "rates": {k: v for k, v in sorted(rates.items())},
        })
        print(f"Exchange rates: {len(rates)} currencies, ECB date {payload['date']}")
    except Exception as exc:                        # noqa: BLE001
        # Never fail the build over this. Cost ordering is a convenience; the
        # journal data is the product. Keeping yesterday's rates is strictly
        # better than dropping the feature, and merge publishes the date.
        if previous:
            print(f"Could not refresh exchange rates ({exc}); keeping the set "
                  f"from {previous.get('date')}.", file=sys.stderr)
        else:
            print(f"Could not fetch exchange rates ({exc}) and none are cached. "
                  "Costs will not be orderable this build.", file=sys.stderr)


if __name__ == "__main__":
    main()
