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
  STATE.loaded = true;
  wireUI();
  runSearch();
}

function applyConfig() {
  const c = STATE.config;
  document.title = c.title;
  $("#site-title").textContent = c.title;
  $("#site-tagline").textContent = c.tagline;
  $("#foot-bod").href = c.bodleian_deals;
  $("#foot-repo").href = `https://github.com/${c.github_repo}`;
}

async function loadShard(id) {
  const key = id.slice(0, 2);
  if (STATE.shards[key]) return STATE.shards[key];
  const data = await (await fetch(`data/details/${key}.json`)).json();
  STATE.shards[key] = data;
  return data;
}

/* ---------------- search ---------------- */
function tokenize(s) { return s.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean); }

function scoreRecord(rec, terms, rawQuery) {
  const title = (rec.t || "").toLowerCase();
  const alt = (rec.a || []).join(" ").toLowerCase();
  const pub = (rec.p || "").toLowerCase();
  const issns = (rec.i || []).join(" ");
  const kw = rec.k || "";
  const hay = `${title} ${alt} ${pub} ${kw}`;
  let score = 0;
  if (rawQuery && title === rawQuery) score += 1000;
  if (rawQuery && title.startsWith(rawQuery)) score += 200;
  if (rawQuery && issns.includes(rawQuery.replace(/\s/g, ""))) score += 500;
  for (const t of terms) {
    if (title.includes(t)) score += 40;
    if (title.split(/\s+/).includes(t)) score += 25;
    if (alt.includes(t)) score += 15;
    if (pub.includes(t)) score += 10;
    if (kw.includes(t)) score += 8;
    if (!hay.includes(t)) score -= 100; // require every term to match somewhere
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
    matches = pool.slice().sort((a, b) => (a.t || "").localeCompare(b.t || ""));
  } else {
    matches = pool
      .map(r => ({ r, sc: scoreRecord(r, terms, raw) }))
      .filter(x => x.sc > 0)
      .sort((a, b) => b.sc - a.sc)
      .map(x => x.r);
  }
  STATE.results = matches;
  renderResults(matches.slice(0, 200), matches.length);
}

/* ---------------- rendering ---------------- */
function badge(status, inDoaj) {
  const [label, cls] = STATUS_LABEL[status] || STATUS_LABEL.none;
  let html = `<span class="badge ${cls}">${esc(label)}</span>`;
  if (inDoaj) html += ` <span class="badge doaj">In DOAJ</span>`;
  return html;
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
      <div class="badge-row">${badge(r.s, r.d)}</div>
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
  const shard = await loadShard(id);
  const j = shard[id];
  if (!j) return;
  const scope = j.scope || {};
  const wd = j.doaj_withdrawn;
  const rep = reportLinks(j);

  const body = `
    <div class="detail-head">
      <h2 id="detail-title">${esc(j.title)}</h2>
      <p class="pub">${esc(j.publisher || "Publisher unknown")}</p>
      <p class="detail-issn">ISSN: ${j.issns.map(esc).join(" · ")}</p>
      <div class="badge-row">${badge(j.deal.status, j.in_doaj)}
        ${j.waiver ? '<span class="badge doaj">APC waivers available</span>' : ""}</div>
    </div>

    ${wd ? `<div class="detail-section"><div class="warn">Note: this journal was recorded as withdrawn from DOAJ (${esc(wd.date)}) — reason: “${esc(wd.reason)}”.</div></div>` : ""}

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

function showAbout() {
  const c = STATE.config;
  $("#detail-body").innerHTML = `
    <h2 id="detail-title">About &amp; methodology</h2>
    <div class="detail-section">
      <p>This tool helps ${esc(c.title.includes("Oxford") ? "Oxford" : "")} researchers find whether the University's open-access agreements cover a given journal, and what publishing there costs once a deal is applied.</p>
      <h4>Where the data comes from</h4>
      <ul class="caveats">
        <li><strong>Deal coverage:</strong> the cOAlition S <a href="https://journalcheckertool.org/transformative-agreements/" target="_blank" rel="noopener">Journal Checker Tool</a> public transformative-agreement data (CC BY 4.0), filtered to Oxford's ROR, plus a hand-curated overlay for discounts and diamond deals taken from the <a href="${esc(c.bodleian_deals)}" target="_blank" rel="noopener">Bodleian deals page</a>.</li>
        <li><strong>Journal metadata &amp; APC list prices:</strong> <a href="https://openalex.org" target="_blank" rel="noopener">OpenAlex</a> (CC0) and <a href="https://doaj.org" target="_blank" rel="noopener">DOAJ</a> (metadata CC0).</li>
        <li><strong>Quality filter:</strong> journals are included only if they're deal-covered, in DOAJ, or from a vetted publisher; journals withdrawn from DOAJ for misconduct-type reasons are excluded. The tool never labels a journal “predatory”.</li>
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
}

boot().catch(err => {
  document.querySelector("main").innerHTML =
    `<p class="empty">Could not load data: ${esc(err.message)}</p>`;
  console.error(err);
});
