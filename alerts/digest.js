/* Turning "what changed this week" into "what changed for you".
 *
 * Pure functions, no I/O, so the part that decides what lands in somebody's
 * inbox can be tested without a network, a database, or a Cloudflare account.
 * worker.js imports these; tests/frontend/digest.test.js exercises them.
 *
 * The rule this file exists to enforce: an alert is sent only for a journal
 * the person actually asked about, and only when something they would act on
 * has changed. A weekly email that fires on noise is unsubscribed from, and
 * then the one that mattered never arrives.
 */

/* Fields whose movement is worth an email. Deliberately narrow — scope text
 * and topic lists churn constantly as OpenAlex reclassifies, and an alert on
 * those would be pure noise. This mirrors changelog.py's COLUMNS, which is the
 * same judgement applied at the other end of the pipeline. */
export const NOTIFIABLE = ["status", "cost_kind", "price", "currency", "disputed"];

/* Human wording for a single field moving. "for good or bad" was the ask, so
 * gains and losses are both reported, and neither is editorialised beyond
 * saying plainly which way it went. */
export function describeChange(field, before, after) {
  const money = (v) => (v === "" || v == null ? "no published price" : v);
  switch (field) {
    case "status":
      return `Oxford deal status changed: ${before || "none"} → ${after || "none"}`;
    case "cost_kind":
      return `What the site can say about the cost changed: ${before} → ${after}`;
    case "price":
      return `Published price changed: ${money(before)} → ${money(after)}`;
    case "currency":
      return `Price currency changed: ${money(before)} → ${money(after)}`;
    case "disputed":
      return after
        ? "Our sources now disagree about this journal, so no cost is stated"
        : "Our sources no longer disagree about this journal";
    default:
      return `${field}: ${before} → ${after}`;
  }
}

/* Which of a subscriber's journals moved, given one build's changes.json.
 *
 * Matching is on ISSN-L, the same identifier the site keys everything else on.
 * A journal the person watches that simply did not change produces nothing —
 * silence is the correct output for "no news".
 */
export function matchChanges(changes, watchedIssns) {
  const watched = new Set(watchedIssns || []);
  if (!watched.size) return [];
  const out = [];

  for (const c of (changes && changes.changed) || []) {
    if (!watched.has(c.issn_l)) continue;
    const lines = [];
    for (const field of NOTIFIABLE) {
      const pair = (c.changes || {})[field];
      if (!pair) continue;
      const [before, after] = pair;
      if (before === after) continue;          // recorded but not a change
      lines.push(describeChange(field, before, after));
    }
    if (lines.length) out.push({ issn_l: c.issn_l, title: c.title, lines });
  }

  // A journal leaving the dataset matters as much as one changing: it is the
  // difference between "no deal" and "we can no longer tell you anything".
  for (const r of (changes && changes.removed) || []) {
    if (watched.has(r.issn_l)) {
      out.push({ issn_l: r.issn_l, title: r.title,
                 lines: ["This journal is no longer listed on the site"] });
    }
  }
  return out;
}

/* The email body. Plain text: it is a short factual notice, it has to survive
 * every mail client, and HTML would invite it to look like marketing. */
export function buildEmail(matches, opts) {
  const site = (opts && opts.site) || "https://ben18785.github.io/oxford-apc-finder/";
  const unsubscribe = (opts && opts.unsubscribe) || "";
  const n = matches.length;
  const subject = n === 1
    ? `Oxford APC Finder: ${matches[0].title} changed`
    : `Oxford APC Finder: ${n} of your journals changed`;

  const body = [
    n === 1
      ? "One of the journals you are watching has changed:"
      : `${n} of the journals you are watching have changed:`,
    "",
  ];
  for (const m of matches) {
    body.push(`${m.title} (${m.issn_l})`);
    for (const line of m.lines) body.push(`  - ${line}`);
    body.push(`  ${site}#compare=${m.issn_l}`);
    body.push("");
  }
  body.push("This is an automated notice from an unofficial tool. Confirm any");
  body.push("figure with the Bodleian open access team before relying on it:");
  body.push("oapayments@bodleian.ox.ac.uk");
  if (unsubscribe) {
    body.push("");
    body.push(`Stop these emails: ${unsubscribe}`);
  }
  return { subject, body: body.join("\n") };
}
