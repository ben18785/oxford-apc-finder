/* Headless test of site/app.js search behaviour against a real build.
 *
 * The search index, the lazily-loaded keyword file and the scoring rules are
 * the part of this project users touch directly, and the Python suite cannot
 * reach any of it. This runs app.js with a stub DOM and a stub fetch, so it
 * needs no browser.
 *
 * Runs under both engines, because CI has Node and macOS ships JavaScriptCore
 * but neither is guaranteed on a given machine:
 *
 *   node tests/frontend/search.test.js [site-dir]
 *   /System/.../JavaScriptCore.framework/.../jsc tests/frontend/search.test.js
 *
 * Exits non-zero if any check fails (jsc reports via the final line).
 */
"use strict";

var IS_JSC = (typeof print === "function" && typeof readFile === "function");
var SITE = "_site";

function say(msg) { if (IS_JSC) { print(msg); } else { console.log(msg); } }
function readTextFile(path) {
  return IS_JSC ? readFile(path) : require("fs").readFileSync(path, "utf8");
}
function runProgram(path) {
  // app.js is strict-mode; a plain eval() would scope its declarations to the
  // eval and hide STATE/runSearch. Both engines need a real program load.
  if (IS_JSC) { load(path); }
  else { require("vm").runInThisContext(readTextFile(path), { filename: path }); }
}
function fail(msg) {
  say("FAIL  " + msg);
  FAILURES++;
}

var FAILURES = 0;
function check(label, ok, detail) {
  if (ok) { say("  ok  " + label); }
  else { fail(label + (detail ? "  (" + detail + ")" : "")); }
}

/* ---------------------------------------------------------- stub DOM */
function El(id) {
  this.id = id; this.value = ""; this.checked = (id === "#deal-only");
  this.innerHTML = ""; this.textContent = ""; this.hidden = false;
  this.href = ""; this.style = {}; this.dataset = {};
}
El.prototype.addEventListener = function () {};
El.prototype.querySelectorAll = function () { return { forEach: function () {} }; };
El.prototype.focus = function () {};
El.prototype.appendChild = function (c) { return c; };
El.prototype.setAttribute = function () {};

var ELS = {};
function el(sel) { if (!ELS[sel]) { ELS[sel] = new El(sel); } return ELS[sel]; }

globalThis.document = {
  querySelector: el, addEventListener: function () {},
  body: { style: {} }, title: ""
};
globalThis.location = { href: "http://localhost/" };
/* The popover reads window geometry and listens for resize. A browser always
 * has these; the harness previously modelled document but not window, so any
 * window reference silently rejected boot() and left the keyword index
 * unloaded — with the only symptom a couple of unrelated-looking failures. */
globalThis.window = {
  addEventListener: function () {}, innerWidth: 1280, innerHeight: 900,
  scrollX: 0, scrollY: 0,
};
globalThis.document.body.appendChild = function (c) { return c; };
globalThis.document.createElement = function () { return new El("created"); };
globalThis.setTimeout = function (fn) { fn(); return 0; };
globalThis.clearTimeout = function () {};
/* Mirrors the parts of the fetch Response the app uses: a missing file must
 * present as {ok:false, status:404}, not as a thrown read error, or the app's
 * own error handling is never exercised. */
globalThis.fetch = function (path) {
  var text;
  try { text = readTextFile(SITE + "/" + path); }
  catch (e) { return Promise.resolve({ ok: false, status: 404,
    json: function () { return Promise.reject(new Error("404")); } }); }
  return Promise.resolve({ ok: true, status: 200,
    json: function () { return Promise.resolve(JSON.parse(text)); } });
};
if (typeof console === "undefined") {          // jsc has no console
  globalThis.console = { log: say, warn: function () {}, error: function () {} };
}

runProgram("site/app.js");

/* boot() and the lazy keyword load are async but every stubbed promise is
 * already resolved, so a fixed number of microtask hops settles them. */
var chain = Promise.resolve();
for (var i = 0; i < 30; i++) { chain = chain.then(function () {}); }

chain.then(function () {
  // app.js declares STATE/runSearch with const/function at program scope.
  // const creates a *lexical* global binding, which is not a property of
  // globalThis — so reference the bare identifiers, which resolve via the
  // global scope chain under both engines.
  function search(query, dealOnly) {
    el("#q").value = query;
    el("#deal-only").checked = !!dealOnly;
    runSearch();
    return STATE.results;
  }

  /* ------------------------------------------------------ data loading */
  check("index loads", STATE.index.length > 0, "got " + STATE.index.length);
  check("keyword index loads", STATE.kwReady === true);
  check("vocabulary is populated", (STATE.vocab || []).length > 0);
  check("keyword rows align with index",
    STATE.kwIds.length === STATE.index.length,
    STATE.kwIds.length + " vs " + STATE.index.length);
  check("header GitHub link is set",
    el("#header-repo").href.indexOf("https://github.com/") === 0,
    el("#header-repo").href);

  /* ----------------------------------------------------------- search */
  var first = STATE.index[0];

  check("exact title search finds the journal",
    search(first.t).some(function (r) { return r.id === first.id; }),
    "searched " + JSON.stringify(first.t));

  check("ISSN search finds exactly that journal",
    search(first.i[0]).filter(function (r) { return r.id === first.id; }).length === 1);

  check("empty query returns every journal",
    search("").length === STATE.index.length);

  check("deal-only filter excludes journals with no deal",
    search("", true).every(function (r) { return r.s !== "none"; }));

  var on = search("", true).length, off = search("", false).length;
  check("deal-only is a non-empty strict subset", on > 0 && on <= off,
    on + " of " + off);

  check("nonsense query returns nothing", search("zzzqqxwv").length === 0);

  check("multi-word query requires every term to match",
    search("zzzqqxwv " + first.t).length === 0);

  /* Keyword search must reach journals whose *title* lacks the term —
   * otherwise the lazily-loaded index is not actually being consulted. */
  var kw = search("biology");
  check("keyword search returns hits", kw.length > 0);
  check("keyword search goes beyond title matches",
    kw.some(function (r) { return (r.t || "").toLowerCase().indexOf("biology") === -1; }));

  /* ----------------------------------------------------- result shape */
  var all = search("");
  check("every result carries a cost summary",
    all.every(function (r) { return typeof r.c === "string" && r.c.length > 0; }));

  check("every result has a known deal status",
    all.every(function (r) {
      return ["covered", "discount", "diamond", "none"].indexOf(r.s) !== -1;
    }));

  var titles = all.map(function (r) { return (r.t || "").toLowerCase(); });
  var sorted = titles.slice().sort();
  check("index is pre-sorted by title (the app relies on this)",
    JSON.stringify(titles) === JSON.stringify(sorted));

  /* ------------------------------------------------- the ledger -------
   * Results render as a table with the cost in its own right-aligned column,
   * and jargon labels carry a "?" explainer. */
  search("");
  var resultsHtml = el("#results").innerHTML;
  check("results render as a ledger table",
    resultsHtml.indexOf("<table class=\"ledger\"") !== -1);
  check("the ledger has a cost column, labelled as an OPEN ACCESS charge",
    resultsHtml.indexOf("cost-cell") !== -1
    && resultsHtml.indexOf("Open access cost") !== -1,
    "every price on the site is an OA charge, and the column must say so");
  check("each row exposes its journal id for opening the detail",
    /<tr data-id="\d{4}-\d{3}[\dX]"/.test(resultsHtml));
  check("status labels carry a ? explainer",
    resultsHtml.indexOf('class="why" data-explain=') !== -1);

  var keys = Object.keys(EXPLAIN);
  check("every deal status has an explanation",
    ["covered", "discount", "diamond", "none"].every(function (k) {
      return keys.indexOf(k) !== -1; }));
  check("the jargon labels users asked about are explained",
    ["doaj", "disputed", "expired"].every(function (k) {
      return keys.indexOf(k) !== -1; }));
  check("explanations are non-trivial prose",
    keys.every(function (k) {
      return EXPLAIN[k][0] && EXPLAIN[k][1].length > 80; }));
  check("the DOAJ explanation says it is not a quality ranking",
    /not a ranking of quality/i.test(EXPLAIN.doaj[1]),
    "DOAJ indexes process, not quality — implying otherwise misleads");

  /* --------------------------------------------- query language -------
   * Regression: quoting a query left the quote marks in the string used for
   * the exact-title bonus, so `"science"` dropped the journal Science from
   * 1st to 48th — quoting made precision worse. Field scoping had the same
   * fault (`title:science` ranked it 375th). */
  var exact = STATE.index.filter(function (r) {
    return (r.t || "").toLowerCase() === "science"; })[0];
  if (exact) {
    check("unquoted exact title ranks first",
      search("science")[0].id === exact.id);
    check("QUOTED exact title still ranks first",
      search('"science"')[0].id === exact.id, "quoting must not break ranking");
    check("field-scoped exact title still ranks first",
      search("title:science")[0].id === exact.id);
  }

  check("title: scope excludes subject-only matches",
    search("title:science").every(function (r) {
      return (r.t || "").toLowerCase().indexOf("science") !== -1
          || (r.a || []).join(" ").toLowerCase().indexOf("science") !== -1; }),
    "a title-scoped search returned something with no such title");

  /* The following assert PROPERTIES of the matching rules, using terms taken
   * from whichever corpus is loaded. Hardcoding real-world examples ("ear",
   * "Sciences") passed locally against 43k journals and failed in CI against
   * the 11-journal fixture build — the test must not assume its own data. */

  // A word that appears in at least two titles, so scoped searches are non-empty.
  var freq = {};
  STATE.index.forEach(function (r) {
    ((r.t || "").toLowerCase().match(/[a-z]{4,}/g) || []).forEach(function (w) {
      freq[w] = (freq[w] || 0) + 1; });
  });
  var common = Object.keys(freq).sort(function (a, b) { return freq[b] - freq[a]; })[0];

  check("a common title word was found to test with", !!common, String(common));

  var scoped = search("title:" + common);
  var unscoped = new Set(search(common).map(function (r) { return r.id; }));
  check("title: results are a subset of the unscoped search",
    scoped.length > 0 && scoped.every(function (r) { return unscoped.has(r.id); }));

  check("every title: match has the term at a WORD START in a title",
    scoped.every(function (r) {
      var hay = ((r.t || "") + " " + (r.a || []).join(" ")).toLowerCase();
      return new RegExp("\\b" + common).test(hay); }),
    "a mid-word match leaked through (this is what made 'ear' match Research)");

  // Plurals and prefixes must still match: search the singular of a plural
  // that actually occurs in a title here.
  var pluralRec = null, singular = null;
  for (var pi = 0; pi < STATE.index.length && !pluralRec; pi++) {
    var m = ((STATE.index[pi].t || "").toLowerCase().match(/\b[a-z]{5,}s\b/) || [])[0];
    if (m) { pluralRec = STATE.index[pi]; singular = m.slice(0, -1); }
  }
  if (pluralRec) {
    check("word-start still matches plurals and prefixes",
      search("title:" + singular).some(function (r) { return r.id === pluralRec.id; }),
      "'" + singular + "' should still find '" + pluralRec.t.slice(0, 40) + "'");
  }

  // Quoted phrases must require adjacency. The phrase is chosen so the test can
  // actually fail: there must exist another journal containing the phrase's
  // FIRST word but not the phrase itself, otherwise dropping adjacency would
  // return an identical set and the check would pass vacuously.
  var phraseRec = null, phrase = null;
  for (var qi = 0; qi < STATE.index.length && !phraseRec; qi++) {
    var t = (STATE.index[qi].t || "").toLowerCase();
    var ws = t.match(/[a-z]{3,}/g) || [];
    if (ws.length < 2) continue;
    var cand = ws[0] + " " + ws[1];
    if (t.indexOf(cand) === -1) continue;
    var rx = new RegExp("\\b" + ws[0]);
    var decoy = STATE.index.some(function (r) {
      var h = ((r.t || "") + " " + (r.a || []).join(" ")).toLowerCase();
      return rx.test(h) && h.indexOf(cand) === -1; });
    if (decoy) { phraseRec = STATE.index[qi]; phrase = cand; }
  }
  if (phraseRec) {
    var phraseHits = search('title:"' + phrase + '"');
    var looseHits = search("title:" + phrase.split(" ")[0]);
    check("quoted phrase finds the title it was taken from",
      phraseHits.some(function (r) { return r.id === phraseRec.id; }), phrase);
    check("quoted phrase requires the words ADJACENT",
      phraseHits.every(function (r) {
        var hay = ((r.t || "") + " " + (r.a || []).join(" ")).toLowerCase();
        return hay.indexOf(phrase) !== -1; }),
      "a non-adjacent match leaked through for \"" + phrase + "\"");
    check("quoted phrase is stricter than its first word alone",
      phraseHits.length < looseHits.length,
      '"' + phrase + '" gave ' + phraseHits.length + " vs " + looseHits.length);
  }

  /* Name matches must all precede subject-only matches. */
  var res = search("science");
  var nm = STATE.nominalCount;
  check("name matches are reported separately", nm > 0 && nm < res.length);
  check("name matches sort before subject matches",
    res.slice(0, nm).every(function (r) {
      var hay = ((r.t || "") + " " + (r.a || []).join(" ") + " " + (r.p || "")).toLowerCase();
      return /\bscience/.test(hay); }),
    "a subject-only match appeared above the divider");

  /* The deal filter hiding a strong name match must be surfaced. */
  search("science", true);
  check("filter-hidden name matches are tracked",
    (STATE.hiddenByFilter || []).length > 0,
    "searching 'science' with the deal filter on hides journals incl. Science");

  /* ------------------------------------- acronyms and OA costs -------- */
  var pnas = STATE.index.filter(function (r) {
    return (r.y || "").split(" ").indexOf("pnas") !== -1
        && /proceedings of the national academy/i.test(r.t || ""); })[0];
  if (pnas) {
    check("an acronym finds the journal it denotes", search("pnas")[0].id === pnas.id,
      "top was " + (search("pnas")[0] || {}).t);
    check("an exact acronym outranks a title merely starting with it",
      search("pnas")[0].id === pnas.id,
      "PNAS Nexus must not outrank PNAS");
  }

  var withAcro = STATE.index.filter(function (r) { return (r.y || "").length > 3; })[0];
  if (withAcro) {
    var a = withAcro.y.split(" ")[0];
    check("acronym search returns the journal it was derived from",
      search(a).some(function (r) { return r.id === withAcro.id; }), a);
  }

  /* Every price the site holds is an OPEN ACCESS charge. In a hybrid journal
   * publishing is free behind the paywall, so the figure must not read as the
   * price of publishing there at all. */
  var hybridPayable = STATE.index.filter(function (r) {
    return r.o === "hybrid" && r.s === "none" && /\d/.test(r.c || ""); })[0];
  if (hybridPayable) {
    el("#show-hybrid").checked = false;
    check("hybrid OA charges are hidden by default",
      costFigure(hybridPayable).text.indexOf("free") !== -1,
      costFigure(hybridPayable).text);
    el("#show-hybrid").checked = true;
    check("the toggle reveals the hybrid OA charge",
      /\d/.test(costFigure(hybridPayable).text),
      costFigure(hybridPayable).text);
    el("#show-hybrid").checked = false;
  }

  var hybridCovered = STATE.index.filter(function (r) {
    return r.o === "hybrid" && r.s === "covered"; })[0];
  if (hybridCovered) {
    check("a covered hybrid still shows £0, which is the useful fact",
      costFigure(hybridCovered).text.indexOf("0") !== -1,
      costFigure(hybridCovered).text);
  }

  /* The publishing model is a fact about the journal, not about Oxford. */
  el("#free-only").checked = true; el("#deal-only").checked = false;
  var free = search("");
  el("#free-only").checked = false;
  var all = search("");
  check("the free-to-publish filter narrows the list",
    free.length > 0 && free.length < all.length,
    free.length + " of " + all.length);
  check("everything it returns really is free to the author",
    free.every(function (r) { return r.f === true; }));
  check("most free journals have no Oxford deal, and that is fine",
    free.filter(function (r) { return r.s === "none"; }).length > 0,
    "these are diamond journals: no deal exists because none is needed");
  check("every journal carries a publishing model",
    all.every(function (r) {
      return ["diamond", "gold", "hybrid", "subscription"].indexOf(r.o) !== -1; }));
  check("each model has an explanation",
    ["diamond", "gold", "hybrid", "subscription"].every(function (m) {
      return EXPLAIN["model_" + m] && EXPLAIN["model_" + m][1].length > 80; }));

  check("the cost column says what the figure is",
    el("#results").innerHTML.indexOf("Open access cost") !== -1);

  /* ----------------------------------------- opening a journal -------
   * Regression: build_site sharded details on 4 characters while loadShard
   * still asked for 2, so every click 404'd and did nothing at all. Search
   * tests passed throughout — nothing here ever opened a journal. */
  return openDetail(first.id).then(function () {
    var body = el("#detail-body").innerHTML;
    check("clicking a journal opens the detail modal",
      el("#detail-modal").hidden === false);
    // Compare against the escaped form: titles legitimately contain quotes
    // and ampersands, which the app escapes on the way into the DOM.
    check("detail modal shows the journal title",
      body.indexOf(esc(first.t)) !== -1, "title missing from rendered detail");
    check("detail modal is not an error placeholder",
      body.indexOf("Could not load this journal") === -1
      && body.indexOf("Journal not found") === -1,
      body.slice(0, 120));
    check("detail modal renders the cost section",
      body.indexOf("Cost for an Oxford author") !== -1);
    check("detail modal renders source links",
      body.indexOf("Sources for the information above") !== -1);
    check("detail modal offers the report box",
      body.indexOf("report-text") !== -1);
    check("detail modal offers a way to browse the journal's articles",
      body.indexOf("Browse recent articles") !== -1
      && body.indexOf("openalex.org/works") !== -1);

    /* A shard that does not exist must say so rather than dying silently. */
    return openDetail("9999-9999").then(function () {
      var err = el("#detail-body").innerHTML;
      check("a missing shard reports an error instead of doing nothing",
        err.indexOf("Could not load") !== -1 || err.indexOf("not found") !== -1,
        err.slice(0, 120));
    });
  }).then(function () {
    say(FAILURES ? "\n" + FAILURES + " check(s) FAILED" : "\nall frontend checks passed");
    if (!IS_JSC) { process.exit(FAILURES ? 1 : 0); }
    else if (FAILURES) { throw new Error(FAILURES + " frontend check(s) failed"); }
  });
}).catch(function (err) {
  say("HARNESS ERROR: " + err + "\n" + (err && err.stack));
  if (!IS_JSC) { process.exit(1); } else { throw err; }
});
