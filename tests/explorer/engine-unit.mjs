// Unit tests for the explorer's Ask-mode engine (the graphify-parity scorer).
//
// Unlike explorer-regression.mjs - which exercises the BUILT page end to end -
// this runs the packaged app.js directly against a tiny synthetic dataset,
// pinning the ranking maths so a refactor cannot drift it invisibly.
//
// Run: node tests/explorer/engine-unit.mjs

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
    title: 'Welsh language in Common Platform',
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

const makeEl = () => ({
  textContent: '', innerHTML: '', value: '', placeholder: '', style: {},
  checked: true, classList: { toggle() {} }, addEventListener() {}, add() {},
});
const elements = {
  data: { textContent: JSON.stringify(DATA) },
  edges: { textContent: JSON.stringify(EDGES) },
  titles: { textContent: '{}' },
  summaries: { textContent: JSON.stringify(SUMMARIES) },
  synonyms: { textContent: JSON.stringify(SYN) },
  tickets: { textContent: '{}' },
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

// bfs: hop distances and cap
const reached = context.bfs([0], 2, 100);
assert('bfs reaches two hops with correct distances',
  reached.get(0) === 0 && reached.get(2) === 1 && reached.get(4) === 2);

if (failures) {
  console.error('\n' + failures + ' engine assertion(s) failed');
  process.exit(1);
}
console.log('\nengine unit tests pass');
