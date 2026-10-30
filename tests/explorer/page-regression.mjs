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

import { copyFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  APP_PATH, READS_PAGE_FORMATS, extractJsonBlocks, loadPage, strip,
} from '../../src/knowledgestore/assets/explorer_harness.mjs';

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

/** [label, repo, sourceFile, communityLabel, kind, connections, tickets] per entry.
 *
 * `connections` is here because it is the only interned LIST column on this
 * page, and the projection once skipped it: the list decode was then covered
 * only by a synthetic row in engine-unit, and nothing checked that a list
 * column of a real page came back as the labels it stood for. `tickets` is a
 * list column the costing DECLINES, so the pair also pins that a declined
 * column reaches the reader untouched.
 *
 * Hand-derived from the fixture's own literals: GRAPH for the labels,
 * repositories and files, LABELS for the community names, INTENT for the
 * tickets, and `links` for the connections - ordered by degree descending then
 * by node id, which is what `entry_connections` documents.
 *
 * AddressEntryComponent's tickets are empty on purpose, and that is a ruling
 * (#425): its file's only ticket, DEMO-4, sits under `proposed` in the intent
 * index, and a node's list carries landed work only. A branch never changed the
 * code this graph was extracted from.
 */
const EXPECTED_ROWS = [
  ['AddressEntryComponent', 'demo-app-b', 'src/address/address-entry.component.ts',
    'Address handling (app B)', 'code', 'AddressPipe', ''],
  ['AddressFormComponent', 'demo-app-a', 'src/address/address-form.component.ts',
    'Address handling (app A)', 'code', 'AddressPipe', ''],
  ['AddressPipe', 'demo-app-a', 'src/pipes/address.pipe.ts',
    'Address handling (app A)', 'code', 'DEMO-1,AddressFormComponent', 'DEMO-1'],
  ['AddressPipe', 'demo-app-b', 'src/pipes/address.pipe.ts',
    'Address handling (app B)', 'code', 'AddressEntryComponent', ''],
  ['Card payment succeeds', 'demo-e2e', 'features/payment.feature',
    'Business Features: Payments', 'scenario', 'Pay a fine online', ''],
  ['CheckoutContainer', 'demo-app-a', 'src/checkout/checkout.container.ts',
    'Payments', 'code', 'PaymentService', ''],
  ['DEMO-1', '', '', 'Business Features: Payments', 'ticket',
    'Pay a fine online,AddressPipe', 'DEMO-1'],
  ['Pay a fine online', 'demo-e2e', 'features/payment.feature',
    'Business Features: Payments', 'feature',
    'PaymentService,DEMO-1,Card payment succeeds', ''],
  ['PayContainer', 'demo-app-b', 'src/pay/pay.container.ts', 'Payments', 'code',
    'PaymentService', ''],
  ['PaymentService', 'demo-core', 'src/payment.service.ts', 'Payments', 'code',
    'Pay a fine online,pay-service (prd),CheckoutContainer,PayContainer', 'DEMO-2'],
  ['pay-service (prd)', 'demo-deploy', 'ansible/group_vars/prd/pay-service_values.yaml.j2',
    'Payments', 'concept', 'PaymentService,prd', ''],
  ['prd', 'demo-deploy', '', 'Payments', 'concept', 'pay-service (prd)', ''],
  // The single-root repository whose file names need escaping (#423). No label
  // for community 5 in LABELS, so its community column is empty.
  ['LedgerBook', 'demo-ledger', 'repositories/demo-ledger/src/odd names/Ledger (v2) #1 50%!*.ts',
    '', 'code', 'LedgerLine', ''],
  ['LedgerLine', 'demo-ledger',
    'repositories/demo-ledger/src/caf\u00e9/it\'s \u00fc\u2603 <b>&"x".ts',
    '', 'code', 'LedgerBook', ''],
];
const flatten = (rows) => rows.map((r) => r.join('\u241f')).sort().join('\n');
/** @param {any[][]} data */
const project = (data) => data.map(
  (e) => [e[0], e[1], e[2], e[3], e[4], e[6].join(','), e[7].join(',')]
);

/** Assert a page's decoded rows are exactly the rows the build built.
 * @param {string} label @param {any[][]} data */
function checkRows(label, data) {
  if (flatten(project(data)) === flatten(EXPECTED_ROWS)) {
    console.log(`ok    interning: ${label} (${data.length} rows)`);
    return;
  }
  failures++;
  console.error(`FAIL  interning: ${label} - not the rows the build built`);
  console.error(`      decoded:  ${flatten(project(data)).slice(0, 400)}`);
  console.error(`      expected: ${flatten(EXPECTED_ROWS).slice(0, 400)}`);
}

checkRows('every row decodes to the values it stood for', api.DATA);

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
  ['1 scripted business feature matches this journey']);
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
// Branch-only work (#425). DEMO-4's only commit sits on a branch, so the intent
// stage wrote its record with `proposed: true` and the branch it was found on.
// Without the label the lookup reads exactly like DEMO-1's: a ticket, a
// description, a date range - an unmerged proposal presented as what the code
// does. The wording is the knowledge-store skill's, so the page and an agent
// reading the same store say the same thing. The branch name carries markup, an
// ampersand and quotes - a valid git ref - so it is matched here as escaped text;
// the inert checks further down assert no live tag came out of it.
const DEMO4_LABEL = 'proposed on branch feature/gazetteer&amp;&quot;lookup&quot;&lt;img/src=x/onerror=alert(1)&gt;';
check('DEMO-4', 'ticket lookup',
  [DEMO4_LABEL,
   'not confirmed on the default branch',
   'Look up the street through the gazetteer before entry']);
// DEMO-5 landed AND has a branch-only commit, so its record carries `branches`
// without `proposed`. Catches the label keyed on `branches`: that would call
// shipped work a proposal, the reverse of the defect above.
check('DEMO-5', 'ticket lookup', ['Document the rollback runbook'], [],
  { text: ['proposed', 'feature/runbook-appendix'] });
// DEMO-5's branch-only commit said "Draft the runbook appendix", and the ticket
// landed (#429). Catches that text pooled into the landed evidence, where it
// reads as an ordinary commit subject - a draft nobody merged quoted as what the
// ticket changed. It must still be shown, under a label saying where it came
// from. Asserted on the markup, because the label and the text are one row.
const BRANCH_SUBJECT = 'commit subject, from a branch-only commit';
const BRANCH_BODY = 'commit body, from a branch-only commit';
const DRAFT = 'Draft the runbook appendix';
const DRAFT_BODY = 'Lists the appendix tables the on-call rota reads first.';
api.q.value = 'DEMO-5';
api.runAsk();
const demo5Html = api.out.innerHTML;
for (const [name, ok] of [
  ['a branch-only subject is shown under its own label',
    demo5Html.includes('<span class="ev-l">' + BRANCH_SUBJECT + '</span> ' + DRAFT)],
  ['a branch-only body is shown under its own label',
    demo5Html.includes('<summary>' + BRANCH_BODY + ' (1)</summary>')
      && demo5Html.includes(DRAFT_BODY)],
  ['a branch-only subject is not presented as a landed subject or description',
    !demo5Html.includes('<span class="ev-l">' + 'commit subject</span> ' + DRAFT)
      && !demo5Html.includes('<span class="ev-l">' + 'description, from commit messages</span> ' + DRAFT)],
]) {
  if (ok) {
    console.log(`ok    DEMO-5: ${name}`);
  } else {
    failures++;
    console.error(`FAIL  DEMO-5: ${name}`);
    console.error(`      answer: ${demo5Html.slice(0, 600)}`);
  }
}
// The same text is searchable and labelled where a question finds it. "appendix"
// is in DEMO-5's branch-only commit and nowhere else in this estate, so a search
// that skipped `ps` and `pb` would report the word as unevidenced.
check('what goes in the runbook appendix?', 'commit evidence',
  ['What the commits say about these words', 'DEMO-5', BRANCH_SUBJECT, DRAFT], [],
  { meta: ['appendix'], text: ['proposed'] });
// A count of one reads in the singular (#429). DEMO-4 has one commit, and DEMO-2
// is carried by one entry in one repository. Catches "1 commits" and its kin.
check('DEMO-4', 'ticket lookup', ['1 commit,'], [], { text: ['1 commits'] });
check('DEMO-2', 'ticket lookup', ['touches 1 graph entry across 1 repository.'], [],
  { text: ['1 graph entries', '1 repositories'] });
// The other answer shapes count the same way. LedgerBook sits alone in its
// repository with one neighbour, so every count these three draw is one.
const ONE_PLURALS = ['1 repositories', '1 matching nodes', '1 nodes', '1 connected nodes',
  '1 communities', '1 connections', '1 items'];
check('which repositories implement LedgerBook?', 'which repositories',
  ['appears in 1 repository (1 matching node).'], [], { text: ONE_PLURALS });
check('where is LedgerBook used?', 'where is it used',
  ['is directly connected to 1 node.', 'demo-ledger 1 connected node ', '(1 connection)',
   'Technical evidence (2 items)'], [], { text: ONE_PLURALS });
check('what is impacted if LedgerBook changes?', 'impact analysis',
  ['could reach 1 connected node within two hops, spanning 1 community in 1 repository.',
   'unlabelled 1 node ', 'Technical evidence (1 item)'], [], { text: ONE_PLURALS });
api.q.value = 'LedgerBook';
api.runSearch();
if (api.meta.textContent === '1 match') {
  console.log('ok    search: one result reads "1 match"');
} else {
  failures++;
  console.error(`FAIL  search: one result reads "${api.meta.textContent}", not "1 match"`);
}
// The other place a ticket is a search result: the evidence section of an
// answer, found by the words its commits used. "gazetteer" is in DEMO-4's
// commits and nowhere else, so this row is the only thing that can answer.
// Catches the label drawn in the lookup but not where the ticket is found.
check('which gazetteer does the lookup use?', 'commit evidence',
  ['What the commits say about these words', 'DEMO-4', DEMO4_LABEL]);
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
// The status line describes the question being answered, not the one before it
// (#429). A ticket lookup returned before the note was reset, so after the
// question above DEMO-1's status line still said "no evidence for: quantum".
// The question above has to have left a note, or this passes over nothing.
if (!api.meta.textContent.includes('no evidence for: quantum')) {
  failures++;
  console.error('FAIL  stale note: the previous question left no note, so the check is vacuous');
}
check('DEMO-1', 'ticket lookup', ['DEMO-1'], [], { meta: ['no evidence for', 'expanded:'] });

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
    ...(info.ps || []).map((t) => ['branch-only commit subject', t]),
    ...(info.pb || []).map((t) => ['branch-only commit body', t]),
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

// A thread the tracker returned only part of must say so where its comments
// are shown (#431): the fixture marks DEMO-1's thread and leaves DEMO-2's
// alone, so the note has to appear for one and not the other.
const TRUNCATION_NOTE = 'not every comment in this thread was fetched';
for (const [id, expected] of [['DEMO-1', true], ['DEMO-2', false]]) {
  api.q.value = id;
  api.runAsk();
  const shown = norm(strip(api.out.innerHTML)).includes(TRUNCATION_NOTE);
  if (shown === expected) {
    console.log(`ok    ${id}: truncated-thread note ${expected ? 'shown' : 'absent'}`);
  } else {
    failures++;
    console.error(`FAIL  ${id}: truncated-thread note ${expected ? 'missing' : 'shown without a mark'}`);
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

// A branch name reaches the page from the repository, through the intent stage,
// and git accepts markup in one. DEMO-4's carries an <img> with a handler, so
// both places that draw its label must render it inert. Catches the label
// interpolated unescaped - the fixture's branch was benign before, and an
// unescaped label then passed this whole file.
for (const question of ['DEMO-4', 'which gazetteer does the lookup use?']) {
  api.q.value = question;
  api.runAsk();
  assertInert(`a hostile branch name in the proposed label: ${question}`, api.out.innerHTML);
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

// --- the page format marker, both directions (#332) -----------------------
// A store publishes `explorer.html`; another environment runs `check-answers`
// against it with whatever library version it has installed. Before the marker,
// an interned page read by a pre-interning library failed on
// `a.localeCompare is not a function` - loud, but only by accident: a sort over
// a column that now held integers, not a format check. On a path not reaching
// that sort first, the integers would have been ranked as labels and the run
// would have reported a plausible score.
//
// Three assertions, and the second and third are what stop the first being a
// trap. A refusal on an unrecognised format is worth nothing if it also refuses
// the pages already published, which carry no marker at all.
const pageConfig = JSON.parse(jsonBlocks.config || '{}');
// The intent stage keeps five branches per ticket (BRANCH_LIMIT), and the page
// says "and possibly others" at the cap only if the build tells it the cap.
// Catches the build no longer writing it: every capped list would then read as
// the complete set of branches.
if (pageConfig.branchLimit === 5) {
  console.log('ok    page config: carries the intent stage\'s branch cap (5)');
} else {
  failures++;
  console.error('FAIL  page config: branchLimit is'
    + ` ${JSON.stringify(pageConfig.branchLimit)}, not the intent stage's cap of 5`);
}
if (READS_PAGE_FORMATS.includes(pageConfig.pageFormat)) {
  console.log(`ok    page format: this page declares format ${pageConfig.pageFormat}, which`
    + ' this library reads');
} else {
  failures++;
  console.error('FAIL  page format: the built page declares'
    + ` ${JSON.stringify(pageConfig.pageFormat)}, which is not a format this library reads`);
  console.error(`      (it reads ${READS_PAGE_FORMATS.join(' and ')}). A page that declares`);
  console.error('      nothing must be read as format 1, so a builder that stopped writing');
  console.error('      the marker would leave every reader-side check green - the two lines');
  console.error('      that carry this: PAGE_FORMAT in build_explorer.py, and READS_PAGE_FORMATS');
  console.error('      in explorer_harness.mjs');
}

// A format from the future, refused by name. The whole point of the marker: a
// reader that cannot decode a page says so, instead of ranking whatever it
// finds. Written into a copy of the real page, so what is refused is a page that
// is otherwise entirely loadable.
const futureDir = mkdtempSync(join(tmpdir(), 'ksb-futureformat-'));
try {
  const futurePage = join(futureDir, 'explorer.html');
  const built = readFileSync(pagePath, 'utf-8');
  const futureHtml = built.replace(
    /(<script id="config" type="application\/json">)[^]*?(<\/script>)/,
    (_m, open, close) => open + JSON.stringify({ ...pageConfig, pageFormat: 99 }) + close
  );
  if (!futureHtml.includes('"pageFormat":99')) {
    failures++;
    console.error('FAIL  page format: the fixture for an unreadable page was not built,');
    console.error('      so the refusal below would be asserted against the real page');
  } else {
    writeFileSync(futurePage, futureHtml);
    let refusal = '';
    try {
      loadPage(futurePage, { requireVerbatim: false });
    } catch (e) {
      refusal = e instanceof Error ? e.message : String(e);
    }
    // By name, not merely by throwing: the failure this replaces was a
    // `TypeError` from a sort, and a reader could not tell it from a bug in the
    // engine. The message has to name the page's format, this library's, and
    // what to do about it.
    const wanted = ['page format 99', 'this library reads page format 1 and 2',
      'Upgrade hmcts-knowledge-store-builder'];
    const absent = wanted.filter((s) => !refusal.includes(s));
    if (refusal && !absent.length) {
      console.log('ok    page format: a page declaring a format this library cannot read is'
        + ' refused by name');
    } else {
      failures++;
      console.error('FAIL  page format: a page declaring format 99 was not refused by name');
      console.error(`      got: ${refusal || '(loaded without complaint)'}`);
      for (const s of absent) console.error(`      message missing: "${s}"`);
    }
  }
} finally {
  rmSync(futureDir, { recursive: true, force: true });
}

// --- a page built before interning still loads and answers (#245) ---------
// Two lines carry that guarantee - `OPTIONAL_BLOCKS` in the shipped harness and
// app.js's `|| '{}'` fallback - and neither was observed. Moving `dicts` into
// REQUIRED_BLOCKS left the Python suite, engine-unit, page-regression and
// check-js all green while a page built before this format failed to load at
// all, naming a block its builder never wrote. Every store that has committed a
// page and not yet rebuilt is in that blast radius, and `check-answers` runs
// against exactly those pages.
//
// So this constructs one: the real fixture page with its rows written out plain
// and the `#dicts` tag removed entirely. Not byte-for-byte what the old build
// wrote - `JSON.stringify` is compact where the builds of that era put a space
// after every separator - and that is not the property under test. What both
// fallbacks key on is the block being ABSENT and the rows being plain values,
// and that is reproduced exactly.
//
// app.js is required from a COPY, deliberately. Node caches modules by path, so
// a second `require` of the shipped path hands back the first page's module
// without re-running it - the assertion would then re-check the interned page
// and pass whatever the fallback did.
const legacyDir = mkdtempSync(join(tmpdir(), 'ksb-preinterning-'));
try {
  const legacyApp = join(legacyDir, 'app.js');
  copyFileSync(APP_PATH, legacyApp);
  const built = readFileSync(pagePath, 'utf-8');
  // `replaceAll` with a plain string, and `String.raw` for the replacement: a
  // global regex and a backslash-escaped literal are the same two bytes and
  // both draw a Sonar smell that the JavaScript analyser will not let a
  // NOSONAR silence.
  const plain = JSON.stringify(api.DATA).replaceAll('</', String.raw`<\/`);
  // The `pageFormat` marker goes too (#332). A page from before interning
  // carries no marker, because the marker did not exist - and an unmarked page
  // must read as format 1 rather than as a refusal, or the check that exists to
  // stop a silent mis-read becomes a refusal of every page already published.
  // Leaving the built page's `"pageFormat": 2` in place here would test a page
  // no builder ever wrote, and this assertion would then say nothing about the
  // pages actually at risk.
  const legacyConfig = { ...pageConfig };
  delete legacyConfig.pageFormat;
  const legacyHtml = built
    .replace(/<script id="dicts" type="application\/json">[^]*?<\/script>\n/, '')
    .replace(
      /(<script id="config" type="application\/json">)[^]*?(<\/script>)/,
      (_m, open, close) => open + JSON.stringify(legacyConfig) + close
    )
    .replace(
      /(<script id="data" type="application\/json">)[^]*?(<\/script>)/,
      (_m, open, close) => open + plain + close
    );
  // Both substitutions have to have bitten. A regex that matched nothing leaves
  // the interned page in place, and everything below would then pass while
  // testing the case it exists to rule out.
  const problems = [];
  if (/<script id="dicts"/.test(legacyHtml)) problems.push('the #dicts block was not removed');
  if (legacyHtml === built) problems.push('the page was not rewritten at all');
  if (legacyHtml.includes('"demo-core"') === false) problems.push('the plain rows are missing');
  // The block, not the page: app.js documents the marker in a comment, and it is
  // inlined here, so a search over the whole page finds `pageFormat` whatever
  // the config block says.
  if ('pageFormat' in JSON.parse(extractJsonBlocks(legacyHtml).config || '{}')) {
    problems.push('the pageFormat marker was not removed');
  }
  if (problems.length) {
    failures++;
    console.error('FAIL  pre-interning page: the fixture for it was not built');
    for (const problem of problems) console.error(`      ${problem}`);
  } else {
    const legacyPage = join(legacyDir, 'explorer.html');
    writeFileSync(legacyPage, legacyHtml);
    // Caught rather than left to propagate. Both ways of breaking this - the
    // block becoming required, or the fallback going - throw during the load,
    // and an unhandled stack trace says a page failed somewhere. What a reader
    // needs is the sentence: a page a store has already committed no longer
    // opens. So the failure is named, and the run still reaches its summary.
    try {
      // requireVerbatim off: this page deliberately does NOT inline app.js
      // byte-for-byte, which is the whole situation a published page is in.
      const legacy = loadPage(legacyPage, { appPath: legacyApp, requireVerbatim: false });
      checkRows('a page with no #dicts block and no pageFormat marker loads and holds the'
        + ' same rows', legacy.api.DATA);
    } catch (e) {
      failures++;
      console.error('FAIL  pre-interning page: a page built before interning no longer loads,');
      console.error('      so every store that has committed one and not yet rebuilt would');
      console.error('      have `check-answers` fail against its own published page.');
      console.error(`      ${e.message}`);
      console.error('      the three lines that carry this: OPTIONAL_BLOCKS in');
      console.error("      explorer_harness.mjs, app.js's `|| '{}'` fallback for an absent");
      console.error('      #dicts block, and `declaredPageFormat` reading an absent marker as 1');
    }
  }
} finally {
  rmSync(legacyDir, { recursive: true, force: true });
}

// --- every row opens its file at the build commit (#423) -------------------
// The page carries one link base per repository in #config, and app.js joins it
// to the row's own path and line. The graph already holds the link for every
// node, written by the real `source-urls` stage in fixture.py, and the two must
// be the same bytes: a page link that differs from the graph's `url` cites a
// different file, or the same file under a second spelling.

/** Record one comparison of what the page did against what it should have.
 * @param {string} name the break this catches @param {unknown} observed
 * @param {unknown} expected derived by hand */
function expectSame(name, observed, expected) {
  const got = JSON.stringify(observed);
  const want = JSON.stringify(expected);
  if (got === want) {
    console.log(`ok    ${name}`);
    return;
  }
  failures++;
  console.error(`FAIL  ${name}`);
  console.error(`      expected ${want}`);
  console.error(`      observed ${got}`);
}

const graphNodes = JSON.parse(readFileSync(join(dirname(pagePath), 'graph.json'), 'utf-8')).nodes;
// Catches: any row whose joined link is not the node's `url` - the base built a
// second way, the single-root prefix kept, a path escaped differently from
// Python's `quote`, a line anchor dropped or invented, or a link written for a
// repository the graph links nothing in (another host, no recorded commit).
// A row matches its node on label, repository and file, and each must match one.
const linkMismatches = [];
for (const row of api.DATA) {
  const nodes = graphNodes.filter((n) => n.label === row[0] && (n.repo || '') === row[1]
    && (n.source_file || '') === row[2]);
  if (nodes.length !== 1) {
    linkMismatches.push({ row: row[0], nodesMatched: nodes.length });
    continue;
  }
  const link = api.sourceLink(row);
  if (link !== (nodes[0].url || '')) {
    linkMismatches.push({ row: row[0], page: link, graph: nodes[0].url || '' });
  }
}
expectSame('links: every row links exactly where the graph url for its node does',
  linkMismatches, []);

// Not vacuous: the comparison above passes over a page that links nothing when
// the graph links nothing too. Counted by hand from fixture.py: three rows each
// in demo-app-a and demo-app-b, PaymentService, and both demo-ledger rows link;
// the demo-e2e feature and scenario (not on github.com), the deployment and its
// environment (no recorded commit) and the DEMO-1 ticket (no file) do not.
expectSame('links: the rows with a link are the ones the fixture configures',
  api.DATA.filter((e) => api.sourceLink(e)).map((e) => e[0]).sort(),
  ['AddressEntryComponent', 'AddressFormComponent', 'AddressPipe', 'AddressPipe',
    'CheckoutContainer', 'LedgerBook', 'LedgerLine', 'PayContainer', 'PaymentService']);

// The links themselves, written out, so the comparison above cannot pass by the
// graph and the page agreeing on something wrong. The escapes are Python's
// `quote(path, safe="/")`; the single-root prefix is gone; the anchors are the
// fixture's `source_location` values.
const LEDGER = 'https://github.com/example-org/demo-ledger/blob/' + 'd'.repeat(40) + '/';
const linkOf = (label) => api.DATA.filter((e) => e[0] === label).map(api.sourceLink).sort();
expectSame('links: a single-root file with awkward characters links escaped, at its line',
  linkOf('LedgerBook'),
  [LEDGER + 'src/odd%20names/Ledger%20%28v2%29%20%231%2050%25%21%2A.ts#L3']);
expectSame('links: non-ASCII and markup in a file name are percent-encoded',
  linkOf('LedgerLine'),
  [LEDGER + 'src/caf%C3%A9/it%27s%20%C3%BC%E2%98%83%20%3Cb%3E%26%22x%22.ts']);
expectSame('links: a line range anchors as a range',
  linkOf('PaymentService'),
  ['https://github.com/example-org/demo-core/blob/' + 'c'.repeat(40)
    + '/src/payment.service.ts#L5-L9']);

// Catches: the link computed but never rendered, rendered without `rel` or
// `target`, or the file name written raw - the fixture's carries `<b>`, `&` and
// quotes, so an unescaped name injects markup into the card.
api.q.value = 'LedgerLine';
api.runSearch();
const ledgerCard = api.out.innerHTML;
expectSame('links: the search card renders the file as an escaped link opening a new tab',
  ledgerCard.includes('<a href="' + LEDGER
    + 'src/caf%C3%A9/it%27s%20%C3%BC%E2%98%83%20%3Cb%3E%26%22x%22.ts" target="_blank"'
    + ' rel="noopener">repositories/demo-ledger/src/café/it\'s ü☃'
    + ' &lt;b&gt;&amp;&quot;x&quot;.ts</a>'),
  true);
expectSame('links: no raw markup from a file name reaches the card',
  ledgerCard.includes('<b>&"x"'), false);

// Catches: a link rendered for a repository with no base. Each card's path line
// is read on its own, so an anchor elsewhere in the results cannot mask one.
/** @param {string} query @param {string} repo @returns {string[]} */
function pathLines(query, repo) {
  api.q.value = query;
  api.runSearch();
  return api.out.innerHTML.split('<div class="path">').slice(1)
    .map((chunk) => chunk.split('</div>')[0])
    .filter((line) => line.startsWith(repo + ' '));
}
for (const [query, repo] of [['Pay a fine online', 'demo-e2e'], ['pay-service', 'demo-deploy']]) {
  const lines = pathLines(query, repo);
  expectSame(`links: ${repo}'s card shows its file with no link`,
    { cards: lines.length > 0, linked: lines.some((line) => line.includes('<a ')) },
    { cards: true, linked: false });
}

// Catches: the "defined in" table of a which-repositories answer still printing
// bare paths - the other place the page shows a file.
api.q.value = 'which repositories implement AddressPipe?';
api.runAsk();
const repoTable = api.out.innerHTML;
expectSame('links: the which-repositories table links each file it names',
  [
    'https://github.com/example-org/demo-app-a/blob/' + 'a'.repeat(40)
      + '/src/pipes/address.pipe.ts#L12',
    'https://github.com/example-org/demo-app-b/blob/' + 'b'.repeat(40)
      + '/src/pipes/address.pipe.ts',
  ].map((url) => repoTable.includes('<a href="' + url + '" target="_blank" rel="noopener">'
    + 'src/pipes/address.pipe.ts</a>')),
  [true, true]);

// --- inputs are labelled and nothing steals focus (#514) -------------------
// Catches: an <input> with no accessible name (WCAG 1.3.1 / 4.1.2) - a screen
// reader announces it as an unnamed edit field - and an `autofocus` attribute,
// which moves focus on load without the user asking (WCAG 3.2.1 / 2.4.3).
// A name is an aria-label, a <label for=id> naming it, or an enclosing <label>
// with text of its own. A placeholder is not a name.
const pageHtml = readFileSync(pagePath, 'utf-8');
const inputTags = [...pageHtml.matchAll(/<input\b[^>]*>/g)].map((m) => m[0]);
const labelFors = new Set([...pageHtml.matchAll(/<label\b[^>]*\bfor="([^"]+)"[^>]*>\s*[^<\s]/g)]
  .map((m) => m[1]));
const wrapped = [...pageHtml.matchAll(/<label\b(?![^>]*\bfor=)[^>]*>(?:(?!<\/label>)[\s\S])*?<\/label>/g)]
  .map((m) => m[0]).filter((l) => l.replace(/<[^>]*>/g, '').trim());
const unlabelled = inputTags.filter((tag) => {
  if (/\baria-label="[^"]+"/.test(tag)) return false;
  const id = (tag.match(/\bid="([^"]+)"/) || [])[1];
  if (id && labelFors.has(id)) return false;
  return !wrapped.some((l) => l.includes(tag));
});
expectSame('a11y: the page has inputs to check', inputTags.length > 0, true);
expectSame('a11y: every <input> has an accessible label', unlabelled, []);
expectSame('a11y: no element carries autofocus',
  [...pageHtml.matchAll(/<[a-z][^>]*\bautofocus\b[^>]*>/gi)].map((m) => m[0]), []);

if (failures) {
  console.error(`\n${failures} question shape(s) failed`);
  process.exit(1);
}
console.log('\nall question shapes pass');
