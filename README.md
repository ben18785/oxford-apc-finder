# Oxford Journal article processing charge (APC) Finder

**[ben18785.github.io/oxford-apc-finder](https://ben18785.github.io/oxford-apc-finder/)**

I created this tool because I have often found it hard to understand whether a given journal was covered by Oxford's various deals with publishers etc.

Usually the information I want is ``how much would I have to pay if my article was accepted in this journal if I am the corresponding author?'', and that is what the tool aims to answer.

This is an independent tool. It is not run by, endorsed by, or checked by the
Bodleian Libraries or the University. The [Bodleian's publisher deals page](https://www.bodleian.ox.ac.uk/open-research/open-access-publishing/journal-article/publisher-deals)
is the authoritative source, and <oapayments@bodleian.ox.ac.uk> can give you an
answer you can rely on.

As such, please use this tool to scope out journal options and then email <oapayments@bodleian.ox.ac.uk> to be sure. **I am not responsible for paying for your APC if you act using only the tool and find the information it gives is wrong or misleading.**

## Where the information comes from, and how it decides

Coverage comes from the [Journal Checker Tool](https://journalcheckertool.org/transformative-agreements/)'s
public transformative-agreement data, filtered to Oxford's ROR identifier. A
journal is reported as covered when Oxford appears as a current participant in
an agreement and that agreement's own title list contains the journal. Journal metadata and list prices come from
[OpenAlex](https://openalex.org) and [DOAJ](https://doaj.org); subject
descriptions are generated from OpenAlex's own topic classification. A hand-maintained overlay adds the things the
agreement data does not carry, e.g. percentage discounts, diamond schemes, funder
restrictions, annual caps, each taken from the Bodleian's deals page and
recorded with a link back to it.

Costs are worked out only by simple arithmetic on published prices: a
percentage discount is subtracted from the list price the sources publish, and
where no price is held the site reports this.

Our decision about what monetary value to show is governed by the following:

> "£0" is asserted only where the evidence establishes £0 without depending on
> an unknown fact: about the author, the article, a remaining quota, or a
> disputed or expired agreement.

So an unqualified £0 appears only for diamond and no-APC journals, where the
journal charges authors nothing. A current
agreement shows "£0 if eligible", because the charge is paid for an eligible
corresponding author and the site cannot see whether you are one. An agreement
with other factors including a capped annual allowance, a funder
restriction, or an end date already passed, shows "£0 if eligible, but
confirm first". And where authoritative sources contradict each other, the
site provides no cost information at all and shows both claims.

A journal is listed if any one of these is true: an Oxford deal covers it; it is
in DOAJ; its publisher is one of about 95 vetted publishers, societies and
university presses; it appears in a transformative agreement anywhere in the
world; it is among the 15,000 most-cited journals; it is among the leading
journals within its own subfield; or the site has listed it before. Journals
withdrawn from DOAJ for misconduct-type reasons are excluded outright.

Every one of our decisions is made by hard-coded rules and no language model
is involved anywhere in producing the data; however, it is true that CLAUDE did contribute most of the infrastructure.
The pipeline is a set of Python scripts applying explicit conditions to the sources above. The site is
rebuilt automatically each week, with the previous dataset kept for comparison
so changes are recorded.

## What it cannot tell you

Whether a deal covers your article depends on your corresponding authorship,
article type, funder, licence choice, submission email address and whether the
publisher's annual allowance is still open. The site states the caveats it
knows about but cannot check any of them for you. Prices are list prices as
published by the sources on the date shown, and publishers change them without
notice.

## Something wrong?

This is a new tool. Bugs will happen, and I want to correct them as soon as people notice them. So please do post a [GitHub issue](https://github.com/ben18785/oxford-apc-finder/issues) if you notice one.

Every journal page has a report box that opens a pre-filled
[GitHub issue](https://github.com/ben18785/oxford-apc-finder/issues). Quoting the agreement identifier
shown on the page (e.g. `els2026jisc`) makes a report much easier to handle.

## Sources and licences

- **Journal Checker Tool** transformative-agreement data: CC BY 4.0
- **OpenAlex**: CC0
- **DOAJ** journal metadata: CC0; withdrawal changelog — CC BY-SA 4.0
- **Bodleian publisher deals page**: facts only, linked at every use

Project code is MIT (see `LICENSE`). Building, running and testing the pipeline
is documented in [DEVELOPMENT.md](DEVELOPMENT.md).
