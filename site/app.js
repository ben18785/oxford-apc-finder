/* Oxford Journal APC Finder — client-side search & rendering.
 * Pure vanilla JS, no dependencies. Search index loads once; full journal
 * detail records load lazily per shard when a journal is opened. */
"use strict";

const STATE = { config: null, index: [], loaded: false, shards: {}, results: [] };
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));

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

/* ---------------- search ---------------- */
function tokenize(s) { return s.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean); }

function scoreRecord(rec, terms, rawQuery, termVocab) {
  const title = (rec.t || "").toLowerCase();
  const alt = (rec.a || []).join(" ").toLowerCase();
  const pub = (rec.p || "").toLowerCase();
  const issns = (rec.i || []).join(" ");
  const ids = STATE.kwReady ? (STATE.kwIds[rec.n] || []) : null;
  let score = 0;
  if (rawQuery && title === rawQuery) score += 1000;
  if (rawQuery && title.startsWith(rawQuery)) score += 200;
  if (rawQuery && issns.includes(rawQuery.replace(/\s/g, ""))) score += 500;
  for (let ti = 0; ti < terms.length; ti++) {
    const t = terms[ti];
    const inKeywords = ids !== null && ids.some(id => termVocab[ti].has(id));
    let hit = false;
    if (title.includes(t)) { score += 40; hit = true; }
    if (title.split(/\s+/).includes(t)) score += 25;
    if (alt.includes(t)) { score += 15; hit = true; }
    if (pub.includes(t)) { score += 10; hit = true; }
    if (inKeywords) { score += 8; hit = true; }
    // Tokenising splits an ISSN on its hyphen ("0001-0383" -> "0001","0383"),
    // and those digits appear in no title — so ISSNs must be a match source in
    // their own right or the AND rule below discards an exact ISSN lookup.
    if (issns.includes(t)) { score += 5; hit = true; }
    // Every term must match somewhere. Returning 0 (runSearch drops anything
    // <= 0) rather than applying a penalty: a penalty is outweighed by a
    // strong title match, so "immunology zzzz" would still return Immunology.
    if (!hit) return 0;
  }
  return score;
}

function runSearch() {
  if (!STATE.loaded) return;
  const raw = $("#q").value.trim().toLowerCase();
  const dealOnly = $("#deal-only").checked;
  const terms = tokenize(raw);
  let pool = STATE.index;
  if (dealOnly) pool = pool.filter(r => r.s !== "none");

  let matches;
  if (!raw) {
    // merge.py already emits journals sorted by title, so the index arrives in
    // display order — re-sorting 43k records with localeCompare on every empty
    // search costs hundreds of milliseconds and changes nothing.
    matches = pool;
  } else {
    const termVocab = terms.map(vocabMatches);
    matches = pool
      .map(r => ({ r, sc: scoreRecord(r, terms, raw, termVocab) }))
      .filter(x => x.sc > 0)
      .sort((a, b) => b.sc - a.sc)
      .map(x => x.r);
  }
  STATE.results = matches;
  renderResults(matches.slice(0, 200), matches.length);
}

/* ---------------- rendering ---------------- */
function badge(status, inDoaj, disputed, expired) {
  const [label, cls] = STATUS_LABEL[status] || STATUS_LABEL.none;
  let html = `<span class="badge ${cls}">${esc(label)}</span>`;
  if (inDoaj) html += ` <span class="badge doaj">In DOAJ</span>`;
  if (disputed) html += ` <span class="badge disputed" title="Oxford's own page and the Journal Checker Tool disagree about this deal">⚠ Sources disagree</span>`;
  if (expired) html += ` <span class="badge expired" title="The agreement's stated end date has passed">⚠ Agreement ended</span>`;
  return html;
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

function renderResults(list, total) {
  const box = $("#results");
  $("#empty").hidden = total > 0;
  $("#result-count").textContent = total
    ? `${total.toLocaleString()} journal${total === 1 ? "" : "s"}${total > list.length ? ` (showing ${list.length})` : ""}`
    : "";
  box.innerHTML = list.map(r => `
    <button class="jcard" data-id="${esc(r.id)}">
      <div class="jcard-top">
        <h3>${esc(r.t)}</h3>
      </div>
      <p class="pub">${esc(r.p || "Publisher unknown")}</p>
      <div class="cost">${esc(r.c)}</div>
      <div class="badge-row">${badge(r.s, r.d, r.x, r.e)}</div>
    </button>`).join("");
  box.querySelectorAll(".jcard").forEach(el =>
    el.addEventListener("click", () => openDetail(el.dataset.id)));
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
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });
  $("#foot-status").addEventListener("click", e => { e.preventDefault(); showStatus(); });
  $("#foot-about").addEventListener("click", e => { e.preventDefault(); showAbout(); });
  $("#disclaimer-link").addEventListener("click", e => { e.preventDefault(); showDisclaimer(); });
  $("#foot-disclaimer").addEventListener("click", e => { e.preventDefault(); showDisclaimer(); });
  $("#foot-changes").addEventListener("click", e => { e.preventDefault(); showChanges(); });
}

boot().catch(err => {
  document.querySelector("main").innerHTML =
    `<p class="empty">Could not load data: ${esc(err.message)}</p>`;
  console.error(err);
});
