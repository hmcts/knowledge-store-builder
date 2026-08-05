// End-to-end test for the explorer page.
//
// Builds a real page from a synthetic estate (tests/explorer/fixture.py writes
// a small graph plus intent, summary and topic layers, then runs the explorer
// stage), asserts the page inlines app.js byte-for-byte, and drives Ask mode
// through every composed answer shape against the data blocks the build
// produced. Together that proves the shipped page behaves as tested.
//
// No dynamic code execution: app.js is required as a plain module and exposes
// its API on a namespaced global.
//
// Run: python3 tests/explorer/fixture.py && node tests/explorer/page-regression.mjs

import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const appPath = join(here, '..', '..', 'src', 'knowledgestore', 'assets', 'app.js');
const pagePath = process.env.KSB_FIXTURE_PAGE
  || join(here, '..', '..', '.fixture-store', 'graphify-out', 'explorer.html');

let html;
try {
  html = readFileSync(pagePath, 'utf-8');
} catch {
  console.error(`FAIL  no built page at ${pagePath}`);
  console.error('      run: python3 tests/explorer/fixture.py');
  process.exit(1);
}
const appSource = readFileSync(appPath, 'utf-8');

// The page must carry app.js verbatim - the module we test IS the shipped code.
if (!html.includes(appSource)) {
  console.error('FAIL  built page does not inline the current app.js');
  process.exit(1);
}

// Linear-time extraction of the embedded JSON blocks (no backtracking-prone
// regex over a potentially very large page). The build escapes "</" inside the
// data, so splitting on the close tag is safe - but an opening "<script" can
// appear in the data itself (a commit body quoting markup), so take the FIRST
// one in each part, which is the block's own tag, never a later one from data.
const jsonBlocks = {};
for (const part of html.split('</script>')) {
  const open = part.indexOf('<script');
  if (open < 0) continue;
  const tagEnd = part.indexOf('>', open);
  const idMatch = /id="(\w+)"/.exec(part.slice(open, tagEnd));
  if (idMatch) jsonBlocks[idMatch[1]] = part.slice(tagEnd + 1);
}

// --- minimal DOM stub, installed before the module loads -----------------
const makeEl = () => ({
  textContent: '', innerHTML: '', value: '', placeholder: '', style: {},
  insertAdjacentHTML(position, markup) {
    this.innerHTML = position === 'afterbegin' ? markup + this.innerHTML : this.innerHTML + markup;
  },
  checked: true,
  classList: { toggle() {} },
  addEventListener() {}, add() {},
});
const elements = {};
for (const id of ['data', 'edges', 'titles', 'summaries', 'synonyms', 'tickets',
                  'config', 'topics', 'dives']) {
  if (!jsonBlocks[id]) throw new Error(`missing embedded JSON block: #${id}`);
  elements[id] = { textContent: jsonBlocks[id] };
}
globalThis.document = {
  getElementById: (id) => (elements[id] ??= makeEl()),
  querySelectorAll: () => [],
};
globalThis.Option = function Option() {};

const require = createRequire(import.meta.url);
require(appPath);
const api = globalThis.__explorerApi;

// --- assertions -----------------------------------------------------------
function strip(h) {
  let text = '';
  let inTag = false;
  for (const ch of h) {
    if (ch === '<') inTag = true;
    else if (ch === '>') { inTag = false; text += ' '; }
    else if (!inTag) text += ch;
  }
  return text.replace(/\s+/g, ' ').trim();
}
let failures = 0;

/** @param {string} haystack @param {string[]} wanted @param {string} label */
const absentFrom = (haystack, wanted, label) =>
  wanted.filter((s) => !haystack.includes(s)).map((s) => `${label} missing: "${s}"`);

/** @param {string} haystack @param {string[]} unwanted @param {string} label */
const presentIn = (haystack, unwanted, label) =>
  unwanted.filter((s) => haystack.includes(s)).map((s) => `${label} must not contain: "${s}"`);

/** Drive one question and assert on the answer.
 * @param {string} question
 * @param {string} wantMode substring of the meta line
 * @param {string[]} wantSubstrings substrings of the answer's visible text
 * @param {string[]} wantMetaSubstrings further substrings of the meta line
 * @param {{text?: string[], meta?: string[]}} forbidden substrings that must
 *   NOT appear, in the visible text or the meta line
 */
function check(question, wantMode, wantSubstrings, wantMetaSubstrings = [], forbidden = {}) {
  api.q.value = question;
  api.runAsk();
  const gotMode = api.meta.textContent;
  const text = strip(api.out.innerHTML);
  const problems = [
    ...absentFrom(gotMode, [wantMode], 'mode'),
    ...absentFrom(text, wantSubstrings, 'answer'),
    ...absentFrom(gotMode, wantMetaSubstrings, 'meta'),
    ...presentIn(text, forbidden.text || [], 'answer'),
    ...presentIn(gotMode, forbidden.meta || [], 'meta'),
  ];
  if (problems.length) {
    failures++;
    console.error(`FAIL  ${question}`);
    for (const p of problems) console.error(`      ${p}`);
    console.error(`      answer: ${text.slice(0, 200)}`);
  } else {
    console.log(`ok    ${question}  ->  ${gotMode.split(' - ')[0]}`);
  }
}

// The fixture estate: two apps each with their own AddressPipe (duplication),
// a shared PaymentService, one Gherkin feature, one ticket with a description,
// one community summary and one topic brief about addresses.
check('which repositories implement AddressPipe?', 'which repositories',
  ['AddressPipe appears in', 'independent implementations']);
check('why does AddressPipe exist?', 'business intent',
  ['AddressPipe', 'What changed here, per the commit history']);
check('what is impacted if PaymentService changes?', 'impact analysis',
  ['Changing', 'PaymentService', 'Affected areas', 'Technical evidence']);
check('where is PaymentService used?', 'where is it used',
  ['directly connected to', 'Used from']);
check('walk me through the payment journey', 'user journey',
  ['scripted business features']);
check('DEMO-1', 'ticket lookup',
  ['DEMO-1', 'graph entries']);
// business-first composition: prose leads, code sits behind the disclosure
check('how are addresses handled?', 'topic brief',
  ['Addresses in the demo estate', 'Pre-written topic brief']);
// deep dives: naming the repository serves the dossier, with its build stamp
check('what is going on with demo-core?', 'deep dive: demo-core',
  ['Deep dive', 'demo-core', 'evidence measured at build abcd1234']);
// no topic and no dive cover this question, so the page offers to have a brief written
check('how are payments taken?', 'open question',
  ['No pre-written brief covers this question']);
// nothing in the graph matches: say so plainly
// A question whose subject is absent but whose ordinary words are not: the
// answer still composes, and the meta line names the word with no evidence
// behind it. Disclosure rather than silence - an earlier abstain-on-rarest-term
// rule silenced four legitimate questions here, because ordinary question words
// ("used", "taken", "walk") are themselves absent from a small corpus.
check('how does quantum payment reconciliation work?', 'no evidence for: quantum', [], ['quantum']);

check('how is quantum scheduling implemented?', 'no evidence',
  ['No evidence in this estate', 'quantum'],
  // Absence is now reported as a finding that NAMES the missing terms,
  // rather than a generic "try different terms". The engine only takes
  // this path when every content term has zero matches across the whole
  // index - a score or coverage threshold could not tell an absent topic
  // from a poorly-ranked real one.
  ['quantum']);

// Ticket evidence: the commit subjects and bodies the ticket artefact carries
// are a retrieval surface of their own. In this estate "postalCode" appears
// only in DEMO-2's commit body and "settlement" only in one of its subjects,
// so nothing else can answer these two questions - and neither word may be
// reported as unevidenced while its evidence is on screen.
check('why does the payment flow use postalCode?', 'business intent',
  ['DEMO-2', 'commit body', 'postalCode'], [],
  { meta: ['no evidence for: postalcode'] });
check('which settlement reference is logged?', 'commit evidence',
  ['DEMO-2', 'commit subject', 'Log the settlement reference'], [],
  { meta: ['no evidence for: settlement'], text: ['No evidence in this estate'] });

// A commit body is user-authored text of up to four thousand characters: the
// page must escape it rather than interpret it, keep the line breaks a
// bulleted body depends on, and never let it read as a tracker title.
api.q.value = 'DEMO-2';
api.runAsk();
const ticketHtml = api.out.innerHTML;
const bodyAt = ticketHtml.indexOf('BREAKING CHANGE');
for (const [name, ok] of [
  ['a commit body is labelled as one', /commit body/i.test(ticketHtml)],
  ['markup in a body is escaped',
    ticketHtml.includes('&lt;script&gt;') && !ticketHtml.includes('<script>')],
  ['an ampersand in a body is escaped', ticketHtml.includes('&amp; the migration plan')],
  ["a body's line breaks survive rendering", ticketHtml.includes('payload.<br>')],
  ['the body sits inside a disclosure, not in the title',
    bodyAt > 0 && bodyAt > ticketHtml.indexOf('<details')],
]) {
  if (ok) {
    console.log(`ok    DEMO-2 lookup: ${name}`);
  } else {
    failures++;
    console.error(`FAIL  DEMO-2 lookup: ${name}`);
    console.error(`      html: ${ticketHtml.slice(0, 300)}`);
  }
}

// Escaped payload text reads alarmingly - "&lt;img src=x onerror=alert(1)&gt;"
// - and is inert, so the test asks two questions that are decidable by reading
// the markup. First, does an opening img, script or iframe tag appear? The page
// emits none of the three anywhere, so one could only have come from data that
// escaped. Second, does any hostile string this fixture carries appear in its
// raw form? That second question is the one that matters for an attribute:
// a quote breakout shifts the quoting, so a scan that blanks quoted spans
// re-absorbs the injected attribute and reports nothing.
const INJECTED_TAGS = ['<img', '<script', '<iframe'];
const RAW_PAYLOADS = [
  '"><img src=x onerror=alert(1)>',           // the estate's configured tracker URL
  'per "line item" onmouseover=alert(1)',     // a description, which lands in a title attribute
  '<script>alert(1)</script>',                // a commit body
];

/** Assert that a rendered answer contains no live markup, however it got there.
 * @param {string} label @param {string} markup */
function assertInert(label, markup) {
  const problems = presentIn(markup.toLowerCase(), INJECTED_TAGS, 'rendered HTML')
    .concat(presentIn(markup, RAW_PAYLOADS, 'rendered HTML, unescaped'));
  if (!problems.length) {
    console.log(`ok    inert: ${label}`);
    return;
  }
  failures++;
  console.error(`FAIL  inert: ${label}`);
  for (const p of problems) console.error(`      ${p}`);
  console.error(`      html: ${markup.slice(0, 400)}`);
}

// Everything the page embeds is untrusted text, wherever it came from, and an
// attribute is the easiest place to get this wrong. Three sources reach one:
// the estate's configured tracker URL lands in an href, the question as typed
// lands in a brief-request href, and a commit body lands in the answer body.
// This fixture's tracker URL closes the attribute and adds an event handler.
api.q.value = 'DEMO-2';
api.runAsk();
assertInert('a hostile tracker URL in a ticket link', api.out.innerHTML);
const linkHtml = api.out.innerHTML;
for (const [name, ok] of [
  ['the tracker URL is escaped where it is interpolated',
    linkHtml.includes('&quot;&gt;&lt;img') && linkHtml.includes('?a=1&amp;b=')],
  ['the ticket link still points at the tracker', linkHtml.includes('href="https://example.invalid/browse/')],
]) {
  if (ok) {
    console.log(`ok    tracker URL: ${name}`);
  } else {
    failures++;
    console.error(`FAIL  tracker URL: ${name}`);
    console.error(`      html: ${linkHtml.slice(0, 400)}`);
  }
}

// Evidence text lands in an attribute too: a ticket chip carries the
// description in its title, where an unescaped quote would close the attribute.
api.q.value = 'how are payments taken?';
api.runAsk();
const chipHtml = api.out.innerHTML;
assertInert('a description with a quote in a chip title', chipHtml);
if (chipHtml.includes('not per &quot;line item&quot; onmouseover=alert(1)')) {
  console.log('ok    chip title: the quote in a description is escaped');
} else {
  failures++;
  console.error('FAIL  chip title: the quote in a description is escaped');
  console.error(`      html: ${chipHtml.slice(chipHtml.indexOf('title='), chipHtml.indexOf('title=') + 200)}`);
}

// The question as typed: tokenising should mean markup never survives into the
// answer, and the brief-request link must escape what it encodes.
for (const hostile of ['<img src=x onerror=alert(1)>', '"><script>alert(1)</script>',
                       'why does <img src=x onerror=alert(1)> exist?']) {
  api.q.value = hostile;
  api.runAsk();
  assertInert(`a hostile question: ${hostile}`, api.out.innerHTML);
  assertInert(`the meta line for: ${hostile}`, api.meta.textContent);
}

if (failures) {
  console.error(`\n${failures} question shape(s) failed`);
  process.exit(1);
}
console.log('\nall question shapes pass');
