/* Headless test of site/app.js search behaviour against a real build.
 *
 * The search index, the lazily-loaded keyword file and the scoring rules are
 * the part of this project users touch directly, and the Python suite cannot
 * reach any of it. This runs app.js with a stub DOM and a stub fetch, so it
 * needs no browser.
 *
 * Runs against TWO datasets, which is why no check may assume a particular
 * count or a particular journal:
 *   * the Tests workflow builds ~14 journals from data/fixtures/;
 *   * the Deploy site workflow restores the cached real dataset (~49,000) and
 *     runs this same suite over it before publishing.
 * A literal like "3 unpriced journals" is right for one and wrong for the
 * other. Derive expectations from the data in hand.
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
var FAILED_LABELS = [];
function fail(msg) {
  say("FAIL  " + msg);
  FAILED_LABELS.push(msg);
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
El.prototype.setAttribute = function (k, v) { this[k] = v; };
El.prototype.matches = function () { return false; };

var ELS = {};
function el(sel) { if (!ELS[sel]) { ELS[sel] = new El(sel); } return ELS[sel]; }

/* A registry of stubbed elements that document.querySelectorAll can return, so
 * code walking the DOM (star buttons, popovers) behaves rather than throwing.
 * Same reason head and window are stubbed: a browser always has these, so the
 * omission shows up as an unrelated-looking failure three steps later. */
var QUERYABLE = [];
globalThis.document = {
  querySelector: el,
  querySelectorAll: function (sel) {
    var hits = QUERYABLE.filter(function (e) { return e.matches(sel); });
    hits.forEach = Array.prototype.forEach;
    return hits;
  },
  addEventListener: function () {},
  // head is stubbed for the same reason window is: a browser always has it, so
  // code that touches it looks fine in review and then silently rejects the
  // whole boot promise here, surfacing as unrelated-looking failures.
  body: { style: {} }, head: { appendChild: function (c) { return c; } }, title: ""
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
var STORE = {};
globalThis.localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(STORE, k) ? STORE[k] : null; },
  setItem: function (k, v) { STORE[k] = String(v); },
  removeItem: function (k) { delete STORE[k]; },
};
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
  /* "46,341 journals" invites "out of what?", so the inclusion rules hang off
   * the count itself rather than being buried in the About page. */
  search("");
  check("the journal count carries a ? explainer",
    el("#result-count").innerHTML.indexOf('data-explain="inclusion"') !== -1,
    el("#result-count").innerHTML);
  check("the count itself still renders",
    /\d/.test(el("#result-count").innerHTML));
  check("the inclusion explainer lists the routes in, not just the total",
    ["DOAJ", "transformative agreement", "most-cited", "publisher"]
      .every(function (s) { return EXPLAIN.inclusion[1].indexOf(s) !== -1; }));
  check("the inclusion explainer admits what it misses",
    /law|humanities/i.test(EXPLAIN.inclusion[1])
    && EXPLAIN.inclusion[1].indexOf("Tell us") !== -1);

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

  /* Every figure is an OPEN ACCESS charge and the column heading says so, so
   * all four publishing models show their number the same way — no filter, no
   * hidden state. */
  var hybridPayable = STATE.index.filter(function (r) {
    return r.o === "hybrid" && r.s === "none" && /\d/.test(r.c || ""); })[0];
  if (hybridPayable) {
    check("a hybrid journal shows its OA charge like any other",
      /\d/.test(costFigure(hybridPayable).text),
      costFigure(hybridPayable).text);
    /* The site has no source for page charges, colour charges or submission
     * fees — near-universal in economics — so it must never tell a reader the
     * subscription route is free. */
    check("the cost cell makes no claim about publishing non-open-access",
      costFigure(hybridPayable).text.toLowerCase().indexOf("free") === -1,
      costFigure(hybridPayable).text);
  }

  /* The same claim must not survive in the explainer either. Removing it from
   * the cost cell and leaving it in bold behind the "?" would just move the
   * over-claim somewhere harder to notice. */
  ["model_hybrid", "model_subscription"].forEach(function (k) {
    var text = EXPLAIN[k][1];
    check(k + " does not assert that publishing non-open-access is free",
      !/is free to you|is free to the author|normally free/.test(text));
    check(k + " names the charges the site cannot see",
      /submission|page or colour|colour charges/.test(text));
  });

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

    /* ---- "Submitting here" ------------------------------------------
     * Present only for DOAJ journals (about half the site), so both the
     * rendered and the omitted case are checked. */
    var withSub = null, withoutSub = null;
    for (var n = 0; n < STATE.index.length && (!withSub || !withoutSub); n++) {
      var rec = STATE.index[n];
      if (rec.d && !withSub) { withSub = rec; }
      if (!rec.d && !withoutSub) { withoutSub = rec; }
    }

    return openDetail(withSub.id).then(function () {
      var b = el("#detail-body").innerHTML;
      /* Assert the invariant rather than assuming this particular journal has
       * the data: the panel must appear exactly when submission data exists,
       * which is what would break if merge stopped emitting the field. */
      var shard = STATE.shards[withSub.id.slice(0, STATE.config.shard_key_length || 4)];
      var hasData = !!(shard && shard[withSub.id] && shard[withSub.id].submission);
      check("the Submitting here panel appears exactly when there is data for it",
        (b.indexOf("Submitting here") !== -1) === hasData,
        withSub.t + " (submission data: " + hasData + ")");
      check("some DOAJ journal in the index actually carries submission data",
        hasData, "merge may have stopped emitting the field");
      check("the panel links the journal's author guidelines",
        b.indexOf("author guidelines") !== -1);
      check("the panel says who recorded it, not just what it says",
        b.indexOf("Recorded by the journal") !== -1 && b.indexOf("doaj.org") !== -1);
      /* The one question users actually asked for, and the one thing no
       * structured source publishes. Saying so beats a silent omission. */
      check("the panel explains why word limits are absent",
        b.indexOf("Word limits") !== -1 && b.indexOf("LaTeX") !== -1);
      return openDetail(withoutSub.id);
    }).then(function () {
      check("a non-DOAJ journal gets no empty Submitting here panel",
        el("#detail-body").innerHTML.indexOf("Submitting here") === -1,
        withoutSub.t);

      /* `false` is a real answer here — DOAJ makes both fields mandatory — so
       * it must render as "No", not vanish the way a null would. */
      var noScreen = STATE.index.filter(function (r) { return r.d; })[0];
      var fake = { submission: { plagiarism_screening: false,
                                 author_retains_copyright: false,
                                 review_process: [], persistent_ids: [] } };
      var html = submissionBlock(fake);
      check("a mandatory boolean that is false renders as No, not as absent",
        (html.match(/<dd>No<\/dd>/g) || []).length === 2, html);

      var missing = submissionBlock({ submission: null });
      check("no submission data renders nothing at all", missing === "");

      /* A few DOAJ records carry the archiving URL with no label. Gating the
       * row on the label alone silently threw the link away. */
      var urlOnly = submissionBlock({ submission: {
        deposit_policy: null,
        deposit_policy_url: "https://example.org/archiving" } });
      check("an archiving URL with no label still renders as a link",
        urlOnly.indexOf("https://example.org/archiving") !== -1, urlOnly);
    }).then(function () {

    /* A shard that does not exist must say so rather than dying silently. */
    return openDetail("9999-9999").then(function () {
      var err = el("#detail-body").innerHTML;
      check("a missing shard reports an error instead of doing nothing",
        err.indexOf("Could not load") !== -1 || err.indexOf("not found") !== -1,
        err.slice(0, 120));
    });
    });

  }).then(function () {
  /* ------------------------------------- the £0 invariant, as rendered
     * The pipeline tests prove journals.json and the index are safe. This is the
     * last link: a change to costFigure() could turn a conditional state back
     * into a green unconditional £0 while every backend test still passed. */
    var riskyRecs = STATE.index.filter(function (r) {
      return /but confirm|^Not confirmed/.test(r.c || "");
    });
    var safeZero = STATE.index.filter(function (r) {
      return /^£0 — diamond|^No APC/.test(r.c || "");
    });
    check("the fixture build contains journals in a weakened state",
      riskyRecs.length > 0, "otherwise the checks below pass vacuously");
    check("and journals whose £0 depends on nothing", safeZero.length > 0);

    check("a weakened claim never renders as a bare £0",
      riskyRecs.every(function (r) { return costFigure(r).text !== "£0"; }),
      JSON.stringify(riskyRecs.map(function (r) { return costFigure(r).text; })));
    check("a weakened claim never renders in the free colour",
      riskyRecs.every(function (r) { return costFigure(r).cls !== "free"; }),
      JSON.stringify(riskyRecs.map(function (r) { return costFigure(r).cls; })));
    check("a weakened claim is marked for caution",
      riskyRecs.every(function (r) { return costFigure(r).cls === "caution"; }));
    check("an unconditional £0 still reads as free",
      safeZero.every(function (r) { return costFigure(r).cls === "free"; }));
    /* The whole point of the tiering: these two groups must not be typographically
     * indistinguishable, or the wording change bought nothing. */
    check("the two groups cannot be confused with each other",
      riskyRecs.every(function (a) {
        return safeZero.every(function (b) {
          return costFigure(a).text !== costFigure(b).text;
        });
      }));

  /* -------------------------------------------------- ordering by cost
   * Only meaningful because the pipeline reconciles 46 currencies first:
   * sorted on raw numbers, a journal charging 150,000,000 IRR (~£2,400) would
   * outrank Nature at $12,290. */
  }).then(function () {
    function sorted(mode) {
      el("#sort").value = mode;
      search("");
      el("#sort").value = "rel";
      return STATE.results;
    }
    var byRel = (el("#sort").value = "rel", search(""));
    var relOrder = byRel.map(function (r) { return r.id; }).join(",");

    var asc = sorted("cost-asc"), desc = sorted("cost-desc");
    var pricedAsc = asc.filter(function (r) { return typeof r.g === "number"; });
    var pricedDesc = desc.filter(function (r) { return typeof r.g === "number"; });

    check("some journals carry a comparable GBP figure", pricedAsc.length > 0,
      pricedAsc.length + " of " + asc.length);
    check("low-to-high really is non-decreasing",
      pricedAsc.every(function (r, i) { return !i || pricedAsc[i-1].g <= r.g; }),
      JSON.stringify(pricedAsc.map(function (r) { return r.g; })));
    check("high-to-low really is non-increasing",
      pricedDesc.every(function (r, i) { return !i || pricedDesc[i-1].g >= r.g; }),
      JSON.stringify(pricedDesc.map(function (r) { return r.g; })));
    check("the two directions are actual reverses of each other",
      pricedAsc[0].g === pricedDesc[pricedDesc.length - 1].g);

    /* A journal with no comparable figure must never be ordered among the
     * priced ones, in either direction — at one end it reads as free, at the
     * other as the most expensive thing on the site. */
    function tailIsUnpriced(list) {
      var seenUnpriced = false;
      return list.every(function (r) {
        if (typeof r.g !== "number") { seenUnpriced = true; return true; }
        return !seenUnpriced;   // no priced row may follow an unpriced one
      });
    }
    check("unpriced journals are held back, ascending", tailIsUnpriced(asc));
    check("unpriced journals are held back, descending", tailIsUnpriced(desc));

    /* Twelve thousand journals share the value 0. If amount were the only key,
     * a capped agreement whose allowance may already be spent would sit
     * indistinguishably among diamond titles that charge nothing — position
     * saying what the wording was changed to stop saying. */
    var zeros = asc.filter(function (r) { return r.g === 0; });
    check("within £0, certainty orders the rows",
      zeros.every(function (r, i) { return !i || (zeros[i-1].v || 0) <= (r.v || 0); }),
      JSON.stringify(zeros.map(function (r) { return r.v; })));
    check("a conditional £0 never precedes an unconditional one",
      zeros.filter(function (r) { return r.v === 2; }).every(function (r) {
        return zeros.indexOf(r) > zeros.map(function (x) { return x.v; }).lastIndexOf(0);
      }));
    check("the same holds sorting downwards",
      (function () {
        var z = desc.filter(function (r) { return r.g === 0; });
        return z.every(function (r, i) { return !i || (z[i-1].v || 0) <= (r.v || 0); });
      })());

    check("a settled £0 sorts as zero, not as missing",
      asc.filter(function (r) { return /^£0/.test(r.c); })
         .every(function (r) { return r.g === 0; }));

    el("#sort").value = "cost-asc"; search("");
    /* Whitespace-tolerant: the phrase straddles a line break in the template
     * literal, so a plain indexOf misses it. */
    check("the reader is told how many journals could not be ordered",
      /no\s+comparable figure/.test(el("#results").innerHTML));
    /* Derived, not hard-coded: this suite runs against the twelve-journal
     * fixture build AND against the real 49,000-journal dataset in the deploy
     * workflow, and a literal count is only ever right for one of them. It is
     * also the stronger assertion — that the number shown IS the number
     * withheld, rather than merely being some number. */
    var noteNum = el("#results").innerHTML
      .match(/<strong>([\d,]+)<\/strong>\s+of these journals/);
    check("and how many, not just that some exist",
      noteNum !== null
      && Number(noteNum[1].replace(/,/g, "")) === STATE.unpricedCount,
      noteNum ? noteNum[1] + " shown vs " + STATE.unpricedCount + " actual"
              : "no count rendered");
    check("and on what date the conversion was made",
      /European Central Bank rates for \d{4}-\d{2}-\d{2}/
        .test(el("#results").innerHTML), (STATE.config.fx || {}).date);
    el("#sort").value = "rel";

    check("switching back to best match restores the original order",
      search("").map(function (r) { return r.id; }).join(",") === relOrder);

  /* The usage-link branch only runs when a build published figures, so it is
   * invisible to a fixtures build — which is how a crash in it reached CI. Run
   * wireUI again with the flag on and make sure it survives. */
  }).then(function () {
    var saved = STATE.config.usage_available;
    STATE.config.usage_available = true;
    var threw = null;
    try { wireUI(); } catch (e) { threw = e; }
    check("wiring the usage link does not throw when a build published figures",
      threw === null, threw && String(threw));
    check("the usage link is revealed", el("#foot-usage-wrap").hidden === false);
    STATE.config.usage_available = saved;

  /* --------------------------------------- starring, compare, export */
  }).then(function () {
    var a = STATE.index[0], b = STATE.index[1], c = STATE.index[2];

    setStarred([]);
    check("nothing is starred to begin with", starredIds().length === 0);

    toggleStar(a.id);
    toggleStar(b.id);
    check("starring persists", starredIds().length === 2);
    /* GitHub Pages user sites all share one origin, so ben18785.github.io/
     * oxford-apc-finder and ben18785.github.io/ai-sci-resources read the same
     * localStorage — an unnamespaced key would collide across the two. */
    check("the storage key is namespaced to this site",
      STAR_KEY.indexOf("oxford-apc-finder") === 0, STAR_KEY);
    check("it is stored as plain ISSNs and nothing else",
      JSON.parse(localStorage.getItem(STAR_KEY)).every(function (x) {
        return /^\d{4}-\d{3}[\dX]$/i.test(x);
      }), localStorage.getItem(STAR_KEY));

    toggleStar(a.id);
    check("starring toggles off again",
      starredIds().length === 1 && starredIds()[0] === b.id);

    setStarred([a.id, a.id, b.id]);
    check("duplicates cannot accumulate", starredIds().length === 2);

    var many = STATE.index.slice(0, 40).map(function (r) { return r.id; });
    setStarred(many.concat(many));
    check("the list is capped so the share link cannot grow unbounded",
      starredIds().length <= 40);

    /* Ordering: the question a shortlist answers is "which is cheapest", so
     * that is what the comparison leads with. */
    setStarred([a.id, b.id, c.id]);
    var rows = compareRows([a.id, b.id, c.id]);
    var costs = rows.map(function (r) {
      return typeof r.g === "number" ? r.g : Infinity; });
    check("the comparison is ordered by cost",
      costs.every(function (g, i) { return !i || costs[i-1] <= g; }),
      JSON.stringify(costs));
    check("an unknown id is dropped rather than rendering an empty row",
      compareRows([a.id, "0000-0000"]).length === 1);

    /* The share link is the answer to "localStorage is per-device": it carries
     * the list, so moving it to a phone and sending it to a colleague are the
     * same action. It must carry the list and nothing else. */
    var link = shareLink([a.id, b.id]);
    check("the share link round-trips the list",
      link.indexOf("#compare=" + a.id + "," + b.id) !== -1, link);
    check("the share link carries no identity",
      !/starred|user|session|token|email/i.test(link), link);

    var esacs = new Map([[a.id, "els2026jisc"]]);
    var text = compareText(rows, esacs);
    ["ISSN", "Oxford deal", "Open access cost", "Unofficial"].forEach(function (s) {
      check("the text export states " + s.toLowerCase(), text.indexOf(s) !== -1);
    });
    check("the text export quotes the agreement identifier",
      text.indexOf("els2026jisc") !== -1);

    var mail = bodleianMail(rows, esacs);
    check("the email goes to the open access team",
      mail.indexOf("mailto:" + STATE.config.contact) === 0, mail.slice(0, 60));
    var decoded = decodeURIComponent(mail);
    check("the email quotes the agreement identifier, so it can be answered",
      decoded.indexOf("els2026jisc") !== -1);
    check("the email asks a specific question rather than being blank",
      decoded.indexOf("corresponding author") !== -1
      && decoded.indexOf("confirm") !== -1);
    check("the email says the figures are unofficial and dated",
      decoded.indexOf("unofficial") !== -1 && /generated \d{4}-\d{2}/.test(decoded));

    setStarred([]);
    return showCompare([]).then(function () {
      check("an empty list explains how to build one",
        el("#detail-body").innerHTML.indexOf("Nothing starred") !== -1);
      return showCompare([a.id, b.id]);
    }).then(function () {
      var html = el("#detail-body").innerHTML;
      // One star button per journal row; counting <tr> would include the header.
      check("the comparison renders a row per journal",
        (html.match(/data-star=/g) || []).length === 2,
        (html.match(/data-star=/g) || []).length + " rows");
      check("it offers the email, a copy, and a share link",
        html.indexOf("compare-mail") !== -1 && html.indexOf("compare-copy") !== -1
        && html.indexOf("share-url") !== -1);
      check("it says plainly that nothing leaves the browser",
        /nothing is sent anywhere/i.test(html));
    });

  /* ------------------------------------------------- usage monitoring */
  }).then(function () {
    /* The beacon must be inert unless a site code is configured. A build with
     * analytics off must load no third-party script and post nothing. */
    var created = 0, realCreate = globalThis.document.createElement;
    globalThis.document.createElement = function () { created++; return new El("created"); };
    var savedCode = STATE.config.goatcounter_code;
    STATE.config.goatcounter_code = null;
    initAnalytics();
    check("no analytics script is loaded when no site code is configured",
      created === 0, created + " element(s) created");

    STATE.config.goatcounter_code = "example";
    initAnalytics();
    check("analytics script is loaded when a site code is configured", created === 1);
    STATE.config.goatcounter_code = savedCode;
    globalThis.document.createElement = realCreate;

    /* Counting must never be able to break the page. With GoatCounter absent
     * (blocked, or still loading) every call is a silent no-op. */
    var threw = false;
    globalThis.window.goatcounter = undefined;
    try {
      track("j/covered/1234-5678", "Nature");
      trackJournalView({ id: "1234-5678", s: "covered", t: "Nature" });
      trackMissedSearch("some missing journal");
    } catch (e) { threw = true; }
    check("tracking is a no-op when the counter is unavailable", !threw);

    /* The deal status travels in the path, so one call yields both the
     * per-journal count and the coverage split. */
    var posted = [];
    globalThis.window.goatcounter = { count: function (o) { posted.push(o); } };
    el("#deal-only").checked = false;
    trackJournalView({ id: "1234-5678", s: "covered", t: "Nature" });
    check("a journal view records status, filter scope and ISSN in the path",
      posted.length === 1 && posted[0].path === "j/covered/all/1234-5678",
      JSON.stringify(posted[0]));
    trackJournalView({ id: "3333-4444", s: undefined, t: "No deal" });
    check("a journal with no deal status still records a usable path",
      posted[1] && posted[1].path === "j/none/all/3333-4444",
      JSON.stringify(posted[1]));
    /* The filter state has to travel with the view, or the coverage share
     * measures the default setting rather than what the reader chose. */
    el("#deal-only").checked = true;
    trackJournalView({ id: "1234-5678", s: "covered", t: "Nature" });
    check("a view made under the deal filter is marked as such",
      posted[2] && posted[2].path === "j/covered/deals/1234-5678",
      JSON.stringify(posted[2]));
    el("#deal-only").checked = false;

    /* Search text is only ever recorded for a search that found nothing, and
     * only after normalising: no punctuation, no case, length-capped. */
    posted.length = 0;
    trackMissedSearch("  Law Quarterly REVIEW!! ");
    check("a missed search is normalised before being recorded",
      posted.length === 1 && posted[0].path === "missing/law quarterly review",
      JSON.stringify(posted[0]));
    posted.length = 0;
    trackMissedSearch("ab");
    check("a too-short query is not recorded", posted.length === 0);
    globalThis.window.goatcounter = undefined;

    /* A real search that finds something must record nothing at all. */
    posted.length = 0;
    globalThis.window.goatcounter = { count: function (o) { posted.push(o); } };
    search(first.t);
    check("a search that finds results records nothing", posted.length === 0,
      JSON.stringify(posted));

    /* Typing a missing title one letter at a time must post the finished
     * query, not every prefix that happened to match nothing. The harness
     * fires setTimeout synchronously, which would hide exactly this, so hold
     * the callbacks in a queue for the duration of the check. */
    var pending = [], realSet = globalThis.setTimeout, realClear = globalThis.clearTimeout;
    globalThis.setTimeout = function (fn) { pending.push(fn); return pending.length; };
    globalThis.clearTimeout = function (h) { if (h) { pending[h - 1] = null; } };
    posted.length = 0;
    trackMissedSearch("zzzq");
    trackMissedSearch("zzzqq");
    trackMissedSearch("zzzqqxwv");
    pending.forEach(function (fn) { if (fn) { fn(); } });
    check("only the finished query is recorded, not each prefix",
      posted.length === 1 && posted[0].path === "missing/zzzqqxwv",
      JSON.stringify(posted));
    globalThis.setTimeout = realSet; globalThis.clearTimeout = realClear;
    globalThis.window.goatcounter = undefined;

    /* The usage view, against a synthetic payload — the real one only exists
     * once a build has pulled stats back. */
    var realFetch = globalThis.fetch;
    globalThis.fetch = function (path) {
      if (path.indexOf("usage.json") === -1) { return realFetch(path); }
      return Promise.resolve({ ok: true, status: 200, json: function () {
        return Promise.resolve({
          generated: "2026-08-08T00:00:00Z", window_days: 90,
          totals: { pageviews: 1200, visitors: 310, journal_views: 77,
                    distinct_journals_viewed: 5, countries: 2 },
          coverage: { covered_journal_share: 0.4, corpus_share: 0.27,
                      sample_journals: 5, sample_views: 20 },
          top_journals: [
            { issn_l: "1234-5678", title: "Nature", status: "covered", views: 40 },
            { issn_l: "3333-4444", title: "Obscure Review", status: "none", views: 7 }],
          withheld: { journals: 3, views: 6, min_views_to_publish: 5 },
          most_wanted: [{ query: "law quarterly review", searches: 5 }],
          top_countries: [{ code: "GB", name: "United Kingdom", count: 800 }],
        });
      } });
    };
    return showUsage().then(function () {
      var u = el("#detail-body").innerHTML;
      check("usage view renders the headline counts",
        u.indexOf("310") !== -1 && u.indexOf("visitors") !== -1);
      check("usage view draws a bar per journal",
        (u.match(/bar-fill/g) || []).length === 2,
        (u.match(/bar-fill/g) || []).length + " bars");
      check("usage view marks deal-covered bars",
        u.indexOf("bar-fill covered") !== -1);
      check("usage view compares coverage against the whole corpus",
        u.indexOf("40%") !== -1 && u.indexOf("27%") !== -1);
      check("usage view says what the coverage share was measured over",
        u.indexOf("switched off") !== -1 && u.indexOf("5 journals") !== -1);
      check("usage view discloses what it withheld",
        u.indexOf("3 journals were looked up") !== -1
        && u.indexOf("below 5 views") !== -1);
      check("usage view lists searches that found nothing",
        u.indexOf("law quarterly review") !== -1);
      check("usage view states the privacy position",
        u.indexOf("no cookies") !== -1 && u.indexOf("no IP addresses") !== -1);
      /* The counts are browser-side estimates. Presenting them as a headcount
       * would be the one genuinely misleading thing this view could do. */
      check("usage view does not present visitors as a count of people",
        /not\s+counts of people/.test(u));
      globalThis.fetch = realFetch;
    });

    /* A build with no usage data must degrade to a message, not an error. */
  }).then(function () {
    var realFetch = globalThis.fetch;
    globalThis.fetch = function (path) {
      if (path.indexOf("usage.json") !== -1) {
        return Promise.resolve({ ok: false, status: 404,
          json: function () { return Promise.reject(new Error("404")); } });
      }
      return realFetch(path);
    };
    return showUsage().then(function () {
      check("usage view degrades gracefully when no data is published",
        el("#detail-body").innerHTML.indexOf("No usage data") !== -1);
      globalThis.fetch = realFetch;
    });
  }).then(function () {
    if (FAILURES) {
      // Repeated at the end because one FAIL among 140 ok lines is invisible in
      // a CI log, and the tail is the part anyone pastes when asking for help.
      say("\n" + FAILURES + " check(s) FAILED:");
      FAILED_LABELS.forEach(function (m) { say("  - " + m); });
    } else {
      say("\nall frontend checks passed");
    }
    if (!IS_JSC) { process.exit(FAILURES ? 1 : 0); }
    else if (FAILURES) { throw new Error(FAILURES + " frontend check(s) failed"); }
  });
}).catch(function (err) {
  say("HARNESS ERROR: " + err + "\n" + (err && err.stack));
  if (!IS_JSC) { process.exit(1); } else { throw err; }
});
