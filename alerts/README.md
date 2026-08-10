# Deal-change alerts — NOT IN USE

**This is unshipped groundwork. Nothing here runs, and the site asks for
nobody's email address.** There is no subscribe form, no `alerts` key in
`config.yaml`, and no endpoint published to the browser; `tests/test_units.py`
asserts all three, so switching this on is a deliberate act rather than
something that can happen by accident.

It is kept because the code is finished and tested, and the decision that
stopped it was about deliverability and obligations rather than about the
design. Turning it on means working through **Setup** below in full, including
publishing the privacy notice.

Why it was not shipped: sending mail people will actually receive needs a
domain the tool sends from, with aligned SPF/DKIM. Without one, the message
most likely to be filtered is the confirmation — and since nothing is ever sent
to an unconfirmed address, a junked confirmation is a subscription that dies
silently, with no bounce and no way for the maintainer to know. That failure
mode is worse than not offering alerts at all.

---

What it would do: email somebody when a journal they care about gains a deal,
loses one, or its price moves.

## Read this first

Turning this on makes **you** a data controller for other people's email
addresses, on a tool whose own masthead says it is unofficial. That is a real
obligation, not a formality: you must publish a privacy notice, honour deletion
requests, and report a breach if one happens. The design below exists to keep
that obligation as small as it can be.

**Nothing personal touches this repository.** Addresses live only in Cloudflare
KV, reached only by the Worker. They are never in the site, the build, the
Actions logs, or a runner's filesystem. If you later delete the Worker and its
namespace, every address is gone.

## How it fits together

```
browser ──POST /subscribe──► Worker ──► KV (email + watched ISSNs, unconfirmed)
                                │
                                └──► Resend ──► confirmation email
                                                     │
browser ──GET /confirm?t=─────► Worker ──► KV (confirmed)
                                                     
weekly cron ──► Worker ──► reads the SITE's public data/changes.json
                       ──► matches each subscriber's ISSNs (alerts/digest.js)
                       ──► Resend ──► one email each, with an unsubscribe link
```

The Worker reads `changes.json` from the published site, so the alert content is
exactly what the site says — there is no second source of truth to drift.

## Setup

1. **Resend** — create an account, verify a sending domain, make an API key.
2. **Cloudflare**

   ```sh
   cd alerts
   wrangler kv namespace create ALERTS
   wrangler secret put RESEND_API_KEY
   wrangler deploy
   ```

   `wrangler.toml` is committed with two `TODO` markers: paste the namespace id
   the first command prints, and — after the first deploy, once the URL exists —
   set `PUBLIC_ORIGIN` to the Worker's own URL and deploy again. `PUBLIC_ORIGIN`
   builds the unsubscribe links, and the cron run has no request to infer it
   from, so a wrong value means unsubscribe links that go nowhere.
3. Set `FROM` at the top of `worker.js` to an address on your verified domain.
4. Put the Worker's URL into `config.yaml` → `alerts.endpoint`, and push.

## What the design already does for you

| Requirement | How |
|---|---|
| Confirmed opt-in | Nothing is sent to an address until the owner clicks the link. Also stops anyone signing up a colleague. |
| Unsubscribe in every message | `buildEmail` refuses to omit it; a test pins that. |
| Data minimisation | An address and a list of ISSNs. No name, no IP, no analytics. |
| Retention | Unconfirmed requests self-delete after 48 hours (`PENDING_TTL_SECONDS`). |
| No enumeration | `/unsubscribe` says the same thing whether or not the address was on the list. |
| Bounded storage | 100 journals per subscriber; re-subscribing replaces the list rather than growing it. |

## The privacy notice you need to publish

Adapt and link it from the site. This is a starting point, not legal advice.

> **Deal-change alerts — privacy notice**
>
> The Oxford Journal APC Finder is an independent tool run by an individual. It
> is not a service of the Bodleian Libraries or the University of Oxford, and
> the controller for this data is the tool's maintainer, contactable through the
> project's GitHub issues.
>
> **What is held:** your email address and the ISSNs of the journals you asked
> to be told about. Nothing else — no name, no IP address, no tracking.
>
> **Why:** to send you an email when one of those journals changes. Your address
> is used for nothing else and is given to nobody, other than the email provider
> (Resend) needed to deliver the message.
>
> **Your consent:** the lawful basis is consent. Nothing is sent until you
> confirm by clicking a link, and you can withdraw at any time using the
> unsubscribe link in every message.
>
> **How long:** until you unsubscribe. Unconfirmed requests are deleted
> automatically after 48 hours.
>
> **Your rights:** you may ask for a copy of what is held about you, ask for it
> to be corrected, or ask for it to be deleted — unsubscribing does the last of
> these immediately. Complaints can be made to the Information Commissioner's
> Office.

## Testing

`alerts/digest.js` decides what lands in an inbox and is pure, so it is tested
without a network, a database or a Cloudflare account:

```sh
node tests/frontend/digest.test.js
```

The Worker's own I/O is not unit-tested — deploy it and subscribe yourself
first. `wrangler tail` shows the cron run.
