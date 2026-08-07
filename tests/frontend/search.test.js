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

var ELS = {};
function el(sel) { if (!ELS[sel]) { ELS[sel] = new El(sel); } return ELS[sel]; }

globalThis.document = {
  querySelector: el, addEventListener: function () {},
  body: { style: {} }, title: ""
};
globalThis.location = { href: "http://localhost/" };
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
