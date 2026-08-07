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
  waiver: ["APC waivers available",
    "<p>The journal states it will waive or reduce its charge for authors who cannot pay, typically those in lower-income countries. Terms are set by the journal, not by Oxford.</p>"],
};

const STATUS_LABEL = {
  covered: ["Covered by Oxford deal", "covered"],
  discount: ["Discount available", "discount"],
  diamond: ["Free to publish (diamond)", "diamond"],
  none: ["No Oxford deal", "none"],
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
  wireUI();
  runSearch();
  loadKeywords();
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
  const { title, alt, pub, issns, ids } = parts;
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

function runSearch() {
  if (!STATE.loaded) return;
  const raw = $("#q").value.trim();
  const dealOnly = $("#deal-only").checked;

  if (!raw) {
    // merge.py already emits journals sorted by title, so the index arrives in
    // display order — re-sorting 43k records on every empty search changes
    // nothing and costs hundreds of milliseconds.
    const pool = dealOnly ? STATE.index.filter((r) => r.s !== "none") : STATE.index;
    STATE.results = pool;
    STATE.nominalCount = pool.length;
    STATE.hiddenByFilter = [];
    return renderResults(pool.slice(0, 200), pool.length, pool.length, []);
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
    if (dealOnly && r.s === "none") {
      if (nominal) hidden.push({ r, sc });
      continue;
    }
    (nominal ? byName : bySubject).push({ r, sc });
  }
  const rank = (a, b) => b.sc - a.sc;
  byName.sort(rank); bySubject.sort(rank); hidden.sort(rank);

  const matches = byName.concat(bySubject).map((x) => x.r);
  STATE.results = matches;
  STATE.nominalCount = byName.length;
  STATE.hiddenByFilter = hidden.map((x) => x.r);
  renderResults(matches.slice(0, 200), matches.length, byName.length,
                STATE.hiddenByFilter);
}

/* ---------------- rendering ---------------- */
function why(key) {
  const [title] = EXPLAIN[key] || [];
  if (!title) return "";
  return ` <button class="why" data-explain="${esc(key)}"
    aria-label="What does &quot;${esc(title)}&quot; mean?">?</button>`;
}

function badge(status, inDoaj, disputed, expired) {
  const [label, cls] = STATUS_LABEL[status] || STATUS_LABEL.none;
  let html = `<span class="badge ${cls}">${esc(label)}${why(status)}</span>`;
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
    <p class="cost-note">The cost shown below follows the Journal Checker Tool.
      Confirm with ${esc(STATE.config.contact)} before submitting.</p>
  </div>`;
}

/* Split the cost summary so the figure can be right-aligned on its own.
 * cost_summary() in build_site.py produces e.g. "£0 — covered by Oxford deal";
 * the ledger shows only the figure, since the status column says the rest. */
function costFigure(rec) {
  const c = rec.c || "";
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
  $("#result-count").textContent = !total ? ""
    : (subject > 0 && nominalCount !== total
        ? `${n(nominalCount)} by name · ${n(subject)} more by subject`
        : `${n(total)} journal${total === 1 ? "" : "s"}`)
      + (total > list.length ? ` (showing ${n(list.length)})` : "");

  let html = "";

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
        r.d ? `<span class="badge doaj">In DOAJ${why("doaj")}</span>` : "",
        r.x ? `<span class="badge disputed">⚠ Sources disagree${why("disputed")}</span>` : "",
        r.e ? `<span class="badge expired">⚠ Agreement ended${why("expired")}</span>` : "",
      ].filter(Boolean).join("");
      const [label] = STATUS_LABEL[r.s] || STATUS_LABEL.none;
      return `${brk}
        <tr data-id="${esc(r.id)}">
          <td>
            <button class="jtitle">${esc(r.t)}</button>
            <div class="jmeta">${esc(r.p || "Publisher unknown")} · ${esc(r.i[0] || "")}</div>
            ${flags ? `<div class="flags">${flags}</div>` : ""}
          </td>
          <td class="state"><span class="swatch sw-${esc(r.s)}"></span>${esc(label)}${why(r.s)}</td>
          <td class="cost-cell ${fig.cls}">${esc(fig.text)}</td>
        </tr>`;
    }).join("");

    html += `<table class="ledger">
      <thead><tr><th>Journal</th><th>Oxford deal</th><th class="num">Cost to you</th></tr></thead>
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
  box.querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".why")) return;
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
  const scope = j.scope || {};
  const wd = j.doaj_withdrawn;
  const rep = reportLinks(j);

  const body = `
    <div class="detail-head">
      <h2 id="detail-title">${esc(j.title)}</h2>
      <p class="pub">${esc(j.publisher || "Publisher unknown")}</p>
      <p class="detail-issn">ISSN: ${j.issns.map(esc).join(" · ")}</p>
      <div class="badge-row">${badge(j.deal.status, j.in_doaj, j.deal.disputed, j.deal.expired)}
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
      <p class="basis">${esc(j.deal.basis || "")}</p>
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
      <div class="stat"><div class="n">${(c.total||0).toLocaleString()}</div><div class="l">journals</div></div>
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

/* ---------------- wiring ---------------- */
let debounce;
function wireUI() {
  $("#q").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(runSearch, 120); });
  $("#search-form").addEventListener("submit", e => { e.preventDefault(); runSearch(); });
  $("#deal-only").addEventListener("change", runSearch);
  $("#modal-close").addEventListener("click", closeModal);
  $("#detail-modal").addEventListener("click", e => { if (e.target.id === "detail-modal") closeModal(); });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { closeWhy(); closeModal(); }
  });
  document.addEventListener("click", e => {
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
  $("#disclaimer-link").addEventListener("click", e => { e.preventDefault(); showDisclaimer(); });
  $("#search-tips").addEventListener("click", e => { e.preventDefault(); showSearchTips(); });
  $("#empty-clear-filter").addEventListener("click", e => {
    e.preventDefault(); $("#deal-only").checked = false; runSearch();
  });
  // A journal absent from the dataset is a coverage gap we want reported, not
  // a dead end. Pre-fill the report with what was actually searched for.
  $("#empty-report").addEventListener("click", () => {
    const q = $("#q").value.trim();
    const body = encodeURIComponent(
      `**Journal I searched for:** ${q}\n\n**Why I expected it to be listed:**\n\n\n` +
      `---\n_Reported from the empty-results message; the journal appears to be ` +
      `outside the site's inclusion rules._`);
    $("#empty-report").href =
      `https://github.com/${STATE.config.github_repo}/issues/new` +
      `?title=${encodeURIComponent("Missing journal: " + q)}` +
      `&labels=user-report&body=${body}`;
  });
  $("#foot-disclaimer").addEventListener("click", e => { e.preventDefault(); showDisclaimer(); });
  $("#foot-changes").addEventListener("click", e => { e.preventDefault(); showChanges(); });
}

boot().catch(err => {
  document.querySelector("main").innerHTML =
    `<p class="empty">Could not load data: ${esc(err.message)}</p>`;
  console.error(err);
});
