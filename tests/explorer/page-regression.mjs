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

if (failures) {
  console.error(`\n${failures} question shape(s) failed`);
  process.exit(1);
}
console.log('\nall question shapes pass');
