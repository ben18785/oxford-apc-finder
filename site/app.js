/* Oxford Journal APC Finder — client-side search & rendering.
 * Pure vanilla JS, no dependencies. Search index loads once; full journal
 * detail records load lazily per shard when a journal is opened. */
"use strict";

const STATE = { config: null, index: [], loaded: false, shards: {}, results: [] };
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));

/* Plain-English explanations for the jargon on the page. One place, so the
 * badge, the detail panel and the search tips can never drift apart. */
const EXPLAIN = {
  covered: ["Covered by Oxford deal",
    "<p>This journal is on the title list of a transformative agreement Oxford takes part in, so the article processing charge should be paid centrally by your division rather than by you.</p><p>Conditions apply: you must be the corresponding author, submit from your <code>@ox.ac.uk</code> address, and choose a CC BY licence.</p>"],
  discount: ["Discount available",
    "<p>No agreement covers this journal outright, but Oxford has negotiated a percentage off the publisher's list price. The figure shown is that arithmetic, not a quotation.</p><p>You or your grant still pay the remainder.</p>"],
  diamond: ["Free to publish (diamond)",
    "<p>Free to publish <em>and</em> free to read. Costs are met by supporting institutions and funders rather than by authors, and Oxford is one of the supporters.</p>"],
  none: ["No Oxford deal",
    "<p>This journal is not on the title list of any agreement Oxford participates in, and its publisher is not in a discount or diamond scheme on the Bodleian's page.</p><p>That is not the same as being ineligible for support: block grants or funder routes may still apply.</p>"],
  doaj: ["In DOAJ",
    "<p>Listed in the <strong>Directory of Open Access Journals</strong>, an independent index that checks journals against around fifty criteria covering peer review, licensing, editorial transparency and fees.</p><p>It is a check on openness and process, <strong>not a ranking of quality</strong>. Its absence means little on its own, since subscription journals are not eligible to be listed.</p>"],
  disputed: ["Sources disagree",
    "<p>The Journal Checker Tool and the Bodleian's own deals page make different claims about this publisher, and we cannot tell which is current.</p><p>Both claims are shown on the journal's page. Confirm with the open access team before submitting.</p>"],
  expired: ["Agreement ended",
    "<p>The agreement covering this journal has passed its stated end date. Renewals are often recorded late, so coverage may well continue, but it is no longer a settled fact.</p>"],
  model_diamond: ["Free to publish",
    "<p><strong>Diamond open access</strong>: free to publish <em>and</em> free to read. The journal's costs are met by its publisher, a university or a consortium rather than by authors.</p><p>No Oxford deal is needed, and none exists — the Bodleian recommends this route to authors without funder support.</p>"],
  model_gold: ["Open access",
    "<p>Fully open access: every article is free to read, and an article processing charge is normally payable to publish.</p><p>The figure shown is that charge, after any Oxford discount.</p>"],
  model_hybrid: ["Subscription journal with a paid open access option",
    "<p>Articles sit behind a paywall by default. Paying the charge shown makes your article open access instead, so the figure is the price of <em>openness</em>, not the price of publishing here.</p><p>Publishing behind the paywall is often free to the author, but not always: submission fees are near-universal in economics and finance, and page or colour charges are common in the physical sciences. This site has no source for any of them — check the journal's own author guidance.</p>"],
  model_subscription: ["Subscription journal",
    "<p>Articles are behind a paywall and we hold no open access charge for this journal.</p><p>There is often nothing to pay, but some journals levy submission, page or colour charges that this site does not track — check the journal's author guidance.</p><p>If you need open access — for a funder mandate, say — ask the publisher what they charge.</p>"],
  transformative: ["Transformative agreement",
    "<p>A contract between a publisher and a university — or, for most of Oxford's, a consortium such as Jisc negotiating for UK universities — that folds subscription fees and open-access charges into a single payment.</p><p>Instead of the library paying to <em>read</em> and the author paying to <em>publish</em>, one deal covers both. That is why an eligible Oxford author pays nothing: the charge is settled centrally, not waived.</p><p>They are called “transformative” because they are meant to be temporary. The intent is to move a subscription journal to fully open access, so each one has an end date and is renegotiated. Each also carries its own conditions — which article types qualify, which licence you must choose, and sometimes a cap on how many articles a year are covered. That is why coverage here is not the same as a guarantee for your particular article.</p>"],
  inclusion: ["What gets listed here",
    "<p>This is not every journal in existence. A journal is listed if <em>any</em> of these is true:</p><ul><li>Oxford has a deal covering it, or the Bodleian lists a discount for its publisher</li><li>It is in the Directory of Open Access Journals</li><li>Its publisher is one of about 95 established publishers, societies and university presses on a vetted list</li><li>It appears in a transformative agreement <em>anywhere in the world</em>, not only Oxford\u2019s</li><li>It is among the 15,000 most-cited journals worldwide</li><li>It is among the leading journals <em>within its own subfield</em> \u2014 there are 252 of those, so every discipline gets its own ranking rather than competing with biomedicine</li><li>This site has listed it before \u2014 kept for a year, so coverage cannot shrink just because a source had a bad day</li></ul><p>Journals withdrawn from DOAJ for misconduct-type reasons are excluded, whoever publishes them.</p><p>The subfield rule is there because a global citation ranking is not neutral between disciplines: it reaches the sciences long before law or the humanities, where scholarship cites in footnotes that citation indexes do not capture. The Law Quarterly Review records 131 citations where a top-200 law journal would need 27,371 \u2014 no worldwide ranking will ever find it, but a ranking <em>within law</em> can. The vetted publisher list covers what is left, and it will always be missing someone. If a journal you expected is absent, use <strong>\u201cJournal missing? Tell us\u201d</strong>: the rules need widening more often than the journal is genuinely out of scope.</p>"],
  waiver: ["APC waivers available",
    "<p>The journal states it will waive or reduce its charge for authors who cannot pay, typically those in lower-income countries. Terms are set by the journal, not by Oxford.</p>"],
};

const STATUS_LABEL = {
  covered: ["Covered by Oxford deal", "covered"],
  discount: ["Discount available", "discount"],
  // Oxford funding a diamond consortium is a fact about Oxford. Whether the
  // journal charges you is a fact about the journal, and lives in MODEL_LABEL.
  diamond: ["Oxford supports this journal", "diamond"],
  none: ["No Oxford deal", "none"],
};

/* The journal's publishing model — what it costs an author regardless of any
 * Oxford arrangement. 13,467 journals cost nothing to publish in yet were
 * labelled only "No Oxford deal", because none is needed. */
const MODEL_LABEL = {
  diamond: ["Free to publish", "m-diamond"],
  gold: ["Open access", "m-gold"],
  hybrid: ["Subscription + paid OA", "m-hybrid"],
  subscription: ["Subscription", "m-sub"],
};

/* ---------------- data loading ---------------- */
async function boot() {
  STATE.config = await (await fetch("config.json")).json();
  applyConfig();
  const idx = await (await fetch("data/index.json")).json();
  STATE.index = idx.journals;
  STATE.counts = idx.counts;
  STATE.generated = idx.generated;
  if (idx.sample_data) $("#sample-banner").hidden = false;
  showStalenessWarning(idx.generated);
  STATE.index.forEach((r, n) => { r.n = n; });   // position in the keyword arrays
  STATE.loaded = true;
  STATE.starred = new Set(starredIds());
  wireUI();
  initAnalytics();
  runSearch();
  loadKeywords();
  openSharedList();
}

/* A shared link carries a list of ISSNs in the hash. It is shown immediately,
 * but deliberately does NOT overwrite whatever the reader has already starred —
 * arriving from a colleague's link should not silently discard your own
 * shortlist. */
function openSharedList() {
  const m = (location.hash || "").match(/^#compare=([\d\-X,]+)$/i);
  if (!m) return;
  const ids = m[1].split(",").filter(Boolean);
  if (ids.length) showCompare(ids);
}

/* Subject keywords are ~75% of the index by size, so they arrive separately
 * and after first paint. Title/publisher/ISSN search works without them; field
 * search starts working the moment they land. */
async function loadKeywords() {
  try {
    const kw = await (await fetch("data/keywords.json")).json();
    STATE.vocab = kw.vocab;
    STATE.kwIds = kw.ids;
    STATE.kwReady = true;
    if ($("#q").value.trim()) runSearch();   // re-score what's on screen
  } catch (err) {
    console.warn("keyword index unavailable; searching titles only", err);
  }
}

/* Vocabulary ids whose word contains this query token. Computed once per
 * search rather than per record. */
function vocabMatches(term) {
  const hits = new Set();
  if (!STATE.kwReady) return hits;
  for (let i = 0; i < STATE.vocab.length; i++) {
    if (STATE.vocab[i].includes(term)) hits.add(i);
  }
  return hits;
}

/* A refresh can fail for weeks and the page would look identical, so say how
 * old the data is once it passes the age the config considers acceptable. */
function showStalenessWarning(generated) {
  const maxDays = STATE.config.max_dataset_age_days;
  if (!generated || !maxDays) return;
  const built = new Date(generated);
  if (isNaN(built)) return;
  const days = Math.floor((Date.now() - built.getTime()) / 86400000);
  if (days <= maxDays) return;
  const el = $("#stale-banner");
  el.innerHTML = `\u26a0 This data was last rebuilt <strong>${days} days ago</strong>
    (${esc(generated.slice(0, 10))}). The automatic refresh may have stopped.
    Deals may have changed since — check the
    <a href="${esc(STATE.config.bodleian_deals)}" target="_blank" rel="noopener">Bodleian deals page</a>
    before relying on anything here.`;
  el.hidden = false;
}

function applyConfig() {
  const c = STATE.config;
  document.title = c.title;
  $("#site-title").textContent = c.title;
  $("#site-tagline").textContent = c.tagline;
  $("#foot-bod").href = c.bodleian_deals;
  const repoUrl = `https://github.com/${c.github_repo}`;
  $("#foot-repo").href = repoUrl;
  $("#header-repo").href = repoUrl;
}

async function loadShard(id) {
  // Length comes from the build rather than being hardcoded here — these two
  // silently disagreed once already, and the only symptom was a dead click.
  const key = id.slice(0, STATE.config.shard_key_length || 4);
  if (STATE.shards[key]) return STATE.shards[key];
  const resp = await fetch(`data/details/${key}.json`);
  if (!resp.ok) throw new Error(`detail data unavailable (${resp.status})`);
  const data = await resp.json();
  STATE.shards[key] = data;
  return data;
}

/* ---------------- search ----------------
 *
 * Query language, deliberately the conventions people already know from GitHub
 * and Gmail rather than full boolean algebra:
 *
 *   cell biology        both terms (AND is the default)
 *   "cell biology"      that exact phrase
 *   cell OR cellular    either
 *   -neuroscience       exclude
 *   title:science       restrict to one field (title, publisher, issn, subject)
 *
 * Everything is in memory, so this is a parsing problem rather than a search
 * infrastructure one. Terms match at the START of a word: plain substring
 * matching made "ear" hit 2,977 titles (Research, Year, Learning), while
 * word-start still finds plurals and prefixes — "science" finds "Sciences",
 * "immuno" finds "Immunology".
 */
const FIELDS = { title: 1, publisher: 1, issn: 1, subject: 1 };
const escapeRx = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const wordStart = (hay, term) => new RegExp("\\b" + escapeRx(term)).test(hay);

/* Split on whitespace but keep quoted phrases whole, including when they are
 * scoped or negated (-"foo bar", title:"foo bar"). */
function tokenizeQuery(raw) {
  return raw.match(/-?(?:[a-zA-Z]+:)?"[^"]*"|\S+/g) || [];
}

function parseClause(token) {
  let negate = false, field = null, phrase = false;
  if (token.startsWith("-") && token.length > 1) { negate = true; token = token.slice(1); }
  const scoped = token.match(/^([a-zA-Z]+):([\s\S]*)$/);
  if (scoped && FIELDS[scoped[1].toLowerCase()]) {
    field = scoped[1].toLowerCase();
    token = scoped[2];
  }
  if (token.length > 1 && token.startsWith('"') && token.endsWith('"')) {
    phrase = true;
    token = token.slice(1, -1);
  }
  const value = token.toLowerCase().trim();
  return value ? { field, value, phrase, negate } : null;
}

/* AND-ed groups of OR-ed alternatives. `OR` must be uppercase so a journal
 * legitimately titled "... or ..." still searches as a word. */
function parseQuery(raw) {
  const groups = [];
  let joinNext = false;
  for (const token of tokenizeQuery(raw)) {
    if (token === "OR") { joinNext = groups.length > 0; continue; }
    const clause = parseClause(token);
    if (!clause) continue;
    // Subject ids are resolved once per clause, not once per record.
    clause.ids = vocabIdsFor(clause.value, clause.phrase);
    if (joinNext) groups[groups.length - 1].push(clause);
    else groups.push([clause]);
    joinNext = false;
  }
  return groups;
}

function vocabIdsFor(value, phrase) {
  const hits = new Set();
  if (!STATE.kwReady) return hits;
  for (let i = 0; i < STATE.vocab.length; i++) {
    const w = STATE.vocab[i];
    if (phrase ? w.includes(value) : w.startsWith(value)) hits.add(i);
  }
  return hits;
}

/* Returns {score, byName}: byName is true when the clause matched the
 * journal's identity (title, alternate title, publisher, ISSN) rather than
 * only its subject keywords. Searching "science" matches ~28,000 journals by
 * subject and a dozen by name; merging those into one list makes the name
 * matches unfindable. */
function clauseScore(rec, clause, parts) {
  const { title, alt, pub, issns, acro, ids } = parts;
  const v = clause.value;
  const hit = (hay) => clause.phrase ? hay.includes(v) : wordStart(hay, v);
  const f = clause.field;
  let score = 0, byName = false;

  if (!f || f === "title") {
    if (hit(title)) { score += clause.phrase ? 60 : 40; byName = true; }
    if (!clause.phrase && title.split(/\s+/).includes(v)) score += 25;
    if (hit(alt)) { score += 15; byName = true; }
  }
  if (!f || f === "publisher") {
    if (hit(pub)) { score += 10; byName = true; }
  }
  if (!f || f === "issn") {
    if (issns.includes(v)) { score += 5; byName = true; }
  }
  // An initialism is how people refer to a journal out loud, so an exact hit
  // is a strong signal — "jrsssa" should find one journal, not a topic list.
  if (!f || f === "title") {
    // An exact initialism is decisive; a prefix is only a hint. Without that
    // split, "pnas" ranked PNAS Nexus above PNAS itself — a title beginning
    // with the letters beat the journal the letters actually denote.
    const acros = acro ? acro.split(" ") : [];
    if (acros.includes(v)) { score += 800; byName = true; }
    else if (acros.some(a => a.length > 2 && a.startsWith(v))) {
      score += 150; byName = true;
    }
  }
  if (!f || f === "subject") {
    if (ids && clause.ids.size && ids.some((id) => clause.ids.has(id))) score += 8;
  }
  return { score, byName };
}

function scoreRecord(rec, groups, rawQuery) {
  const parts = {
    title: (rec.t || "").toLowerCase(),
    alt: (rec.a || []).join(" ").toLowerCase(),
    pub: (rec.p || "").toLowerCase(),
    issns: (rec.i || []).join(" "),
    acro: (rec.y || "").toLowerCase(),
    ids: STATE.kwReady ? (STATE.kwIds[rec.n] || []) : null,
  };
  let score = 0, nominal = true;

  // Whole-query bonuses use the cleaned query, so quoting or punctuation can
  // never suppress them — quoting "science" used to drop the journal Science
  // from 1st to 48th because the raw string still carried its quote marks.
  if (rawQuery) {
    if (parts.title === rawQuery) score += 1000;
    else if (parts.title.startsWith(rawQuery)) score += 200;
    if (parts.issns.includes(rawQuery.replace(/\s/g, ""))) score += 500;
  }

  for (const group of groups) {
    let groupHit = false, groupByName = false, groupScore = 0;
    for (const clause of group) {
      const { score: s, byName } = clauseScore(rec, clause, parts);
      const matched = s > 0;
      if (clause.negate ? !matched : matched) {
        groupHit = true;
        groupScore = Math.max(groupScore, s);
        if (byName || clause.negate) groupByName = true;
      }
    }
    if (!groupHit) return { sc: 0, nominal: false };   // every group must match
    score += groupScore;
    if (!groupByName) nominal = false;
  }
  return { sc: score, nominal };
}

/* Ordering by cost, which is only honest once currencies are reconciled.
 *
 * Journals price in 46 currencies. Sorted on the raw number, the dearest
 * journal on the site would be one charging 150,000,000 IRR — about £2,400 —
 * while Nature at $12,290 sat far below it. The pipeline therefore attaches a
 * GBP figure (`g`) converted at dated ECB reference rates.
 *
 * `g` is absent, never zero, wherever nothing comparable exists: no published
 * price, a discount off an unknown base, sources in conflict, or a currency
 * the ECB does not publish. Those journals are held back as a labelled group
 * instead of being sorted to one end, where they would silently read as either
 * the cheapest or the most expensive thing available. */
function sortByCost(list, direction) {
  const priced = [], unpriced = [];
  for (const r of list) (typeof r.g === "number" ? priced : unpriced).push(r);
  // Ties break on certainty before title, in both directions. Twelve thousand
  // journals share the value 0, and without this a capped agreement whose
  // allowance may already be spent sits indistinguishably among diamond titles
  // that charge nothing at all — position quietly saying what the wording was
  // changed to stop saying.
  priced.sort((a, b) => (direction === "cost-desc" ? b.g - a.g : a.g - b.g)
                        || (a.v || 0) - (b.v || 0)
                        || (a.t || "").localeCompare(b.t || ""));
  return { priced, unpriced };
}

function runSearch() {
  if (!STATE.loaded) return;
  // Any new search invalidates a pending "found nothing" report — otherwise a
  // prefix that matched nothing gets recorded even though the finished query
  // succeeded a keystroke later.
  clearTimeout(missedTimer);
  const raw = $("#q").value.trim();
  const dealOnly = $("#deal-only").checked;
  const freeOnly = $("#free-only") && $("#free-only").checked;
  // One predicate for both the empty-query and the scored path, so a filter
  // cannot apply on one and silently not the other.
  const passes = (r) => (!dealOnly || r.s !== "none") && (!freeOnly || r.f);

  if (!raw) {
    // merge.py already emits journals sorted by title, so the index arrives in
    // display order — re-sorting 43k records on every empty search changes
    // nothing and costs hundreds of milliseconds.
    const pool = (dealOnly || freeOnly) ? STATE.index.filter(passes) : STATE.index;
    return finish(pool, pool.length, []);
  }

  const groups = parseQuery(raw);
  if (!groups.length) return renderResults([], 0, 0, []);

  // The text the user is actually looking for, rebuilt from the parsed
  // clauses. Taking it from the raw string meant quotes and field prefixes
  // leaked into the exact-title comparison: `"science"` dropped the journal
  // Science from 1st to 48th, and `title:science` to 375th.
  const cleaned = groups
    .map((g) => g.filter((c) => !c.negate).map((c) => c.value).join(" "))
    .filter(Boolean).join(" ").trim();

  // Score the whole index, then apply the deal filter — so we can tell the
  // user when their best name match was hidden by the filter rather than
  // simply absent.
  const byName = [], bySubject = [], hidden = [];
  for (const r of STATE.index) {
    const { sc, nominal } = scoreRecord(r, groups, cleaned);
    if (sc <= 0) continue;
    if (!passes(r)) {
      // Track only the deal filter's casualties: a strong name match hidden by
      // it looks identical to one the tool has never heard of.
      if (nominal && dealOnly && r.s === "none" && (!freeOnly || r.f)) {
        hidden.push({ r, sc });
      }
      continue;
    }
    (nominal ? byName : bySubject).push({ r, sc });
  }
  const rank = (a, b) => b.sc - a.sc;
  byName.sort(rank); bySubject.sort(rank); hidden.sort(rank);

  const matches = byName.concat(bySubject).map((x) => x.r);
  // A genuine miss, not one the deal filter caused — those are a different
  // thing and the user is already told about them.
  if (!matches.length && !hidden.length) trackMissedSearch(cleaned);
  finish(matches, byName.length, hidden.map((x) => x.r));
}

/* Shared tail of both search paths, so an ordering can never apply to one and
 * silently not the other — the same reason `passes` is a single predicate. */
function finish(matches, nominalCount, hidden) {
  const sort = ($("#sort") && $("#sort").value) || "rel";
  let list = matches, unpriced = null;
  if (sort !== "rel") {
    // Copy first: the empty-query path hands back STATE.index itself.
    const split = sortByCost(matches.slice(), sort);
    list = split.priced.concat(split.unpriced);
    unpriced = split.unpriced.length;
    // The name/subject boundary is a statement about relevance order, so it
    // means nothing once the rows are ordered by price.
    nominalCount = 0;
  }
  STATE.results = list;
  STATE.nominalCount = nominalCount;
  STATE.hiddenByFilter = hidden;
  STATE.unpricedCount = unpriced;
  renderResults(list.slice(0, 200), list.length, nominalCount, hidden);
}

/* ---------------- rendering ---------------- */
function why(key) {
  const [title] = EXPLAIN[key] || [];
  if (!title) return "";
  return ` <button class="why" data-explain="${esc(key)}"
    aria-label="What does &quot;${esc(title)}&quot; mean?">?</button>`;
}

function modelBadge(model) {
  const entry = MODEL_LABEL[model];
  if (!entry) return "";
  return `<span class="badge ${entry[1]}">${esc(entry[0])}${why("model_" + model)}</span>`;
}

function badge(status, inDoaj, disputed, expired, model) {
  const [label, cls] = STATUS_LABEL[status] || STATUS_LABEL.none;
  let html = modelBadge(model);
  // The deal badge is only worth space when there is a deal to report.
  if (status !== "none") html += ` <span class="badge ${cls}">${esc(label)}${why(status)}</span>`;
  if (inDoaj) html += ` <span class="badge doaj">In DOAJ${why("doaj")}</span>`;
  if (disputed) html += ` <span class="badge disputed" title="Oxford's own page and the Journal Checker Tool disagree about this deal">⚠ Sources disagree${why("disputed")}</span>`;
  if (expired) html += ` <span class="badge expired" title="The agreement's stated end date has passed">⚠ Agreement ended${why("expired")}</span>`;
  return html;
}

/* Popover shared by every "?" on the page. */
let openWhy = null;
function closeWhy() { const p = $("#why-pop"); if (p) { p.hidden = true; } openWhy = null; }
function showWhy(btn) {
  const entry = EXPLAIN[btn.dataset.explain];
  if (!entry) return;
  let pop = $("#why-pop");
  if (!pop) {
    pop = document.createElement("div");
    pop.id = "why-pop"; pop.className = "pop"; pop.setAttribute("role", "dialog");
    document.body.appendChild(pop);
  }
  pop.innerHTML = `<h4>${esc(entry[0])}</h4>${entry[1]}`;
  pop.hidden = false;
  const r = btn.getBoundingClientRect();
  const w = Math.min(pop.offsetWidth, window.innerWidth - 24);
  let left = r.left + window.scrollX - w / 2 + r.width / 2;
  left = Math.max(12, Math.min(left, window.innerWidth - w - 12));
  pop.style.left = left + "px";
  pop.style.top = (r.bottom + window.scrollY + 8) + "px";
  openWhy = btn;
}

/* The agreement's stated end date has passed. JCT records renewals late, so
 * this is not proof coverage stopped — but "£0" must not be shown as settled. */
function expiryBlock(e) {
  if (!e) return "";
  return `<div class="dispute">
    <h4>\u26a0 This agreement's end date has passed</h4>
    <p>The Oxford agreement covering this journal was recorded as ending on
      <strong>${esc(e.end_date)}</strong> \u2014 ${e.days} days ago.</p>
    <p>It may well have been renewed: the Journal Checker Tool still lists Oxford
      as a participant, and renewals are often recorded late. But the cost below
      assumes coverage that is past its stated end date.</p>
    <p class="cost-note">Confirm with ${esc(STATE.config.contact)} before submitting.</p>
  </div>`;
}

/* A deal our sources contradict each other about. Shown prominently: the
 * computed cost below it may be wrong, and the user needs to know that
 * before acting on it. */
function disputeBlock(d) {
  if (!d) return "";
  return `<div class="dispute">
    <h4>⚠ Check this one before you rely on it</h4>
    <p>${esc(d.note || "")}</p>
    ${d.jct_says ? `<p><strong>Journal Checker Tool says:</strong> ${esc(d.jct_says)}</p>` : ""}
    ${d.bodleian_says ? `<p><strong>The Bodleian's page says:</strong> ${esc(d.bodleian_says)}</p>` : ""}
    <p class="cost-note">Because these sources disagree, this tool states
      <strong>no expected cost</strong> for this journal — picking one of them
      would be a guess with your money. Confirm with
      ${esc(STATE.config.contact)} before relying on either figure.</p>
  </div>`;
}

/* Split the cost summary so the figure can be right-aligned on its own.
 * cost_summary() in build_site.py produces e.g. "£0 — covered by Oxford deal";
 * the ledger shows only the figure, since the status column says the rest. */
function costFigure(rec) {
  const c = rec.c || "";
  // Every figure here is the cost of publishing OPEN ACCESS, and the column
  // heading says so — which is what lets the number stand unqualified for all
  // four publishing models alike.
  //
  // Hybrid journals were previously shown as "free, or pay for OA" instead,
  // hiding the charge behind a filter. That was wrong twice over: the filter
  // moved 667 of 46,315 rows and so looked broken, and "free" was an
  // unsourced claim about the subscription route. Hybrids commonly levy
  // submission fees (near-universal in economics), page charges and colour
  // charges, and no source this site reads records any of them. The Bodleian
  // says as much on its own page, and the disclaimer carries it.
  if (rec.o === "subscription" && /^APC unknown/.test(c)) {
    return { text: "subscription", cls: "none" };
  }
  // Three tiers, and the distinction between them is the point of the column:
  //
  //   £0             the journal charges authors nothing — diamond or no-APC.
  //                  Nothing about you can change this.
  //   £0 if eligible an Oxford agreement pays it for an eligible corresponding
  //                  author. True for the great majority of covered journals.
  //   confirm first  as above, but with a journal-specific risk on top: a
  //                  finite annual allowance, a funder restriction, or an
  //                  agreement past its end date.
  //   not confirmed  our sources contradict each other; no figure is asserted.
  //
  // Colouring the second the same green as the first was the old behaviour and
  // is what let "this journal is in an agreement" read as "I will not be
  // invoiced".
  if (/^Not confirmed/.test(c)) return { text: "not confirmed", cls: "caution" };
  if (/^£0 if eligible — but confirm/.test(c)) {
    return { text: "£0 — confirm first", cls: "caution" };
  }
  if (/^£0 if eligible/.test(c)) return { text: "£0 if eligible", cls: "free" };
  if (/^£0/.test(c)) return { text: "£0", cls: "free" };
  const m = c.match(/^~?([\d,]+\s+[A-Z]{3})/);
  if (m) return { text: (c.startsWith("~") ? "≈ " : "") + m[1], cls: "" };
  if (/^No APC/i.test(c)) return { text: "no charge", cls: "free" };
  if (/discount/i.test(c)) return { text: c.replace(/\s*\(.*\)$/, ""), cls: "" };
  return { text: "not published", cls: "none" };
}

function renderResults(list, total, nominalCount, hidden) {
  const box = $("#results");
  $("#empty").hidden = total > 0 || (hidden && hidden.length > 0);

  // Report the two kinds separately. "28,010 journals" for a search on
  // "science" is true and completely useless.
  const subject = total - nominalCount;
  const n = (x) => x.toLocaleString();
  const countText = !total ? ""
    : (subject > 0 && nominalCount !== total
        ? `${n(nominalCount)} by name · ${n(subject)} more by subject`
        : `${n(total)} journal${total === 1 ? "" : "s"}`)
      + (total > list.length ? ` (showing ${n(list.length)})` : "");
  // A count is the obvious place to ask "out of what?", and on an empty search
  // this line *is* the corpus size — so the inclusion rules hang off it rather
  // than being buried in the About page. innerHTML, not textContent, because
  // why() returns markup; the count itself is generated numbers.
  $("#result-count").innerHTML = countText
    ? esc(countText) + why("inclusion") : "";

  let html = "";

  // Ordering by price silently drops journals that have no comparable figure,
  // so say how many and why — an ordering that quietly omits 5,000 journals
  // reads as a complete list of everything.
  if (STATE.unpricedCount) {
    const fx = STATE.config.fx || {};
    html += `<p class="filter-note">Ordered by cost, converted to sterling at
      ${fx.date ? `European Central Bank rates for ${esc(fx.date)}`
                : "published exchange rates"}.
      <strong>${n(STATE.unpricedCount)}</strong> of these journals have no
      comparable figure — no published price, a discount off an unknown base,
      sources in conflict, or a currency the ECB does not publish — and are
      listed after the priced ones rather than being ordered among them.</p>`;
  }

  // A strong name match removed by the deal filter looks identical to one the
  // tool has never heard of. Searching "science" with the filter on hides the
  // journal Science, which has no Oxford deal — say so rather than leave the
  // user to conclude it is missing.
  if (hidden && hidden.length) {
    const names = hidden.slice(0, 3).map((r) => `<strong>${esc(r.t)}</strong>`).join(", ");
    html += `<p class="filter-note">${names}${hidden.length > 3
      ? ` and ${hidden.length - 3} more` : ""} match your search but have
      <strong>no Oxford deal</strong>, so the filter above is hiding
      ${hidden.length === 1 ? "it" : "them"}.
      <a href="#" id="show-all">Show journals without a deal</a></p>`;
  }

  if (list.length) {
    const rows = list.map((r, i) => {
      const brk = (i === nominalCount && nominalCount > 0)
        ? `<tr class="subject-break"><td colspan="3">Journals whose
           <strong>subject</strong> matches, but not their name</td></tr>` : "";
      const fig = costFigure(r);
      const flags = [
        modelBadge(r.o),
        r.d ? `<span class="badge doaj">In DOAJ${why("doaj")}</span>` : "",
        r.x ? `<span class="badge disputed">⚠ Sources disagree${why("disputed")}</span>` : "",
        r.e ? `<span class="badge expired">⚠ Agreement ended${why("expired")}</span>` : "",
      ].filter(Boolean).join("");
      const [label] = STATUS_LABEL[r.s] || STATUS_LABEL.none;
      return `${brk}
        <tr data-id="${esc(r.id)}">
          <td>
            ${starButton(r.id)}<button class="jtitle">${esc(r.t)}</button>
            <div class="jmeta">${esc(r.p || "Publisher unknown")} · ${esc(r.i[0] || "")}</div>
            ${flags ? `<div class="flags">${flags}</div>` : ""}
          </td>
          <td class="state"><span class="swatch sw-${esc(r.s)}"></span>${esc(label)}${why(r.s)}</td>
          <td class="cost-cell ${fig.cls}">${esc(fig.text)}</td>
        </tr>`;
    }).join("");

    html += `<table class="ledger">
      <thead><tr><th>Journal</th><th>Oxford deal</th><th class="num">Open access cost</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }

  box.innerHTML = html;

  const showAll = $("#show-all");
  if (showAll) showAll.addEventListener("click", (e) => {
    e.preventDefault();
    $("#deal-only").checked = false;
    runSearch();
  });
  // The title is the real control, so keyboard users get one stop per row;
  // clicking anywhere else in the row is a convenience for mouse users.
  updateStarUI();
  box.querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".why")) return;
      if (e.target.closest(".star")) return;   // starring is not navigation
      openDetail(tr.dataset.id);
    });
  });
}

function priceStr(p) { return p ? `${Number(p.price).toLocaleString()} ${esc(p.currency)}` : "—"; }

function costBlock(j) {
  const c = j.cost;
  let main = "", calc = "";
  switch (c.kind) {
    case "covered": main = "£0 to you"; break;
    case "diamond": main = "£0 — diamond OA"; break;
    case "no_apc": main = "No APC"; break;
    case "discount":
      main = `~${priceStr(c.estimated)}`;
      calc = `List price ${priceStr(c.list)} − ${c.pct}% Oxford discount`;
      break;
    case "discount_unknown_base": main = `${c.pct}% Oxford discount`; break;
    case "list_price": main = priceStr(c.list); break;
    default: main = "Not published";
  }
  return `<div class="cost-line">${main}</div>
    ${calc ? `<div class="cost-calc">${esc(calc)}</div>` : ""}
    <div class="cost-note">${esc(c.note || "")}</div>
    <div class="cost-note">Prices are list prices as published by the source and its retrieval date — always confirm on the journal's own page.</div>`;
}

function sourceLinks(j) {
  const p = j.provenance || {};
  const rows = [];
  const seen = new Set();
  const add = (url, label, extra = "") => {
    const k = `${label}|${url}`;
    if (!url || seen.has(k)) return;
    seen.add(k);
    rows.push(`<a href="${esc(url)}" target="_blank" rel="noopener">${esc(label)} ↗</a>${extra}`);
  };
  if (p.deal) for (const d of p.deal) add(d.url, d.label);
  if (p.oxford) add(p.oxford.url, p.oxford.label);
  if (p.metadata) add(p.metadata.url, p.metadata.label,
    ` <span class="cost-note">(retrieved ${esc((p.metadata.retrieved || "").slice(0,10))})</span>`);
  if (p.doaj) add(p.doaj.url, p.doaj.label);
  return rows.map(r => `<div class="src">${r}</div>`).join("");
}

function reportLinks(j) {
  const repo = STATE.config.github_repo;
  const title = encodeURIComponent(`Data issue: ${j.title} (${j.issns[0]})`);
  const body = encodeURIComponent(
    `**Journal:** ${j.title}\n**ISSN(s):** ${j.issns.join(", ")}\n**Deal status shown:** ${j.deal.status}\n**Page URL:** ${location.href}\n\n**What looks wrong (please describe):**\n\n\n---\n_Sources shown on the page:_ OpenAlex / DOAJ / JCT (see the journal's source links).`);
  const url = `https://github.com/${repo}/issues/new?title=${title}&labels=user-report&body=${body}`;
  return { url, title, body };
}

/* What DOAJ records about actually submitting here.
 *
 * Only journals in DOAJ have any of this — about half the site — so the whole
 * section is omitted rather than rendering a page of blanks on the other half.
 *
 * The two booleans are never null in the source: DOAJ requires an answer, so
 * `false` genuinely means "no", not "we don't know". They are rendered as a
 * plain No for that reason, which is a stronger claim than the site usually
 * makes and is only safe because the field is mandatory upstream. */
function submissionBlock(j) {
  const s = j.submission;
  if (!s) return "";

  const rows = [];
  const row = (label, value) => {
    if (value) rows.push(`<dt>${esc(label)}</dt><dd>${value}</dd>`);
  };

  if (s.review_process && s.review_process.length) {
    row("Peer review", esc(s.review_process.join(" · ")));
  }
  // Stored as a string, and not always a bare number.
  const wk = (s.weeks_to_publication || "").toString().trim();
  if (wk) {
    row("Time to publication",
        /^\d+$/.test(wk) ? `About ${esc(wk)} week${wk === "1" ? "" : "s"} from submission`
                         : esc(wk));
  }
  if (typeof s.plagiarism_screening === "boolean") {
    row("Plagiarism screening", s.plagiarism_screening ? "Yes" : "No");
  }
  if (typeof s.author_retains_copyright === "boolean") {
    row("Author keeps copyright", s.author_retains_copyright ? "Yes" : "No");
  }
  if (s.persistent_ids && s.persistent_ids.length) {
    row("Persistent identifiers", esc(s.persistent_ids.join(" · ")));
  }
  // A handful of records carry the URL with no label. Gating the row on the
  // label alone silently threw the link away for those.
  if (s.deposit_policy || s.deposit_policy_url) {
    const label = esc(s.deposit_policy || "See the journal's policy");
    row("Archiving policy", s.deposit_policy_url
      ? `<a href="${esc(s.deposit_policy_url)}" target="_blank" rel="noopener">${label} ↗</a>`
      : label);
  }

  if (!rows.length && !s.author_instructions_url) return "";

  return `
    <div class="detail-section">
      <h4>Submitting here</h4>
      ${s.author_instructions_url ? `<p class="src"><a class="btn"
        href="${esc(s.author_instructions_url)}" target="_blank" rel="noopener"
        >The journal's author guidelines ↗</a></p>` : ""}
      ${rows.length ? `<dl class="subm-grid">${rows.join("")}</dl>` : ""}
      <p class="derived-note">Recorded by the journal in its
        <a href="https://doaj.org/" target="_blank" rel="noopener">DOAJ</a> entry,
        not by Oxford — and describing the journal's stated policy, not any
        individual submission.</p>
      <p class="cost-note">Word limits, LaTeX and preprint policies are not
        shown because no structured source publishes them; they live in the
        author guidelines above.</p>
    </div>`;
}

async function openDetail(id) {
  let shard;
  try {
    shard = await loadShard(id);
  } catch (err) {
    console.error(err);
    return showModal(`<h2 id="detail-title">Could not load this journal</h2>
      <p class="cost-note">${esc(err.message)}. This is a fault in the site, not
        a statement about the journal. Please
        <a href="https://github.com/${esc(STATE.config.github_repo)}/issues/new?title=${
          encodeURIComponent("Journal detail failed to load: " + id)
        }&labels=user-report" target="_blank" rel="noopener">report it</a>.</p>`);
  }
  const j = shard[id];
  if (!j) {
    return showModal(`<h2 id="detail-title">Journal not found</h2>
      <p class="cost-note">No detail record is held for ${esc(id)}.</p>`);
  }
  // Counted here rather than on the row click, so a failed load is not
  // recorded as a reader having seen the journal.
  trackJournalView({ id, s: (j.deal || {}).status, t: j.title });

  const scope = j.scope || {};
  const wd = j.doaj_withdrawn;
  const rep = reportLinks(j);

  const body = `
    <div class="detail-head">
      <h2 id="detail-title">${starButton(id)}${esc(j.title)}</h2>
      <p class="pub">${esc(j.publisher || "Publisher unknown")}</p>
      <p class="detail-issn">ISSN: ${j.issns.map(esc).join(" · ")}</p>
      <div class="badge-row">${badge(j.deal.status, j.in_doaj, j.deal.disputed, j.deal.expired, j.oa_status)}
        ${j.waiver ? '<span class="badge doaj">APC waivers available</span>' : ""}</div>
    </div>

    ${wd ? `<div class="detail-section"><div class="warn">Note: this journal was recorded as withdrawn from DOAJ (${esc(wd.date)}) — reason: “${esc(wd.reason)}”.
      <span class="attrib">Reason quoted from the
        <a href="${esc(STATE.config.doaj_withdrawn_changelog)}" target="_blank" rel="noopener">DOAJ withdrawal changelog</a>,
        <a href="https://creativecommons.org/licenses/by-sa/4.0/" target="_blank" rel="noopener">CC BY-SA 4.0</a>.</span>
    </div></div>` : ""}

    ${expiryBlock(j.deal.expired)}
    ${disputeBlock(j.deal.disputed)}

    <div class="detail-section">
      <h4>Why this result</h4>
      <p class="basis">${esc(j.deal.basis || "")}${
        /transformative agreement/i.test(j.deal.basis || "") ? why("transformative") : ""}</p>
      ${j.deal.esac_id ? `<p class="cost-note">Agreement identifier:
        <code>${esc(j.deal.esac_id)}</code> \u2014 quote this if you report a problem.</p>` : ""}
    </div>

    <div class="detail-section">
      <h4>Cost for an Oxford author</h4>
      ${costBlock(j)}
      ${j.deal.caveats && j.deal.caveats.length ? `<ul class="caveats">${j.deal.caveats.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
    </div>

    <div class="detail-section">
      <h4>Scope</h4>
      ${scope.sentence ? `<p>${esc(scope.sentence)}</p><p class="derived-note">Derived from OpenAlex subject classification — not the publisher's own wording.</p>` : "<p class='cost-note'>No scope summary held.</p>"}
      ${(scope.keywords && scope.keywords.length) ? `<div>${scope.keywords.slice(0,10).map(k => `<span class="chip">${esc(k)}</span>`).join("")}</div>` : ""}
      ${scope.aims_url ? `<p class="src" style="margin-top:8px"><a href="${esc(scope.aims_url)}" target="_blank" rel="noopener">Aims &amp; scope on the journal's own site ↗</a></p>` : ""}
    </div>

    ${(j.browse && j.browse.length) ? `<div class="detail-section">
      <h4>Browse recent articles</h4>
      <p class="cost-note">See what this journal actually publishes — these open
        on the publisher's or index's own site.</p>
      ${j.browse.map(b => `<div class="src"><a href="${esc(b.url)}"
        target="_blank" rel="noopener">${esc(b.label)} \u2197</a></div>`).join("")}
    </div>` : ""}

    ${submissionBlock(j)}

    <div class="detail-section">
      <h4>Sources for the information above</h4>
      ${sourceLinks(j)}
    </div>

    <div class="detail-section report-box">
      <h4>Something look wrong?</h4>
      <p class="cost-note">Type what's incorrect and it will open a pre-filled report on GitHub, where it's publicly tracked and fixed.</p>
      <textarea id="report-text" placeholder="e.g. The APC discount shown is out of date — the publisher's page now says…"></textarea>
      <div>
        <a class="btn" id="report-submit" href="${esc(rep.url)}" target="_blank" rel="noopener">Report on GitHub ↗</a>
        <a class="btn secondary" href="https://github.com/${esc(STATE.config.github_repo)}/issues" target="_blank" rel="noopener">See all reports</a>
      </div>
    </div>

    <div class="detail-section">
      <p class="cost-note">No Oxford deal that fits? Oxford's
        <a href="${esc(STATE.config.bodleian_block_grants)}" target="_blank" rel="noopener">block grants</a>
        or the green (self-archiving) route may still apply. Authoritative answers: ${esc(STATE.config.contact)}.</p>
    </div>`;

  $("#detail-body").innerHTML = body;
  $("#detail-modal").hidden = false;
  document.body.style.overflow = "hidden";
  updateStarUI();

  // Live-update the report link with whatever the user types.
  const ta = $("#report-text"), submit = $("#report-submit");
  ta.addEventListener("input", () => {
    const extra = ta.value.trim();
    const base = decodeURIComponent(rep.body);
    const merged = base.replace("**What looks wrong (please describe):**\n\n",
      `**What looks wrong (please describe):**\n${extra}\n`);
    submit.href = `https://github.com/${STATE.config.github_repo}/issues/new?title=${rep.title}&labels=user-report&body=${encodeURIComponent(merged)}`;
  });
  $("#modal-close").focus();
}

function showModal(html) {
  $("#detail-body").innerHTML = html;
  $("#detail-modal").hidden = false;
  document.body.style.overflow = "hidden";
  $("#modal-close").focus();
}

function closeModal() {
  $("#detail-modal").hidden = true;
  document.body.style.overflow = "";
}

/* ---------------- status / about views ---------------- */
async function showStatus() {
  const s = await (await fetch("data/status.json")).json();
  const c = s.counts;
  const rows = Object.entries(s.sources_fetched).slice(0, 30)
    .map(([k, v]) => `<tr><td>${esc(v.retrieved.slice(0,16).replace("T"," "))}</td><td><a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(k)}</a></td></tr>`).join("");
  $("#detail-body").innerHTML = `
    <h2 id="detail-title">Data freshness &amp; sources</h2>
    <p class="cost-note">Dataset generated ${esc((s.dataset_generated||"").replace("T"," ").slice(0,16))} UTC · site built ${esc((s.built||"").replace("T"," ").slice(0,16))} UTC.</p>
    <div class="stat-grid">
      <div class="stat"><div class="n">${(c.total||0).toLocaleString()}</div><div class="l">journals${why("inclusion")}</div></div>
      <div class="stat"><div class="n">${(c.covered||0).toLocaleString()}</div><div class="l">covered by a deal</div></div>
      <div class="stat"><div class="n">${(c.discount||0).toLocaleString()}</div><div class="l">discounted</div></div>
      <div class="stat"><div class="n">${(c.diamond||0).toLocaleString()}</div><div class="l">diamond OA</div></div>
      <div class="stat"><div class="n">${(c.in_doaj||0).toLocaleString()}</div><div class="l">in DOAJ</div></div>
      <div class="stat"><div class="n">${(c.disputed||0).toLocaleString()}</div><div class="l">flagged: sources disagree</div></div>
      <div class="stat"><div class="n">${(c.expired||0).toLocaleString()}</div><div class="l">flagged: agreement ended</div></div>
      <div class="stat"><div class="n">${(c.excluded_misconduct||0).toLocaleString()}</div><div class="l">excluded (DOAJ misconduct)</div></div>
    </div>
    <div class="detail-section">
      <h4>Most recent source fetches</h4>
      <p class="cost-note">Showing the ${Object.keys(s.sources_fetched).length} newest of
        ${(s.sources_fetched_total || 0).toLocaleString()} URLs fetched in the last refresh.</p>
      <table class="status-table"><tbody>${rows}</tbody></table>
    </div>`;
  $("#detail-modal").hidden = false;
  document.body.style.overflow = "hidden";
  $("#modal-close").focus();
}

/* What people actually look up. Deliberately modest in what it claims: these
 * are counts of page interactions, not of people, and the difference matters
 * enough to say on the page rather than bury in a footnote. */
async function showUsage() {
  let u;
  try {
    u = await (await fetch("data/usage.json")).json();
  } catch {
    return showModal(`<h2 id="detail-title">How this site is used</h2>
      <p class="cost-note">No usage data has been published yet.</p>`);
  }
  const t = u.totals || {}, cov = u.coverage || {};
  const pct = (x) => (x == null ? "—" : `${(x * 100).toFixed(0)}%`);
  const n = (x) => (x || 0).toLocaleString();

  const top = u.top_journals || [];
  const max = top.reduce((m, r) => Math.max(m, r.views), 0) || 1;
  const bars = top.map((r) => `
    <div class="bar-row">
      <div class="bar-label" title="${esc(r.title)}">${esc(r.title)}</div>
      <div class="bar-track">
        <div class="bar-fill ${r.status === "covered" ? "covered" : ""}"
             style="width:${Math.max(2, (r.views / max) * 100).toFixed(1)}%"></div>
      </div>
      <div class="bar-value">${n(r.views)}</div>
    </div>`).join("");

  const wanted = (u.most_wanted || []).map((m) =>
    `<li><code>${esc(m.query)}</code> — ${n(m.searches)} searches</li>`).join("");

  // The coverage rate only means something next to the corpus rate. On its own
  // it is a number nobody can calibrate.
  const delta = (cov.covered_journal_share != null && cov.corpus_share != null)
    ? cov.covered_journal_share - cov.corpus_share : null;

  $("#detail-body").innerHTML = `
    <h2 id="detail-title">How this site is used</h2>
    <p class="cost-note">Last ${esc(String(u.window_days || 90))} days ·
      updated ${esc((u.generated || "").replace("T", " ").slice(0, 16))} UTC.</p>

    <div class="stat-grid">
      <div class="stat"><div class="n">${n(t.visitors)}</div><div class="l">visitors</div></div>
      <div class="stat"><div class="n">${n(t.pageviews)}</div><div class="l">sessions</div></div>
      <div class="stat"><div class="n">${n(t.journal_views)}</div><div class="l">journals looked up</div></div>
      <div class="stat"><div class="n">${n(t.distinct_journals_viewed)}</div><div class="l">different journals</div></div>
      <div class="stat"><div class="n">${esc(pct(cov.covered_journal_share))}</div><div class="l">of those have a deal</div></div>
      <div class="stat"><div class="n">${n(t.countries)}</div><div class="l">countries</div></div>
    </div>

    <div class="detail-section">
      <h4>Are the deals aimed at what people publish in?</h4>
      ${cov.sample_journals ? `<p>${esc(pct(cov.covered_journal_share))} of the journals
        readers looked up are covered by an Oxford deal, against
        ${esc(pct(cov.corpus_share))} of every journal this site tracks.${
          delta == null ? "" : delta >= 0.02
          ? " Readers are landing on covered journals more often than chance — the deals are pointed at the right titles."
          : delta <= -0.02
            ? " Readers are landing on covered journals <em>less</em> often than chance, which suggests a gap between what the deals cover and what people are actually trying to publish in."
            : " That is close to chance."}</p>
      <p class="cost-note">Counted over the ${n(cov.sample_journals)} journals opened
        with the “only show journals with an Oxford deal” filter <em>switched off</em>
        (${n(cov.sample_views)} views). Views made with the filter on are excluded
        deliberately: that list is already 100% covered, so including them would
        measure the default setting rather than what researchers chose.</p>`
      : `<p class="cost-note">Not enough data yet. This is measured only over journals
        opened with the deal filter switched off, since the filtered list is already
        100% covered and would answer the question with its own default.</p>`}
    </div>

    ${top.length ? `<div class="detail-section">
      <h4>Most looked up</h4>
      <div class="bar-chart">${bars}</div>
      <p class="cost-note">Bars in colour are journals with an Oxford deal.
        ${u.withheld && u.withheld.journals
          ? `A further ${n(u.withheld.journals)} journals were looked up
             ${n(u.withheld.views)} times between them but are not listed
             individually: below ${esc(String(u.withheld.min_views_to_publish))} views,
             naming a journal here says more about a person than about the journal.`
          : ""}</p>
    </div>` : ""}

    ${wanted ? `<div class="detail-section">
      <h4>Searched for, not found</h4>
      <p class="cost-note">Queries that returned nothing. This is the site's own
        to-do list — each one is either a journal that should be in scope, or a
        search that should have worked.</p>
      <ul class="wanted-list">${wanted}</ul>
    </div>` : ""}

    <div class="detail-section">
      <h4>What is and is not recorded</h4>
      <p>Counting is done by <a href="https://www.goatcounter.com" target="_blank"
        rel="noopener">GoatCounter</a>, which sets <strong>no cookies</strong>, stores
        <strong>no IP addresses</strong>, and issues no identifier that could follow you
        to another site. Recorded: that a journal page was opened, and the country a
        request came from. Not recorded: who you are, what you searched for when the
        search worked, or anything you go on to do.</p>
      <p class="cost-note">“Visitors” and “sessions” are browser-side estimates, not
        counts of people — one person on a laptop and a phone is two, and a shared
        machine may be one. Treat them as an order of magnitude.</p>
    </div>`;
  $("#detail-modal").hidden = false;
  document.body.style.overflow = "hidden";
  $("#modal-close").focus();
}

async function showChanges() {
  let s;
  try {
    s = await (await fetch("data/changes.json")).json();
  } catch {
    s = null;
  }
  const repo = STATE.config.github_repo;
  let body;
  if (!s) {
    body = `<p class="cost-note">No change record is available for this build.</p>`;
  } else if (s.baseline) {
    body = `<p class="cost-note">This is the first recorded build, so there is
      nothing to compare it against yet. Future refreshes will list their changes here.</p>`;
  } else {
    const row = (c) => {
      const bits = Object.entries(c.changes).map(([field, [before, now]]) =>
        `${esc(field)}: <code>${esc(before || "—")}</code> → <code>${esc(now || "—")}</code>`);
      return `<li><strong>${esc(c.title)}</strong> <span class="cost-note">(${esc(c.issn_l)})</span><br>
        <span class="cost-note">${bits.join(" · ")}</span></li>`;
    };
    const statusChanges = (s.changed || []).filter(c => c.changes.status);
    body = `
      <div class="stat-grid">
        <div class="stat"><div class="n">${(s.summary.added||0).toLocaleString()}</div><div class="l">added</div></div>
        <div class="stat"><div class="n">${(s.summary.removed||0).toLocaleString()}</div><div class="l">removed</div></div>
        <div class="stat"><div class="n">${(s.summary.changed||0).toLocaleString()}</div><div class="l">changed</div></div>
      </div>
      ${statusChanges.length ? `<div class="detail-section">
        <h4>Deal status changes</h4>
        <ul class="caveats">${statusChanges.slice(0,60).map(row).join("")}</ul></div>` : ""}
      ${(s.removed||[]).length ? `<div class="detail-section">
        <h4>Removed from the dataset</h4>
        <ul class="caveats">${s.removed.slice(0,40).map(r =>
          `<li>${esc(r.title)} <span class="cost-note">(${esc(r.issn_l)})</span></li>`).join("")}</ul></div>` : ""}`;
  }
  $("#detail-body").innerHTML = `
    <h2 id="detail-title">What changed recently</h2>
    <p class="cost-note">Compared with the previous build${s && s.generated
      ? ` · generated ${esc(s.generated.replace("T", " ").slice(0, 16))} UTC` : ""}.
      Deals change and sources get corrected; this is the record of it.</p>
    ${body}
    <p class="src" style="margin-top:14px">
      <a href="https://github.com/${esc(repo)}/blob/main/CHANGELOG-data.md" target="_blank" rel="noopener">Full dated changelog ↗</a> ·
      <a href="https://github.com/${esc(repo)}/commits/main/data/state/journal_state.tsv" target="_blank" rel="noopener">Every change, in git history ↗</a>
    </p>`;
  $("#detail-modal").hidden = false;
  document.body.style.overflow = "hidden";
  $("#modal-close").focus();
}

function showSearchTips() {
  const row = (syntax, means, example) =>
    `<tr><td><code>${esc(syntax)}</code></td><td>${means}</td>
     <td class="cost-note">${esc(example)}</td></tr>`;
  showModal(`
    <h2 id="detail-title">Search tips</h2>
    <p class="cost-note">Typing several words requires all of them, which is
      usually what you want. These let you be more precise.</p>
    <table class="tips-table"><tbody>
      ${row("cell biology", "Both words (AND is the default)", "matches Cell Biology International")}
      ${row('"cell biology"', "That exact phrase", "excludes Cell and Biology separately")}
      ${row("cell OR cellular", "Either word. OR must be capitals", "catches spelling variants")}
      ${row("-neuroscience", "Exclude", "science -neuroscience")}
      ${row("title:science", "Only the journal title", "the sharpest way to cut out subject matches")}
      ${row("publisher:elsevier", "Only the publisher", "publisher:wiley title:physics")}
      ${row("issn:0036-8075", "Look up an ISSN", "also works without the prefix")}
      ${row("subject:epidemiology", "Only subject keywords", "browse a field")}
    </tbody></table>
    <div class="detail-section">
      <h4>Why a search can return thousands</h4>
      <p>Results are split into journals matching <strong>by name</strong> and
        those matching only <strong>by subject</strong>, with a divider between
        them. A word like “science” appears in the subject keywords of tens of
        thousands of journals, so the subject list is long by nature —
        <code>title:science</code> removes it entirely.</p>
      <h4>How results are ordered</h4>
      <p>An exact title match ranks first, then titles starting with what you
        typed, then ISSN matches, then matches elsewhere in the title,
        alternate titles, publisher and finally subject keywords. Ties keep
        alphabetical order.</p>
    </div>`);
}

function showDisclaimer() {
  const c = STATE.config;
  $("#detail-body").innerHTML = `
    <h2 id="detail-title">Disclaimer</h2>
    <div class="detail-section disclaimer-body">
      <p><strong>This is an independent, unofficial tool.</strong> It is not run by,
        endorsed by, or checked by the Bodleian Libraries or the University of Oxford,
        and nothing on it is an offer, entitlement, or decision about funding.</p>

      <h4>The information may be wrong or out of date</h4>
      <p>Everything here is assembled automatically from third-party sources
        (the Journal Checker Tool, OpenAlex, DOAJ) plus a hand-maintained reading of
        the Bodleian's published deals page. Those sources are themselves incomplete,
        and they disagree with each other more often than you would expect. The data is
        rebuilt on a schedule, so it always lags reality: a deal can start, change,
        hit its annual cap, or end between rebuilds.</p>

      <h4>Costs shown are estimates, not quotes</h4>
      <p>Prices are list prices as published by the sources, on the retrieval date shown
        against each fact. Discounts are applied arithmetically to those list prices.
        Publishers change prices without notice, charge in different currencies, and add
        page, colour and submission fees this site does not model.</p>

      <h4>Eligibility depends on things this site cannot see</h4>
      <p>Whether a deal actually covers <em>your</em> article turns on your corresponding
        authorship, article type, funder, licence choice, submission email address, and
        whether the publisher's annual allowance is still open. The site surfaces the
        caveats it knows about, but it cannot check any of them for you.</p>

      <h4>No warranty</h4>
      <p>The information is provided "as is", without warranty of any kind, express or
        implied. The maintainers accept no liability for any loss, cost, or charge arising
        from reliance on it — including any article processing charge you become liable
        for. If you need an answer you can rely on, ask
        <a href="mailto:${esc(c.contact)}">${esc(c.contact)}</a>; the Bodleian's
        <a href="${esc(c.bodleian_deals)}" target="_blank" rel="noopener">publisher deals page</a>
        is the authoritative source, not this one.</p>

      <h4>Found something wrong?</h4>
      <p>Please report it — every journal page has a report box, and reports are tracked
        publicly at
        <a href="https://github.com/${esc(c.github_repo)}/issues" target="_blank" rel="noopener">the issue tracker</a>.
        Corrections are welcome and applied openly.</p>

      <p class="cost-note">The code and data pipeline are open source under the MIT licence,
        which also disclaims warranty and liability. Every fact links to the source it came
        from, so you can check it yourself — please do.</p>
    </div>`;
  $("#detail-modal").hidden = false;
  document.body.style.overflow = "hidden";
  $("#modal-close").focus();
}

function showAbout() {
  const c = STATE.config;
  $("#detail-body").innerHTML = `
    <h2 id="detail-title">About &amp; methodology</h2>
    <div class="detail-section">
      <p>This tool helps ${esc(c.title.includes("Oxford") ? "Oxford" : "")} researchers find whether the University's open-access agreements cover a given journal, and what publishing there costs once a deal is applied.</p>
      <h4>Where the data comes from</h4>
      <ul class="caveats">
        <li><strong>Deal coverage:</strong> the cOAlition S <a href="https://journalcheckertool.org/transformative-agreements/" target="_blank" rel="noopener">Journal Checker Tool</a> public transformative-agreement data (CC BY 4.0), filtered to Oxford's ROR, plus a hand-curated overlay for discounts and diamond deals taken from the <a href="${esc(c.bodleian_deals)}" target="_blank" rel="noopener">Bodleian deals page</a>.</li>
        <li><strong>Journal metadata &amp; APC list prices:</strong> <a href="https://openalex.org" target="_blank" rel="noopener">OpenAlex</a> (CC0) and <a href="https://doaj.org" target="_blank" rel="noopener">DOAJ</a> (metadata CC0). DOAJ's <a href="${esc(c.doaj_withdrawn_changelog)}" target="_blank" rel="noopener">withdrawal changelog</a> is separately licensed <a href="https://creativecommons.org/licenses/by-sa/4.0/" target="_blank" rel="noopener">CC BY-SA 4.0</a>, and is credited wherever a withdrawal reason is quoted.</li>
        <li><strong>Quality filter:</strong> journals are included only if they're deal-covered, in DOAJ, or from a vetted publisher; journals withdrawn from DOAJ for misconduct-type reasons are excluded. The tool never labels a journal “predatory”.</li>
        <li><strong>When sources disagree:</strong> the Journal Checker Tool and the Bodleian's own page do not always say the same thing about a publisher. Rather than silently picking one, those journals carry a “sources disagree” warning showing both claims, and you should confirm with the open access team before relying on the figure.</li>
      </ul>
      <p class="cost-note">Everything is rebuilt automatically each week by deterministic scripts, with outbound links checked for rot. This is an independent tool, not an official Bodleian Libraries service. Confirm eligibility at ${esc(c.contact)}.</p>
      <p class="src"><a href="https://github.com/${esc(c.github_repo)}" target="_blank" rel="noopener">Source code &amp; issue tracker ↗</a></p>
    </div>`;
  $("#detail-modal").hidden = false;
  document.body.style.overflow = "hidden";
  $("#modal-close").focus();
}

/* ---------------- starred journals ----------------
 *
 * Kept in localStorage: no account, no server, nothing sent anywhere. The list
 * is a handful of ISSNs and it stays on the machine that made it.
 *
 * What that costs is honest to state rather than hide: the list is per browser
 * and per device, and clearing site data clears it. The share link below is the
 * answer to both — it carries the list in the URL, so moving a shortlist to a
 * phone and sending it to a colleague are the same action.
 *
 * The key is namespaced because every GitHub Pages user site shares one origin:
 * ben18785.github.io/oxford-apc-finder and ben18785.github.io/ai-sci-resources
 * read the same localStorage, so a bare "starred" key would collide. */
const STAR_KEY = "oxford-apc-finder:starred";

function starredIds() {
  try {
    const raw = JSON.parse(localStorage.getItem(STAR_KEY) || "[]");
    return Array.isArray(raw) ? raw.filter((x) => typeof x === "string") : [];
  } catch {
    return [];               // private browsing, or someone else's junk
  }
}

function setStarred(ids) {
  // De-duplicated and capped: the comparison is unreadable past a dozen or so,
  // and an unbounded list would eventually outgrow both localStorage and the
  // share link.
  const unique = [...new Set(ids)].slice(0, 40);
  try { localStorage.setItem(STAR_KEY, JSON.stringify(unique)); } catch { /* full or blocked */ }
  STATE.starred = new Set(unique);
  updateStarUI();
  return unique;
}

function toggleStar(id) {
  const now = starredIds();
  setStarred(now.includes(id) ? now.filter((x) => x !== id) : now.concat(id));
}

/* Reflect the count in the toolbar and the pressed state of every visible star,
 * without re-running the search — starring must not reshuffle the list you are
 * reading. */
function updateStarUI() {
  const n = (STATE.starred || new Set()).size;
  const btn = $("#starred-open");
  if (btn) {
    btn.hidden = n === 0;
    btn.textContent = `Compare ${n} starred journal${n === 1 ? "" : "s"}`;
  }
  document.querySelectorAll("button.star[data-star]").forEach((b) => {
    const on = STATE.starred.has(b.dataset.star);
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.textContent = on ? "★" : "☆";
    b.title = on ? "Remove from your comparison list" : "Add to your comparison list";
  });
}

function starButton(id) {
  return `<button class="star" data-star="${esc(id)}" aria-pressed="false"
    aria-label="Add this journal to your comparison list">☆</button>`;
}

/* ---------------- comparison and export ----------------
 *
 * Journals are rows, not columns: a genuine side-by-side falls apart past three
 * or four titles and is unreadable on a phone, whereas rows stay legible at
 * forty and collapse cleanly.
 *
 * Ordered by cost so the cheapest option is the first thing read — that is the
 * question a shortlist exists to answer. */
function compareRows(ids) {
  const byId = new Map(STATE.index.map((r) => [r.id, r]));
  return ids.map((id) => byId.get(id)).filter(Boolean)
    .sort((a, b) => {
      const ga = typeof a.g === "number" ? a.g : Infinity;
      const gb = typeof b.g === "number" ? b.g : Infinity;
      return ga - gb || (a.v || 0) - (b.v || 0)
             || (a.t || "").localeCompare(b.t || "");
    });
}

/* The agreement identifier lives in the detail shard, not the index, and it is
 * the single most useful thing to quote to the open access team — it tells them
 * exactly which contract to look up. Worth a few small fetches. */
async function agreementIds(rows) {
  const out = new Map();
  await Promise.all(rows.map(async (r) => {
    try {
      const shard = await loadShard(r.id);
      const esac = ((shard[r.id] || {}).deal || {}).esac_id;
      if (esac) out.set(r.id, esac);
    } catch { /* the comparison is still useful without it */ }
  }));
  return out;
}

function shareLink(ids) {
  const base = location.href.split("#")[0];
  return `${base}#compare=${ids.join(",")}`;
}

/* Plain text rather than a file download: it is going into an email or a chat
 * message, and a .csv nobody opens helps no one. */
function compareText(rows, esacs) {
  const lines = ["Journals I am considering, and what the Oxford APC Finder says:", ""];
  rows.forEach((r, i) => {
    const [label] = STATUS_LABEL[r.s] || STATUS_LABEL.none;
    const model = (MODEL_LABEL[r.o] || [""])[0];
    lines.push(`${i + 1}. ${r.t}  (ISSN ${r.i[0] || "unknown"})`);
    lines.push(`   Publisher: ${r.p || "unknown"}${model ? " · " + model : ""}`);
    lines.push(`   Oxford deal: ${label}`);
    // costFigure, not the full summary: the line above already names the deal,
    // and r.c repeats it ("Covered by Oxford deal — £0 if eligible — covered
    // by Oxford deal").
    lines.push(`   Open access cost: ${costFigure(r).text}`);
    if (esacs.get(r.id)) lines.push(`   Agreement: ${esacs.get(r.id)}`);
    lines.push("");
  });
  lines.push(`Compiled with ${shareLink(rows.map((r) => r.id))}`);
  lines.push(`Unofficial tool; data generated ${(STATE.generated || "").slice(0, 10)}.`);
  return lines.join("\n");
}

/* A pre-structured enquiry, not a blank email. The open access team get the
 * same shape of question every time, with the agreement identifiers already
 * quoted — which is the difference between a query they can answer and one they
 * have to research from scratch. */
function bodleianMail(rows, esacs) {
  const body = [
    "Dear Open Access team,",
    "",
    "I am considering submitting to the journals below, and would like to confirm",
    "what Oxford's agreements would cover for me as corresponding author.",
    "",
  ];
  rows.forEach((r, i) => {
    const [label] = STATUS_LABEL[r.s] || STATUS_LABEL.none;
    body.push(`${i + 1}. ${r.t} (ISSN ${r.i[0] || "unknown"})`);
    body.push(`   Publisher: ${r.p || "unknown"}`);
    body.push(`   The APC Finder shows: ${label} — ${costFigure(r).text}`);
    if (esacs.get(r.id)) body.push(`   Agreement identifier: ${esacs.get(r.id)}`);
    body.push("");
  });
  body.push("Could you confirm whether the APC would be covered in each case,");
  body.push("and flag anything I should be aware of before submitting?");
  body.push("");
  body.push("Many thanks,");
  body.push("");
  body.push("---");
  body.push("Compiled with the Oxford APC Finder, an unofficial tool:");
  body.push(shareLink(rows.map((r) => r.id)));
  body.push(`Its figures are automated and may be out of date (data generated ${(STATE.generated || "").slice(0, 10)}).`);
  const subject = `Open access coverage query — ${rows.length} journal${rows.length === 1 ? "" : "s"}`;
  return `mailto:${STATE.config.contact}?subject=${encodeURIComponent(subject)}`
         + `&body=${encodeURIComponent(body.join("\n"))}`;
}

// Long mailto bodies are silently truncated by some clients, so past this many
// journals the email is offered as a clipboard copy instead of a broken link.
const MAILTO_MAX_JOURNALS = 8;

async function showCompare(ids) {
  const rows = compareRows(ids);
  if (!rows.length) {
    return showModal(`<h2 id="detail-title">Nothing starred yet</h2>
      <p class="cost-note">Use the ☆ beside a journal to add it here, then come
        back to compare them side by side and send the list to the open access
        team.</p>`);
  }
  const esacs = await agreementIds(rows);
  const body = rows.map((r) => {
    const fig = costFigure(r);
    const [label] = STATUS_LABEL[r.s] || STATUS_LABEL.none;
    return `<tr>
      <td>${starButton(r.id)}<strong>${esc(r.t)}</strong>
        <div class="jmeta">${esc(r.p || "Publisher unknown")} · ${esc(r.i[0] || "")}</div></td>
      <td>${modelBadge(r.o)}</td>
      <td>${esc(label)}</td>
      <td class="num cost-cell ${fig.cls}">${esc(fig.text)}</td>
    </tr>`;
  }).join("");

  const tooLong = rows.length > MAILTO_MAX_JOURNALS;
  $("#detail-body").innerHTML = `
    <h2 id="detail-title">Your shortlist</h2>
    <p class="cost-note">Ordered by cost, cheapest first. Starring is stored in
      this browser only — nothing is sent anywhere and there is no account. Use
      the link below to move the list to another device or send it to someone.</p>
    <table class="compare-table">
      <thead><tr><th>Journal</th><th>Model</th><th>Oxford deal</th>
        <th class="num">Open access cost</th></tr></thead>
      <tbody>${body}</tbody>
    </table>
    <div class="compare-actions">
      ${tooLong ? "" : `<a class="btn" id="compare-mail" href="${esc(bodleianMail(rows, esacs))}">Email the open access team ↗</a>`}
      <button class="btn secondary" id="compare-copy">Copy as text</button>
      <button class="btn secondary" id="compare-clear">Clear the list</button>
    </div>
    ${tooLong ? `<p class="cost-note">With more than ${MAILTO_MAX_JOURNALS} journals
      the pre-filled email would be truncated by some mail clients, so copy the
      text instead and paste it into a message to ${esc(STATE.config.contact)}.</p>` : ""}
    <div class="detail-section">
      <h4>Share this list</h4>
      <p class="cost-note">Opening this link anywhere shows the same comparison.
        It carries only the ISSNs — no identity, nothing about you.</p>
      <input class="share-box" id="share-url" readonly
             value="${esc(shareLink(rows.map((r) => r.id)))}">
    </div>`;
  $("#detail-modal").hidden = false;
  document.body.style.overflow = "hidden";

  const copyText = compareText(rows, esacs);
  $("#compare-copy").addEventListener("click", (e) => {
    const done = () => { e.target.textContent = "Copied"; };
    try {
      if (navigator.clipboard) navigator.clipboard.writeText(copyText).then(done, done);
      else done();
    } catch { done(); }
  });
  $("#compare-clear").addEventListener("click", () => {
    setStarred([]);
    closeModal();
    runSearch();
  });
  const share = $("#share-url");
  if (share && share.addEventListener) {
    share.addEventListener("focus", () => share.select && share.select());
  }
  $("#modal-close").focus();
}

/* ---------------- usage monitoring ----------------
 *
 * Cookieless and aggregate-only. GoatCounter sets no cookie, stores no IP
 * address, and issues no cross-site identifier, so there is nothing here that
 * needs a consent banner and nothing that follows a reader off this page.
 *
 * Everything below is a no-op unless config.json carries a goatcounter_code —
 * with monitoring off, no third-party script is fetched at all. Every call is
 * also guarded against the script being blocked, which it often is: an ad
 * blocker must degrade the counting, never the tool. */
function initAnalytics() {
  const code = STATE.config && STATE.config.goatcounter_code;
  if (!code) return;
  window.goatcounter = { path: () => location.pathname || "/" };
  const s = document.createElement("script");
  s.async = true;
  s.src = "https://gc.zgo.at/count.js";
  s.setAttribute("data-goatcounter", `https://${code}.goatcounter.com/count`);
  document.head.appendChild(s);
}

/* Record one event. Silent by design: a failure here must be invisible. */
function track(path, title) {
  try {
    const gc = window.goatcounter;
    if (gc && typeof gc.count === "function") {
      gc.count({ path, title: title || "", event: true });
    }
  } catch { /* counting is never worth an error in the console */ }
}

/* The deal status rides in the path rather than going as a second event, so
 * one call yields both the per-journal count and the coverage split — and the
 * two cannot drift apart the way separate counters would.
 *
 * The filter state rides along too, because without it the coverage share is
 * not a finding, it is an artefact: "Only show journals with an Oxford deal"
 * is ON by default, so most readers are choosing from a list that is already
 * 100% covered. Only views taken with the filter OFF say anything about what
 * people would have picked freely, and only those are used for the share. */
function trackJournalView(rec) {
  if (!rec) return;
  const el = $("#deal-only");
  const scope = el && el.checked ? "deals" : "all";
  track(`j/${rec.s || "none"}/${scope}/${rec.id}`, rec.t || "");
}

/* Only searches that found NOTHING are recorded, and only the normalised
 * terms. A zero-result search is the one query whose text is actually useful —
 * it is a coverage gap, and the list of them is a work queue for the
 * publisher allowlist. Recording every search instead would be a far larger
 * pile of free text for no added insight.
 *
 * Held well past the 120ms search debounce so only a finished query is
 * recorded. Without this, typing a missing title one letter at a time posts
 * every prefix that happens to match nothing, and the resulting list is a
 * tower of fragments rather than the thing the reader was looking for. */
let missedTimer;
function trackMissedSearch(raw) {
  const q = (raw || "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ")
    .replace(/\s+/g, " ").trim().slice(0, 60);
  clearTimeout(missedTimer);
  if (q.length < 3) return;
  missedTimer = setTimeout(() => track(`missing/${q}`, "(no results)"), 2500);
}

/* ---------------- wiring ---------------- */
let debounce;
function wireUI() {
  $("#q").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(runSearch, 120); });
  $("#search-form").addEventListener("submit", e => { e.preventDefault(); runSearch(); });
  $("#deal-only").addEventListener("change", runSearch);
  $("#free-only").addEventListener("change", runSearch);
  $("#sort").addEventListener("change", runSearch);
  $("#starred-open").addEventListener("click", () => showCompare(starredIds()));
  $("#modal-close").addEventListener("click", closeModal);
  $("#detail-modal").addEventListener("click", e => { if (e.target.id === "detail-modal") closeModal(); });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { closeWhy(); closeModal(); }
  });
  document.addEventListener("click", e => {
    const star = e.target.closest(".star");
    if (star) {
      e.preventDefault(); e.stopPropagation();
      toggleStar(star.dataset.star);
      return;
    }
    const btn = e.target.closest(".why");
    if (btn) {
      e.preventDefault(); e.stopPropagation();
      if (openWhy === btn) closeWhy(); else showWhy(btn);
      return;
    }
    if (!e.target.closest("#why-pop")) closeWhy();
  });
  window.addEventListener("resize", closeWhy);
  $("#foot-status").addEventListener("click", e => { e.preventDefault(); showStatus(); });
  $("#foot-about").addEventListener("click", e => { e.preventDefault(); showAbout(); });
  // Offered only when a build actually published figures, so the link can
  // never lead to an empty page. The separator lives inside the wrapper in the
  // markup rather than being injected next to the link: showing a link and its
  // punctuation is one decision, so it should be one toggle.
  const usageWrap = $("#foot-usage-wrap");
  if (usageWrap && STATE.config.usage_available) {
    usageWrap.hidden = false;
    $("#foot-usage").addEventListener("click", e => { e.preventDefault(); showUsage(); });
  }
  $("#disclaimer-link").addEventListener("click", e => { e.preventDefault(); showDisclaimer(); });
  $("#search-tips").addEventListener("click", e => { e.preventDefault(); showSearchTips(); });
  // Always reachable, not only when a search comes up empty: someone who finds
  // nothing usually concludes the tool is broken rather than that the journal
  // sits outside its coverage — so make reporting the gap a visible option.
  const missingReportUrl = () => {
    const q = $("#q").value.trim();
    const body = encodeURIComponent(
      `**Journal I could not find:** ${q || "(describe it here)"}\n\n` +
      `**Why I expected it to be listed:**\n\n\n---\n` +
      `_Reported from the site. It may sit outside the inclusion rules; if so, ` +
      `the rules need widening._`);
    return `https://github.com/${STATE.config.github_repo}/issues/new` +
           `?title=${encodeURIComponent("Missing journal: " + (q || "?"))}` +
           `&labels=user-report&body=${body}`;
  };
  $("#missing-journal").addEventListener("click", e => {
    e.currentTarget.href = missingReportUrl();
  });
  $("#empty-report").addEventListener("click", e => {
    e.currentTarget.href = missingReportUrl();
  });

  $("#empty-clear-filter").addEventListener("click", e => {
    e.preventDefault(); $("#deal-only").checked = false; runSearch();
  });
  $("#foot-disclaimer").addEventListener("click", e => { e.preventDefault(); showDisclaimer(); });
  $("#foot-changes").addEventListener("click", e => { e.preventDefault(); showChanges(); });
}

boot().catch(err => {
  document.querySelector("main").innerHTML =
    `<p class="empty">Could not load data: ${esc(err.message)}</p>`;
  console.error(err);
});
