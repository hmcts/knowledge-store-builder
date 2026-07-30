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
// regex over a potentially very large page).
const jsonBlocks = {};
for (const part of html.split('</script>')) {
  const open = part.lastIndexOf('<script');
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

function check(question, wantMode, wantSubstrings, wantMetaSubstrings = []) {
  api.q.value = question;
  api.runAsk();
  const gotMode = api.meta.textContent;
  const text = strip(api.out.innerHTML);
  const problems = [];
  if (!gotMode.includes(wantMode)) {
    problems.push(`mode: wanted "${wantMode}", got "${gotMode}"`);
  }
  for (const s of wantSubstrings) {
    if (!text.includes(s)) problems.push(`missing: "${s}"`);
  }
  for (const s of wantMetaSubstrings) {
    if (!gotMode.includes(s)) problems.push(`meta missing: "${s}"`);
  }
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
// a topic match takes precedence over the request-a-brief link, unchanged
check('how are payments taken?', 'open question',
  ['No pre-written brief covers this question']);
// nothing in the graph matches: say so plainly
check('how is quantum scheduling implemented?', 'nothing in the graph matches', []);

if (failures) {
  console.error(`\n${failures} question shape(s) failed`);
  process.exit(1);
}
console.log('\nall question shapes pass');
