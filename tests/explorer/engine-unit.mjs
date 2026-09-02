// Unit tests for the explorer's Ask-mode engine (the graphify-parity scorer).
//
// Unlike explorer-regression.mjs - which exercises the BUILT page end to end -
// this runs the packaged app.js directly against a tiny synthetic dataset,
// pinning the ranking maths so a refactor cannot drift it invisibly.
//
// Run: node tests/explorer/engine-unit.mjs

import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';

// [label, repo, sourceFile, communityLabel, kind, degree, connections, tickets, communityId]
const DATA = [
  ['AddressPipe', 'repo-a', 'src/pipes/address.pipe.ts', 'Address Handling', 'code', 9, [], ['CRC-12016'], 1],
  ['AddressPipe', 'repo-b', 'src/pipes/address.pipe.ts', 'Address Handling B', 'code', 4, [], [], 2],
  ['AddressInputComponent', 'repo-a', 'src/address-input.ts', 'Address Handling', 'code', 6, [], [], 1],
  ['ResultsService', 'repo-a', 'src/results.service.ts', 'Hearing Results', 'code', 8, [], [], 3],
  ['Amend defendant address', 'repo-e2e', 'features/amend.feature', 'Business Features: CPS', 'feature', 3, [], ['DD-1'], 4],
];
const EDGES = [0, 2, 2, 4]; // AddressPipe(a) - AddressInput; AddressInput - feature
const SUMMARIES = { 3: 'Recording of hearing outcomes and results for court.' };
const SYN = { outcome: [['result', 0.687], ['verdict', 0.623]] };
const DIVES = {
  'demo-core': {
    title: 'Deep dive: demo-core',
    html: '<h2>demo-core</h2>',
    source: 'docs/deep-dives/demo-core.md',
    sha: 'abcd1234',
  },
};
const TOPICS = {
  'welsh-language': {
    title: 'Welsh language support',
    keywords: ['welsh', 'cymraeg', 'bilingual'],
    html: '<h2>Welsh language</h2><p>Headline verdict...</p>',
    source: 'docs/topics/welsh-language.md',
  },
  'address-handling': {
    title: 'How addresses are implemented',
    keywords: ['address', 'postcode'],
    html: '<h2>Addresses</h2>',
    source: 'docs/topics/address-handling.md',
  },
};

// Ticket evidence, as ticket-descriptions.json.gz carries it: `d` the curated
// description, `s` the commit subjects verbatim, `b` the body prose. The keys
// are deliberately NOT in sorted order, so retrieval that depended on object
// iteration order would not produce the required ordering.
//
// Vocabulary that exists nowhere else in this fixture: "settlement" only in a
// subject, "postalCode" only in a body, "reconciliation" only in the tie-break
// tickets. A question using one of those can be answered from ticket evidence
// alone or not at all.
const TICKET_INFO = {
  'DD-1': {
    d: ['Amend the defendant address journey'],
    s: ['Amend the defendant address journey', 'wip'],
    b: ['BREAKING CHANGE: postalCode replaces postcode.\n'
        + '- callers must migrate\n'
        + '- see ADR 0007 <script>alert(1)</script> & the migration plan'],
    first: '2024-01-05', last: '2024-02-01', repos: ['repo-e2e'], n: 4,
  },
  'CRC-12016': {
    d: ['Format the address on the confirmation screen'],
    s: ['Format the address on the confirmation screen', 'Log the settlement reference'],
    first: '2024-01-05', last: '2024-03-01', repos: ['repo-a'], n: 3,
  },
  // one term, repeated: coverage of the query must beat sheer frequency
  'MANY-1': {
    d: ['Address the address validation on the address form'],
    s: ['Address the address validation on the address form'],
    first: '2024-02-02', last: '2024-02-03', repos: ['repo-b'], n: 2,
  },
  'TIE-3': tie('2024-04-03'),
  'TIE-1': tie('2024-04-01'),
  'TIE-4': tie('2024-04-04'),
  'TIE-2': tie('2024-04-02'),
  // The same distinctive word, three times over, inside a long prose body: a
  // long text matches anything by surface area. Its id sorts before RARE-1's,
  // so nothing but length normalisation can put the focused ticket first.
  'LONG-1': {
    b: ['The voucher work is described here at length. '
        + 'It touches the basket, the confirmation screen and the receipt. '.repeat(6)
        + 'The voucher rules were agreed with the business, and the voucher '
        + 'behaviour is unchanged for existing baskets.'],
    first: '2024-06-03', last: '2024-06-04', repos: ['repo-a'], n: 1,
  },
  // two distinctive words, and none of the corpus's ordinary ones
  'RARE-1': {
    d: ['Add the voucher chargeback reversal'],
    s: ['Add the voucher chargeback reversal'],
    first: '2024-06-01', last: '2024-06-02', repos: ['repo-a'], n: 2,
  },
};

/** Four tickets with identical evidence, so only the id can order them.
 * @param {string} day */
function tie(day) {
  return {
    d: ['Nightly reconciliation of the ledger'],
    first: day, last: day, repos: ['repo-a'], n: 1,
  };
}

// Twelve tickets carrying the corpus's ordinary vocabulary - "was", "removed",
// "legacy" - and nothing distinctive. Their job is to make those words common,
// because how informative a word is can only be measured against a corpus. A
// handful of tickets would make every word rare and hide the defect these
// exercise.
for (let i = 1; i <= 12; i++) {
  TICKET_INFO['NOISE-' + i] = {
    d: ['The legacy flag was removed from screen ' + i],
    first: '2024-05-01', last: '2024-05-02', repos: ['repo-a'], n: 1,
  };
}

const makeEl = () => ({
  textContent: '', innerHTML: '', value: '', placeholder: '', style: {},
  insertAdjacentHTML(position, html) {
    this.innerHTML = position === 'afterbegin' ? html + this.innerHTML : this.innerHTML + html;
  },
  checked: true, classList: { toggle() {} }, addEventListener() {}, add() {},
});
const elements = {
  data: { textContent: JSON.stringify(DATA) },
  edges: { textContent: JSON.stringify(EDGES) },
  titles: { textContent: '{}' },
  summaries: { textContent: JSON.stringify(SUMMARIES) },
  synonyms: { textContent: JSON.stringify(SYN) },
  tickets: { textContent: JSON.stringify(TICKET_INFO) },
  config: { textContent: '{}' },
  topics: { textContent: JSON.stringify(TOPICS) },
  dives: { textContent: JSON.stringify(DIVES) },
};
const documentStub = {
  getElementById: (id) => (elements[id] ??= makeEl()),
  querySelectorAll: () => [],
};
globalThis.document = documentStub;
globalThis.Option = function Option() {};
const require = createRequire(import.meta.url);
require('../../src/knowledgestore/assets/app.js');
const context = globalThis.__explorerApi;

let failures = 0;
function assert(name, condition, detail = '') {
  if (condition) {
    console.log('ok    ' + name);
  } else {
    failures++;
    console.error('FAIL  ' + name + (detail ? ' - ' + detail : ''));
  }
}
const deepEqual = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// decodeRows: the data block's interned columns, restored (#245)
//
// The whole of this file's DATA is un-interned - there is no `dicts` element in
// the stub above - so every assertion below it already depends on an absent
// block decoding to the identity. These four exercise the other side directly,
// because the failure is silent: a row is positional, and a column restored
// from the wrong table or read with the wrong key type yields `undefined` in a
// cell every ranking function then reads without complaint.
assert('decodeRows leaves a page with no dictionaries alone',
  deepEqual(context.decodeRows([['AddressPipe', 'repo-a']], {}),
            [['AddressPipe', 'repo-a']]));
assert('decodeRows restores a scalar column from its table',
  deepEqual(context.decodeRows([[0, 'x'], [1, 'y']], { 0: ['repo-a', 'repo-b'] }),
            [['repo-a', 'x'], ['repo-b', 'y']]));
assert('decodeRows restores every element of a list column',
  deepEqual(context.decodeRows([['x', [1, 0, 1]]], { 1: ['DEMO-1', 'DEMO-2'] }),
            [['x', ['DEMO-2', 'DEMO-1', 'DEMO-2']]]));
// A numeric column interns profitably when its values are wider than their
// indices - 13-digit epoch milliseconds are the case that retired the "never
// intern a numeric field" rule - so the table's values must come back as the
// numbers they were, not as the strings a stringifying decoder would hand back.
assert('decodeRows gives a numeric column back its numbers',
  deepEqual(context.decodeRows([[0], [1]], { 0: [1739923200000, 1739923200001] }),
            [[1739923200000], [1739923200001]]));

// queryTerms: stopwords drop, stemming, dedupe, all-stopword fallback
assert('queryTerms drops stopwords and stems plurals',
  deepEqual(context.queryTerms('which repositories implement the addresses?'),
            ['repository', 'implement', 'address']));
assert('queryTerms falls back when everything is a stopword',
  context.queryTerms('why does it').length > 0);

// idfFor: rarer tokens weigh more
const w = context.idfFor(['addresspipe', 'address']);
assert('idf weighs rare tokens above common ones', w['addresspipe'] > w['address'],
  JSON.stringify(w));

// rankNodes: exact label match dominates substring matches
const ranked = context.rankNodes(['addresspipe']);
assert('exact label match ranks first', DATA[ranked[0][1]][0] === 'AddressPipe');
assert('both exact copies outrank the substring-free node',
  ranked.slice(0, 2).every((r) => DATA[r[1]][0] === 'AddressPipe'));

// coverage scaling: matching both terms beats matching one of two
const two = context.rankNodes(['address', 'input']);
assert('term coverage prefers the node matching more of the query',
  DATA[two[0][1]][0] === 'AddressInputComponent',
  DATA[two[0][1]][0]);

// expandTerms: semantic neighbours with discounted weights
const expansions = context.expandTerms(['outcome']);
assert('expandTerms returns discounted MiniLM neighbours',
  deepEqual(expansions.map((e) => e[0]), ['result', 'verdict'])
  && expansions.every((e) => e[1] > 0 && e[1] < 0.5));

// expansion bridges vocabulary: "outcome" alone must reach ResultsService
const semantic = context.rankNodes(['outcome'], expansions);
assert('semantic expansion surfaces results vocabulary',
  semantic.some((r) => DATA[r[1]][0] === 'ResultsService'));

// pickSeeds: guarantees a representative for every term
const seeds = context.pickSeeds(context.rankNodes(['addresspipe', 'resultsservice']),
                                ['addresspipe', 'resultsservice']);
assert('pickSeeds includes a node for every query term',
  seeds.some((i) => DATA[i][0] === 'AddressPipe')
  && seeds.some((i) => DATA[i][0] === 'ResultsService'));

// matchTopic: direct keyword hits select the brief; expansion-only must not
const welsh = context.matchTopic('how does welsh language functionality work?', []);
assert('matchTopic serves the Welsh brief on a direct keyword hit',
  welsh !== null && welsh.slug === 'welsh-language');
assert('matchTopic prefers the topic with more keyword hits',
  context.matchTopic('bilingual welsh address forms', []).slug === 'welsh-language');
assert('matchTopic requires at least one direct hit',
  context.matchTopic('how do court forms work?', [['welsh', 0.4]]) === null);
assert('matchTopic returns null when nothing matches',
  context.matchTopic('how are hearings listed?', []) === null);

// matchDive: repository name as a substring of the question selects its dive
assert('matchDive hits when the question names the repository',
  context.matchDive('what is wrong with demo-core right now?').repo === 'demo-core');
assert('matchDive returns null otherwise',
  context.matchDive('how are payments taken?') === null);

// runAsk: a dive match must survive even when ranking finds nothing at all
// to match against - the "nothing in the graph matches" guard must consider
// dive (and topic), not fire before either is computed (regression: it used
// to short-circuit on an empty ranked list before the dive lookup ran).
// None of this fixture's DATA/labels/sources contain "demo", "core" or
// "internals", so ranking this question yields zero hits.
context.q.value = 'tell me about demo-core internals';
context.runAsk();
assert('a dive match survives a zero-hit ranked list',
  context.meta.textContent.includes('deep dive: demo-core'), context.meta.textContent);
assert('the zero-hit dive question is not reported as unmatched',
  !context.meta.textContent.includes('nothing in the graph matches'), context.meta.textContent);

// Inferred evidence must never outrank direct evidence.
//
// Measured on a real estate before this was fixed: "what handles video
// transcription of hearings?" returned ten Helm-chart `image` fields as its
// top ten, because `image` is a semantic neighbour of `video`. A neighbour
// matching a label exactly earned the prefix tier (x100) and escaped the
// coverage scaling that direct term matches are subject to (cov^2 = 0.0625 for
// one term of four), so a guess at similarity 0.27 scored 229 while the true
// answer - whose own label contains the query term "transcription" - scored 13.
//
// A neighbour is inferred; a term match is evidence. Here "address" is a direct
// prefix match on AddressPipe, while ResultsService matches only the neighbour
// "result" expanded from "outcome". AddressPipe must win.
const dTerms = ['outcome', 'address'];
const dRanked = context.rankNodes(dTerms, context.expandTerms(dTerms));
assert('a semantic neighbour does not outrank a direct term match',
  dRanked.length > 0 && DATA[dRanked[0][1]][0] === 'AddressPipe',
  JSON.stringify(dRanked.slice(0, 3).map((r) => [DATA[r[1]][0], r[0].toFixed(2)])));

// bfs: hop distances and cap
const reached = context.bfs([0], 2, 100);
assert('bfs reaches two hops with correct distances',
  reached.get(0) === 0 && reached.get(2) === 1 && reached.get(4) === 2);

/* ---- ticket evidence: the `s` and `b` fields as a retrieval surface ---- */

const evidenceIds = (terms) => context.ticketEvidence(terms).map((m) => m.id);

// A word only a commit body holds is still evidence. Breaks if the body field
// is not indexed - which was the state of the page before this existed.
assert('a term only a commit body holds surfaces its ticket',
  deepEqual(evidenceIds(['postalcode']), ['DD-1']), JSON.stringify(evidenceIds(['postalcode'])));

// Same for a subject the description filter rejected or never carried.
assert('a term only a commit subject holds surfaces its ticket',
  deepEqual(evidenceIds(['settlement']), ['CRC-12016']),
  JSON.stringify(evidenceIds(['settlement'])));

// A single letter matches almost any prose, so it is an accident, not a query.
assert('a one-character term alone surfaces nothing',
  deepEqual(evidenceIds(['a']), []), JSON.stringify(evidenceIds(['a'])));
assert('a one-character term alongside a real one still retrieves',
  deepEqual(evidenceIds(['a', 'postalcode']), ['DD-1']),
  JSON.stringify(evidenceIds(['a', 'postalcode'])));

// Cap and determinism: four tickets carry identical evidence, so their weights
// and occurrence counts are equal and only the id can order them - and only
// three may be shown.
assert('at most three tickets, ordered by id when the weights tie',
  deepEqual(evidenceIds(['reconciliation']), ['TIE-1', 'TIE-2', 'TIE-3']),
  JSON.stringify(evidenceIds(['reconciliation'])));

// Carrying a rarer word beats repeating a commoner one: DD-1 has "address" and
// the corpus's only "postalCode", MANY-1 has "address" six times over.
assert('a rarer term outranks more occurrences of a commoner one',
  evidenceIds(['address', 'postalcode'])[0] === 'DD-1',
  JSON.stringify(context.ticketEvidence(['address', 'postalcode'])));

/* ---- how informative a word is decides the ranking ----
 *
 * The regression that matters, measured on a real estate before this was fixed.
 * A question carrying one distinctive word and two ordinary ones ("was",
 * "removed") returned three tickets, and not one of them was among the 34 whose
 * evidence held the distinctive word: every winner had matched only the two
 * ordinary words. Counting how many terms a ticket matched treats every word as
 * equally informative, so three tickets matching two throwaway words beat the
 * ticket carrying the word that gives the question its meaning - and the page
 * then states, under a labelled evidence heading, that those tickets answer it.
 * Confidently wrong is worse than silent.
 *
 * What separates them is rarity measured in the ticket corpus itself. Not a
 * stopword list: which words are ordinary varies by corpus, a fixed list is
 * never complete, and this estate has taught that lesson once already.
 */
const skewed = ['was', 'voucher', 'removed'];
assert('the distinctive word decides the ranking, not the ordinary ones',
  evidenceIds(skewed)[0] === 'RARE-1', JSON.stringify(context.ticketEvidence(skewed)));
assert('tickets matching only the ordinary words are not returned at all',
  !evidenceIds(skewed).some((id) => id.startsWith('NOISE-')),
  JSON.stringify(evidenceIds(skewed)));

// Two rare words carry more of a question than three ordinary ones, whatever the
// term count says: RARE-1 matches two of these five terms, each NOISE ticket
// matches three.
const mixed = ['voucher', 'chargeback', 'was', 'removed', 'legacy'];
assert('two rare terms outrank three common ones',
  evidenceIds(mixed)[0] === 'RARE-1', JSON.stringify(context.ticketEvidence(mixed)));

// Where two tickets carry the same distinctive word, the one whose evidence is
// about that word beats the one that merely contains it. Measured on a real
// estate: the tickets that wrongly won a question were long prose bodies holding
// its ordinary words, and the tickets that deserved to win were one-line
// subjects naming its subject. LONG-1 repeats "voucher" three times in a long
// body and sorts first by id, so only length normalisation puts RARE-1 above it.
assert('a short focused ticket outranks a long one repeating the same word',
  evidenceIds(['voucher'])[0] === 'RARE-1', JSON.stringify(context.ticketEvidence(['voucher'])));

// A word in every ticket's evidence tells you nothing about which ticket to
// read, so it must contribute almost nothing to the score.
const weightOf = (terms) => context.ticketEvidence(terms)[0].weight;
assert('a word in almost every ticket contributes almost nothing',
  weightOf(['the']) < weightOf(['voucher']) / 2,
  `ubiquitous ${weightOf(['the'])} vs rare ${weightOf(['voucher'])}`);

// The absence rule and the new surface must agree. Before this, a term the
// index held only in a commit body was reported as unevidenced while its
// evidence was rendered directly below - the page contradicting itself.
assert('a term evidenced only in a commit body is not reported as unevidenced',
  deepEqual(context.unevidencedTerms(['postalcode']), []),
  JSON.stringify(context.unevidencedTerms(['postalcode'])));
assert('a term evidenced only in a commit subject is not reported as unevidenced',
  deepEqual(context.unevidencedTerms(['settlement']), []),
  JSON.stringify(context.unevidencedTerms(['settlement'])));
assert('a term nothing holds is still reported as unevidenced',
  deepEqual(context.unevidencedTerms(['quantum']), ['quantum']),
  JSON.stringify(context.unevidencedTerms(['quantum'])));

/* ---- display ---- */

context.q.value = 'DD-1';
context.runAsk();
const ticketHtml = context.out.innerHTML;
assert('a ticket lookup names the commit body as a commit body',
  /commit body/i.test(ticketHtml));
assert('the body is not presented as the ticket title',
  ticketHtml.includes('“Amend the defendant address journey”')
  && ticketHtml.indexOf('BREAKING CHANGE') > ticketHtml.indexOf('<details'),
  ticketHtml.slice(0, 300));
assert('markup in a body is escaped, not rendered',
  ticketHtml.includes('&lt;script&gt;') && !ticketHtml.includes('<script>')
  && ticketHtml.includes('&amp;'),
  ticketHtml.slice(ticketHtml.indexOf('BREAKING CHANGE'), ticketHtml.indexOf('BREAKING CHANGE') + 260));
assert("a body's line breaks survive rendering",
  ticketHtml.includes('postcode.<br>') && !ticketHtml.includes('postcode.\n'),
  ticketHtml.slice(ticketHtml.indexOf('BREAKING CHANGE'), ticketHtml.indexOf('BREAKING CHANGE') + 260));

context.q.value = 'CRC-12016';
context.runAsk();
const crcHtml = context.out.innerHTML;
// the evidence rows sit between the answer paragraph and the first disclosure
const crcEvidence = crcHtml.slice(crcHtml.indexOf('</div>'), crcHtml.indexOf('<details'));
assert('a subject the description does not carry is shown, labelled as a subject',
  /commit subject/i.test(crcEvidence) && crcEvidence.includes('Log the settlement reference'),
  crcEvidence);
assert('a subject the description already says is not repeated',
  !crcEvidence.includes('Format the address on the confirmation screen'), crcEvidence);

// Ask mode: a question only ticket evidence can answer gets that evidence,
// and is neither silenced nor reported as unevidenced.
context.q.value = 'what happened to postalCode?';
context.runAsk();
assert('a question only a commit body answers surfaces that ticket',
  context.out.innerHTML.includes('DD-1') && /commit body/i.test(context.out.innerHTML),
  context.out.innerHTML.slice(0, 300));
assert('a body-evidenced question is not answered "no evidence in this estate"',
  !context.out.innerHTML.includes('No evidence in this estate'),
  context.out.innerHTML.slice(0, 200));
assert('the body-evidenced term is not named as unevidenced',
  !context.meta.textContent.includes('no evidence for: postalcode'),
  context.meta.textContent);

// A question no ticket evidence matches must render byte-for-byte as it did
// before ticket evidence existed: this is an additional section, never a
// replacement. Recorded from the unmodified engine against this fixture.
context.q.value = 'which repositories implement AddressPipe?';
context.runAsk();
const unchanged = createHash('sha256')
  .update(context.meta.textContent + ' ' + context.out.innerHTML)
  .digest('hex');
assert('a question with no ticket evidence renders exactly as before',
  unchanged === '734c03f122f115a1689d34123e3fca76b0e5eb1e211cdfc5722a4bb79fe282cc', unchanged);

if (failures) {
  console.error('\n' + failures + ' engine assertion(s) failed');
  process.exit(1);
}
console.log('\nengine unit tests pass');
