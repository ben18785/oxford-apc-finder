# Journal article processing charge (APC) Finder

**[ben18785.github.io/oxford-apc-finder](https://ben18785.github.io/oxford-apc-finder/)**

I created this tool because I have often found it hard to understand whether publishing open access in a given journal was covered by Oxford's various deals with publishers etc.

Usually the information I want is ``how much would I have to pay if my article was accepted in this journal if I am the corresponding author?'', and that is what the tool aims to answer.

The site opens on the University of Oxford, the one university it covers in
detail. Researchers elsewhere in the UK can choose their own university under
**My university**; see [Other universities](#other-universities) below for
what that does and does not tell you.

This is an independent tool. It is not run by, endorsed by, or checked by any
university or library, including the Bodleian Libraries and the University of
Oxford. For Oxford, the [Bodleian's publisher deals page](https://www.bodleian.ox.ac.uk/open-research/open-access-publishing/journal-article/publisher-deals)
is the authoritative source, and <oapayments@bodleian.ox.ac.uk> can give you an
answer you can rely on.

As such, please use this tool to scope out journal options and then email <oapayments@bodleian.ox.ac.uk> (or, elsewhere, your own library's open access team) to be sure. **I am not responsible for paying for your APC if you act using only the tool and find the information it gives is wrong or misleading.**

## Where the information comes from

For every journal, whichever university is chosen:

- **Journal metadata and list prices** come from [OpenAlex](https://openalex.org)
  and [DOAJ](https://doaj.org); subject descriptions are generated from
  OpenAlex's own topic classification.
- **Which agreements cover which journals** comes from the
  [Journal Checker Tool](https://journalcheckertool.org/transformative-agreements/)'s
  public transformative-agreement data, read for each university by its
  [ROR](https://ror.org) identifier.

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

What the site then tells you depends on the university chosen under
**My university**: Oxford, the default, or any other.

## At Oxford: deals and costs

A journal is reported as covered when Oxford appears as a current participant
in an agreement and that agreement's own title list contains the journal. A
hand-maintained overlay adds the things the agreement data does not carry,
e.g. percentage discounts, diamond schemes, funder restrictions, annual caps,
and agreements the Bodleian lists that the Journal Checker Tool does not, each
taken from the Bodleian's deals page and recorded with a link back to it.
Where the two disagree, the overlay corrects the agreement data or the site
shows both claims.

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

## Other universities

Choosing another university under **My university** (the list holds about 150
UK universities, found by typing part of the name) changes two things, and
neither is a price:

- a warning, naming the university, on every page: the site holds no details
  of that university's publisher deals beyond what the Journal Checker Tool
  lists, nobody at its library has checked them, and that data has been wrong
  for Oxford in the direction that costs an author money;
- an amber "Agreement listed — ask your library" flag on journals in an
  agreement the university takes part in, with the agreement identifier to
  quote to the library.

Costs are shown only where a journal charges nobody (diamond open access);
every other journal reads "depends on your institution". Flagged journals are
never counted, sorted or filtered as free. Choosing "another university (not
listed)" gives the same view without the flags. The list is built from the
UK-wide Jisc agreements in the Journal Checker Tool data, restricted to
universities by their [ROR](https://ror.org) records.

A link can open the site on a particular university: add `?inst=` and the
university's ROR identifier, e.g. `?inst=04v2twj65` for Oxford Brookes.

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
- **ROR** organisation registry (used to name and select universities): CC0

Project code is MIT (see `LICENSE`). Building, running and testing the pipeline
is documented in [DEVELOPMENT.md](DEVELOPMENT.md).
