/* Tests for alerts/digest.js — what lands in somebody's inbox.
 *
 *   node tests/frontend/digest.test.js
 *   /System/.../jsc tests/frontend/digest.test.js
 *
 * digest.js is an ES module because Cloudflare Workers require one. Both
 * engines here run scripts, not modules, so the `export ` keywords are stripped
 * before evaluating. That is a shim for the harness, not a transform anyone
 * ships: worker.js imports the file unmodified.
 */
"use strict";

var IS_JSC = (typeof print === "function" && typeof readFile === "function");
function say(m) { if (IS_JSC) { print(m); } else { console.log(m); } }
function read(p) {
  return IS_JSC ? readFile(p) : require("fs").readFileSync(p, "utf8");
}

var FAILURES = 0, FAILED = [];
function check(label, ok, detail) {
  if (ok) { say("  ok  " + label); }
  else { say("FAIL  " + label + (detail ? "  (" + detail + ")" : ""));
         FAILED.push(label); FAILURES++; }
}

var src = read("alerts/digest.js").replace(/^export /gm, "");
if (IS_JSC) { globalThis.eval(src); }
else { require("vm").runInThisContext(src, { filename: "alerts/digest.js" }); }

/* ------------------------------------------------------------ matching */
var changes = {
  changed: [
    { issn_l: "1111-1111", title: "Watched And Changed",
      changes: { status: ["none", "covered"], price: ["3000", ""] } },
    { issn_l: "2222-2222", title: "Not Watched",
      changes: { status: ["covered", "none"] } },
    { issn_l: "3333-3333", title: "Watched, Nothing Real Changed",
      changes: { status: ["covered", "covered"] } },
    { issn_l: "4444-4444", title: "Watched, Only Noise",
      changes: { scope: ["a", "b"] } },
  ],
  removed: [{ issn_l: "5555-5555", title: "Watched And Removed" }],
  added: [{ issn_l: "6666-6666", title: "Watched And Added" }],
};
var watched = ["1111-1111", "3333-3333", "4444-4444", "5555-5555"];
var m = matchChanges(changes, watched);
var titles = m.map(function (x) { return x.title; });

check("a watched journal that changed is reported",
  titles.indexOf("Watched And Changed") !== -1, titles.join(", "));
/* The whole point of a watch list: somebody else's journal is not your news. */
check("a journal nobody is watching is not reported",
  titles.indexOf("Not Watched") === -1, titles.join(", "));
/* A field recorded as changed but with identical values is a diff artefact,
 * not something to email about. */
check("an unchanged value is not reported as a change",
  titles.indexOf("Watched, Nothing Real Changed") === -1, titles.join(", "));
/* Scope text and topics churn every week as OpenAlex reclassifies. An alert on
 * those gets the whole thing unsubscribed from, and then the one that mattered
 * never arrives. */
check("churn in fields nobody acts on is ignored",
  titles.indexOf("Watched, Only Noise") === -1, titles.join(", "));
/* "No longer listed" is the difference between "no deal" and "we can no longer
 * tell you anything", which is worth knowing. */
check("a watched journal leaving the site is reported",
  titles.indexOf("Watched And Removed") !== -1, titles.join(", "));

check("watching nothing produces nothing", matchChanges(changes, []).length === 0);
check("an empty build produces nothing",
  matchChanges({ changed: [], removed: [], added: [] }, watched).length === 0);
check("a missing payload does not throw",
  matchChanges(null, watched).length === 0 && matchChanges({}, watched).length === 0);

/* --------------------------------------------------------- the wording */
var good = describeChange("status", "none", "covered");
var bad = describeChange("status", "covered", "none");
check("a deal appearing is reported", /none → covered/.test(good), good);
/* The ask was alerts "for good or for bad" — a deal disappearing is the one
 * you most need to hear about, so it must not be quietly dropped. */
check("a deal disappearing is reported too", /covered → none/.test(bad), bad);
check("an empty price reads as words, not a blank",
  /no published price/.test(describeChange("price", "3000", "")),
  describeChange("price", "3000", ""));
check("a new source conflict is explained, not just flagged",
  /disagree/.test(describeChange("disputed", "", "1")));

/* ----------------------------------------------------------- the email */
var mail = buildEmail(m, { unsubscribe: "https://example.org/u?t=abc" });
check("the subject counts what changed", /2 of your journals changed/.test(mail.subject),
  mail.subject);
check("one change gets a singular subject",
  /Watched And Changed changed$/.test(buildEmail([m[0]], {}).subject),
  buildEmail([m[0]], {}).subject);
check("the body names each journal and its ISSN",
  mail.body.indexOf("Watched And Changed (1111-1111)") !== -1);
check("and links straight to it", mail.body.indexOf("#compare=1111-1111") !== -1);
/* Same posture as every other surface: this is an unofficial tool, and an
 * unprompted email is the last place to let someone forget that. */
check("the email says it is unofficial and names the Bodleian",
  /unofficial/.test(mail.body) && /oapayments@bodleian/.test(mail.body));
/* Required by PECR for unsolicited marketing and simply decent for anything
 * else — every message carries its own way out. */
check("every email carries an unsubscribe link",
  mail.body.indexOf("https://example.org/u?t=abc") !== -1);

if (FAILURES) {
  say("\n" + FAILURES + " check(s) FAILED:");
  FAILED.forEach(function (f) { say("  - " + f); });
} else { say("\nall digest checks passed"); }
if (!IS_JSC) { process.exit(FAILURES ? 1 : 0); }
else if (FAILURES) { throw new Error(FAILURES + " digest check(s) failed"); }
