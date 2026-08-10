/* Deal-change alerts. A Cloudflare Worker, deployed separately from the site.
 *
 * WHY THIS IS NOT A GITHUB ACTION
 * -------------------------------
 * Email addresses are personal data. Keeping them here means they never enter
 * the repository, the Actions logs, or a runner's filesystem — the site stays
 * a static build with nothing personal in it, and the blast radius of anything
 * going wrong with the site is unchanged. This Worker is the only component
 * that ever sees an address.
 *
 * WHAT IT DOES
 *   POST /subscribe    {email, issns:[...]}   -> stores unconfirmed, emails a link
 *   GET  /confirm?t=   -> activates the subscription
 *   GET  /unsubscribe?t= -> deletes it
 *   cron               -> reads the site's changes.json, emails whoever is affected
 *
 * Nothing is sent to an address until the owner has clicked the confirmation
 * link. That is required for unsolicited email under PECR, and it is also the
 * only defence against someone signing up a colleague as a joke.
 *
 * SETUP
 *   wrangler kv namespace create ALERTS
 *   wrangler secret put RESEND_API_KEY
 *   wrangler deploy
 * See alerts/README.md, which also carries the privacy notice you must publish.
 */

import { matchChanges, buildEmail } from "./digest.js";

const SITE = "https://ben18785.github.io/oxford-apc-finder/";
const CHANGES_URL = SITE + "data/changes.json";
const FROM = "Oxford APC Finder <alerts@your-domain.example>";

// An unconfirmed subscription is an address nobody has agreed to us holding,
// so it expires on its own rather than sitting in the store indefinitely.
const PENDING_TTL_SECONDS = 60 * 60 * 48;
// One person cannot watch the entire site: an unbounded list would make every
// weekly email a full changelog, and the store unbounded with it.
const MAX_WATCHED = 100;

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json",
               "access-control-allow-origin": "*",
               "access-control-allow-headers": "content-type" },
  });

const page = (text) =>
  new Response(`<!doctype html><meta charset="utf-8">
    <title>Oxford APC Finder alerts</title>
    <body style="font:17px/1.5 Georgia,serif;max-width:34rem;margin:4rem auto;padding:0 1rem">
    <p>${text}</p><p><a href="${SITE}">Back to the Oxford APC Finder</a></p>`,
    { headers: { "content-type": "text/html; charset=utf-8" } });

const token = () =>
  [...crypto.getRandomValues(new Uint8Array(24))]
    .map((b) => b.toString(16).padStart(2, "0")).join("");

/* Addresses are keyed by hash, not stored in the key, so the list of keys is
 * not itself a list of who has signed up. */
async function emailKey(email) {
  const digest = await crypto.subtle.digest(
    "SHA-256", new TextEncoder().encode(email.trim().toLowerCase()));
  return "email:" + [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, "0")).join("");
}

const looksLikeEmail = (s) =>
  typeof s === "string" && s.length <= 254 && /^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(s);

const cleanIssns = (list) =>
  [...new Set((Array.isArray(list) ? list : [])
    .filter((i) => typeof i === "string" && /^\d{4}-\d{3}[\dX]$/i.test(i))
    .map((i) => i.toUpperCase()))].slice(0, MAX_WATCHED);

async function sendMail(env, to, subject, text) {
  const r = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { authorization: `Bearer ${env.RESEND_API_KEY}`,
               "content-type": "application/json" },
    body: JSON.stringify({ from: FROM, to, subject, text }),
  });
  if (!r.ok) throw new Error(`Resend ${r.status}: ${(await r.text()).slice(0, 200)}`);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return json({});

    if (url.pathname === "/subscribe" && request.method === "POST") {
      let body;
      try { body = await request.json(); } catch { return json({ error: "bad json" }, 400); }
      const email = (body.email || "").trim();
      const issns = cleanIssns(body.issns);
      if (!looksLikeEmail(email)) return json({ error: "that does not look like an email address" }, 400);
      if (!issns.length) return json({ error: "star at least one journal first" }, 400);

      // Re-subscribing replaces the watch list rather than accumulating one,
      // and re-confirming is required each time: consent is for a list, not
      // for an address in perpetuity.
      const t = token();
      await env.ALERTS.put(`pending:${t}`,
        JSON.stringify({ email, issns, at: Date.now() }),
        { expirationTtl: PENDING_TTL_SECONDS });
      const link = `${url.origin}/confirm?t=${t}`;
      await sendMail(env, email, "Confirm your Oxford APC Finder alerts",
        ["Somebody — probably you — asked for email alerts when Oxford's open",
         "access deals change for these journals:", "",
         ...issns.map((i) => `  ${i}`), "",
         "Confirm by opening this link. Nothing is sent until you do:",
         link, "",
         "If this was not you, ignore this message. The request expires in 48",
         "hours and your address is then deleted.", "",
         "This is an unofficial tool, not a Bodleian Libraries service."].join("\n"));
      return json({ ok: true, pending: true });
    }

    if (url.pathname === "/confirm") {
      const t = url.searchParams.get("t") || "";
      const raw = await env.ALERTS.get(`pending:${t}`);
      if (!raw) return page("That confirmation link has expired or has already been used.");
      const sub = JSON.parse(raw);
      const key = await emailKey(sub.email);
      const existing = await env.ALERTS.get(key);
      if (existing) await env.ALERTS.delete(`sub:${existing}`);
      const st = token();
      await env.ALERTS.put(`sub:${st}`, JSON.stringify(
        { email: sub.email, issns: sub.issns, confirmed: Date.now() }));
      await env.ALERTS.put(key, st);
      await env.ALERTS.delete(`pending:${t}`);
      return page(`Confirmed — you will get an email when any of your
        ${sub.issns.length} journals change. Every message carries a link to stop them.`);
    }

    if (url.pathname === "/unsubscribe") {
      const t = url.searchParams.get("t") || "";
      const raw = await env.ALERTS.get(`sub:${t}`);
      if (raw) {
        await env.ALERTS.delete(`sub:${t}`);
        await env.ALERTS.delete(await emailKey(JSON.parse(raw).email));
      }
      // Same wording either way: whether an address was on the list is not
      // something an unauthenticated caller should be able to probe for.
      return page("Unsubscribed. You will not get any more alerts from this tool.");
    }

    return json({ error: "not found" }, 404);
  },

  async scheduled(event, env, ctx) {
    const changes = await (await fetch(CHANGES_URL)).json();
    if (!changes || changes.baseline) return;   // first build has nothing to diff

    let cursor, sent = 0;
    do {
      const list = await env.ALERTS.list({ prefix: "sub:", cursor });
      for (const k of list.keys) {
        const sub = JSON.parse(await env.ALERTS.get(k.name));
        const matches = matchChanges(changes, sub.issns);
        if (!matches.length) continue;          // silence is correct for no news
        const t = k.name.slice("sub:".length);
        const { subject, body } = buildEmail(matches, {
          site: SITE,
          unsubscribe: `${env.PUBLIC_ORIGIN || ""}/unsubscribe?t=${t}`,
        });
        // One failure must not stop everyone else's mail.
        try { await sendMail(env, sub.email, subject, body); sent++; }
        catch (err) { console.error(`send failed for ${k.name}: ${err}`); }
      }
      cursor = list.list_complete ? null : list.cursor;
    } while (cursor);
    console.log(`alerts: ${sent} email(s) sent`);
  },
};
