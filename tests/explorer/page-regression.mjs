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

import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { loadPage, strip } from '../../src/knowledgestore/assets/explorer_harness.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const pagePath = process.env.KSB_FIXTURE_PAGE
  || join(here, '..', '..', '.fixture-store', 'graphify-out', 'explorer.html');

// Page loading, block extraction and the DOM stub live in the shipped harness,
// which `check-answers` also uses. They were duplicated here; a harness holding
// its own private copy of the extraction rules is the same shape of mistake this
// codebase has made four times over (#115, #146, #134), so there is one copy.
//
// requireVerbatim, because THIS suite is testing the shipped code: the page must
// inline the current app.js byte for byte. A store checking its own published
// page must not demand that, and does not.
let api;
let jsonBlocks;
try {
  ({ api, blocks: jsonBlocks } = loadPage(pagePath, { requireVerbatim: true }));
} catch (e) {
  console.error(`FAIL  ${e.message}`);
  console.error('      run: python3 tests/explorer/fixture.py');
  process.exit(1);
}

// --- assertions -----------------------------------------------------------
// `strip` comes from the shared harness, for the same reason as the loader above.
let failures = 0;

// --- the data block's interning (#245) ------------------------------------
// The build replaces a column's values with indices into a per-column table
// wherever that costs fewer bytes than repeating them, and app.js decodes the
// rows before anything reads one. Rows are positional and read by index in
// fifty places below, so a decode that restored the wrong value would not fail
// loudly: it would answer every question in this file about the wrong entry,
// with the right shape and the right count.
//
// Two assertions, and the first is what stops the second being vacuous. A page
// with no interned column decodes trivially, and the decision is taken from the
// page's own data - so if the fixture ever stopped being interned, the
// round-trip below would pass over rows nothing had encoded, and this gate would
// report green over a decoder it no longer exercised.
//
// The expected rows are written out by hand from the fixture's own literals
// (tests/explorer/fixture.py: GRAPH for the labels, repositories and files,
// LABELS for the community names, INTENT for the tickets), never read back from
// the page they are checking. Sorted, because the row ORDER is the degree sort's
// business and is pinned elsewhere; what this asserts is the content.
const dicts = JSON.parse(jsonBlocks.dicts || '{}');
const internedColumns = Object.keys(dicts).map(Number).sort((a, b) => a - b);
if (internedColumns.length) {
  console.log(`ok    interning: the page interns column(s) ${internedColumns.join(', ')}`);
} else {
  failures++;
  console.error('FAIL  interning: no column of this page is interned, so the round-trip');
  console.error('      assertion below would pass over rows nothing encoded');
}

/** [label, repo, sourceFile, communityLabel, kind, tickets] per indexed entry. */
const EXPECTED_ROWS = [
  ['AddressEntryComponent', 'demo-app-b', 'src/address/address-entry.component.ts',
    'Address handling (app B)', 'code', ''],
  ['AddressFormComponent', 'demo-app-a', 'src/address/address-form.component.ts',
    'Address handling (app A)', 'code', ''],
  ['AddressPipe', 'demo-app-a', 'src/pipes/address.pipe.ts',
    'Address handling (app A)', 'code', 'DEMO-1'],
  ['AddressPipe', 'demo-app-b', 'src/pipes/address.pipe.ts',
    'Address handling (app B)', 'code', ''],
  ['Card payment succeeds', 'demo-e2e', 'features/payment.feature',
    'Business Features: Payments', 'scenario', ''],
  ['CheckoutContainer', 'demo-app-a', 'src/checkout/checkout.container.ts',
    'Payments', 'code', ''],
  ['DEMO-1', '', '', 'Business Features: Payments', 'ticket', 'DEMO-1'],
  ['Pay a fine online', 'demo-e2e', 'features/payment.feature',
    'Business Features: Payments', 'feature', ''],
  ['PayContainer', 'demo-app-b', 'src/pay/pay.container.ts', 'Payments', 'code', ''],
  ['PaymentService', 'demo-core', 'src/payment.service.ts', 'Payments', 'code', 'DEMO-2'],
  ['pay-service (prd)', 'demo-deploy', 'ansible/group_vars/prd/pay-service_values.yaml.j2',
    'Payments', 'concept', ''],
  ['prd', 'demo-deploy', '', 'Payments', 'concept', ''],
];
const flatten = (rows) => rows.map((r) => r.join('\u241f')).sort().join('\n');
const decodedRows = api.DATA.map((e) => [e[0], e[1], e[2], e[3], e[4], e[7].join(',')]);
if (flatten(decodedRows) === flatten(EXPECTED_ROWS)) {
  console.log(`ok    interning: all ${api.DATA.length} rows decode to the values they stood for`);
} else {
  failures++;
  console.error('FAIL  interning: the decoded rows are not the rows the build built');
  console.error(`      decoded:  ${flatten(decodedRows).slice(0, 400)}`);
  console.error(`      expected: ${flatten(EXPECTED_ROWS).slice(0, 400)}`);
}

/** @param {string} haystack @param {string[]} wanted @param {string} label */
const absentFrom = (haystack, wanted, label) =>
  wanted.filter((s) => !haystack.includes(s)).map((s) => `${label} missing: "${s}"`);

/** @param {string} haystack @param {string[]} unwanted @param {string} label */
const presentIn = (haystack, unwanted, label) =>
  unwanted.filter((s) => haystack.includes(s)).map((s) => `${label} must not contain: "${s}"`);

/** Drive one query through search mode and assert on the rendered cards.
 *
 * Search mode, not Ask: `check` below calls `runAsk`, which composes prose from
 * the graph and never renders a result card, so nothing it asserts can prove a
 * card field reaches the reader.
 * @param {string} query
 * @param {string[]} wantSubstrings substrings of the visible card text
 * @param {string[]} forbidden substrings that must NOT appear in it
 */
function checkSearch(query, wantSubstrings, forbidden = []) {
  api.q.value = query;
  api.runSearch();
  const text = strip(api.out.innerHTML);
  const problems = [
    ...absentFrom(text, wantSubstrings, 'results'),
    ...presentIn(text, forbidden, 'results'),
  ];
  if (problems.length) {
    failures++;
    console.error(`FAIL  search: ${query}`);
    for (const p of problems) console.error(`      ${p}`);
    console.error(`      results: ${text.slice(0, 200)}`);
  } else {
    console.log(`ok    search: ${query}  ->  ${api.meta.textContent}`);
  }
}

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
// DEMO-1 has both a tracker summary and a mined description. The tracker's
// words take the headline - that is the precedence rule - but the mined
// description is separate evidence and must still appear, under a label naming
// where it came from. Asserting only the headline let a build ship that dropped
// every mined description the moment a tracker title existed.
check('DEMO-1', 'ticket lookup',
  ['DEMO-1', 'graph entries',
   'Show a formatted address on the payment confirmation',   // tracker: headline
   'description, from commit messages',                      // provenance named
   'Add address formatting to the payment confirmation screen']);  // mined: kept
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

// Deployment evidence must be findable by its configuration, not only by its
// name: "which services set two CPUs in prd" is the question this layer exists
// for, and a key nobody can search for is a key the page does not carry.
checkSearch('replicas', ['pay-service (prd)', 'replicas=4'], ['AddressPipe']);
checkSearch('pay-service', ['pay-service (prd)', 'resources.limits.cpu=2']);
// The environment node is the other half of the layer, and environments are
// named "prd", "dev", "aat" - three letters, which the minified-symbol filter
// reads as junk. Assert on the connection line, which only its own card draws:
// the deployment card also contains "prd", so matching that would prove nothing.
checkSearch('prd', ['connects to: pay-service (prd)']);

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

// --- evidence conservation ------------------------------------------------
// The guarantee people actually rely on is that evidence the artefact carries
// REACHES THE READER. That cannot be tested a layer below where it breaks: a
// unit test asserted merge_ticket_evidence kept the mined descriptions, it did
// keep them, the assertion passed - and the page then declined to draw them,
// because the tracker title had taken the slot the description used to hold.
//
// So this asserts the property directly, over every ticket the page carries,
// rather than naming one string in one answer. A future field that arrives and
// displaces another is caught here without anyone remembering to add a case.
//
// Two limits are deliberate policy, encoded rather than assumed:
//   - a bare lookup shows the first two comments only; a 122-comment thread is
//     not an answer, and the full thread is in the committed artefact.
//   - a commit subject that merely repeats a shown description is suppressed
//     by extraSubjects, which a leading-words match satisfies anyway.
const TICKETS = JSON.parse(jsonBlocks.tickets);
const norm = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
// Build-time caps clip long text, so match a leading run of words: it survives
// every cap the build applies while still being specific to the one field.
const head = (s) => norm(s).split(' ').slice(0, 6).join(' ');

for (const [id, info] of Object.entries(TICKETS)) {
  api.q.value = id;
  api.runAsk();
  const shownText = norm(strip(api.out.innerHTML));
  const required = [
    ...(info.d || []).map((t) => ['mined description', t]),
    ...(info.s || []).map((t) => ['commit subject', t]),
    ...(info.b || []).map((t) => ['commit body', t]),
    ...(info.x ? [['tracker description', info.x]] : []),
    ...(info.c || []).slice(0, 2).map((t) => ['tracker comment', t]),
  ];
  const missing = required
    .filter(([, text]) => text && !shownText.includes(head(text)))
    .map(([field, text]) => `${field} never reaches the page: "${text.slice(0, 70)}"`);
  if (missing.length) {
    failures++;
    console.error(`FAIL  ${id}: evidence carried by the artefact but not shown`);
    for (const m of missing) console.error(`      ${m}`);
  } else {
    console.log(`ok    ${id}: every field of its evidence reaches the page`);
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
