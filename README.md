# Oxford Journal APC Finder

**[ben18785.github.io/oxford-apc-finder](https://ben18785.github.io/oxford-apc-finder/)**

Search for a journal and see whether the University of Oxford's open-access
agreements cover it, what publishing there is likely to cost once any deal is
applied, and a link to the source of every fact shown.

This is an independent tool. It is not run by, endorsed by, or checked by the
Bodleian Libraries or the University, and it is not an entitlement decision —
the [Bodleian's publisher deals page](https://www.bodleian.ox.ac.uk/open-research/open-access-publishing/journal-article/publisher-deals)
is the authoritative source, and <oapayments@bodleian.ox.ac.uk> can give you an
answer you can rely on.

## Where the information comes from, and how it decides

Coverage comes from the [Journal Checker Tool](https://journalcheckertool.org/transformative-agreements/)'s
public transformative-agreement data, filtered to Oxford's ROR identifier. A
journal is reported as covered when Oxford appears as a current participant in
an agreement *and* that agreement's own title list contains the journal; both
facts read straight from the agreement data, and both shown on the page so
either can be challenged. Journal metadata and list prices come from
[OpenAlex](https://openalex.org) and [DOAJ](https://doaj.org); subject
descriptions are generated from OpenAlex's own topic classification rather than
publisher marketing copy. A hand-maintained overlay adds the things the
agreement data does not carry, e.g. percentage discounts, diamond schemes, funder
restrictions, annual caps, each taken from the Bodleian's deals page and
recorded with a link back to it.

Costs are worked out by simple arithmetic on published prices, never guessed: a
covered journal shows £0, a percentage discount is subtracted from the list
price the sources publish, and where no price is held the site says so rather
than estimating one. Journals enter the dataset only if
they are covered by a deal, listed in DOAJ, or published by a vetted publisher,
and those withdrawn from DOAJ for misconduct-type reasons are excluded outright. Where sources contradict each other, or where an agreement's stated
end date has passed, the journal carries a visible warning showing both claims
rather than picking one.

**Every one of these decisions is made by hard-coded rules and no language model
is involved anywhere in producing the data.** The pipeline is a set of Python
scripts applying explicit conditions to the sources above: string matching on
ISSNs, date comparisons, a curated YAML file of Oxford-specific facts, and
templated sentences. The same inputs always give the same output, and any
result can be traced to the rule and the source that produced it. The site is
rebuilt automatically each week, with the previous dataset kept for comparison
so changes are recorded rather than silently overwritten.

## What it cannot tell you

Whether a deal covers *your* article depends on your corresponding authorship,
article type, funder, licence choice, submission email address and whether the
publisher's annual allowance is still open. The site surfaces the caveats it
knows about but cannot check any of them for you. Prices are list prices as
published by the sources on the date shown, and publishers change them without
notice.

## Something wrong?

Every journal page has a report box that opens a pre-filled
[GitHub issue](https://github.com/ben18785/oxford-apc-finder/issues); reports
are public and corrections are applied openly. Quoting the agreement identifier
shown on the page (e.g. `els2026jisc`) makes a report much easier to act on.

## Sources and licences

- **Journal Checker Tool** transformative-agreement data — CC BY 4.0
- **OpenAlex** — CC0
- **DOAJ** journal metadata — CC0; withdrawal changelog — CC BY-SA 4.0
- **Bodleian publisher deals page** — facts only, linked at every use

Project code is MIT (see `LICENSE`). Building, running and testing the pipeline
is documented in [DEVELOPMENT.md](DEVELOPMENT.md).
