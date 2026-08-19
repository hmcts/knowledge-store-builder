// Break the store on purpose, and require the answer gate to notice.
//
// A green harness reports two things with the same output: that the store answers
// its questions, and that nothing is checking whether it does. This distinguishes
// them. Every mutation below is a failure that actually happened on a real estate,
// or one that survived an earlier version of this very runner.
//
// The runner's own history is why this file exists. Three successive versions of
// the ticket-mode predicate passed the good page and every mutation:
//
//   1. claimed `ticket` from the id appearing in the question - true of any string
//   2. accepted either evidence source, so blanking either one alone still passed
//      (the composite-masks-a-dead-part trap, reproduced inside the runner built
//      to catch it)
//   3. counted the ticket's own graph node as evidence that it reached the graph
//
// None of that was visible from a passing run. All three were found here.
//
// Run: python3 tests/explorer/fixture.py && node tests/explorer/answer-regression-mutations.mjs

import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const runner = join(root, 'src', 'knowledgestore', 'assets', 'answer_regression.mjs');
const questions = join(here, 'fixtures', 'questions.txt');
const page = process.env.KSB_FIXTURE_PAGE
  || join(root, '.fixture-store', 'graphify-out', 'explorer.html');

let source;
try {
  source = readFileSync(page, 'utf-8');
} catch {
  console.error(`FAIL  no built page at ${page}`);
  console.error('      run: python3 tests/explorer/fixture.py');
  process.exit(1);
}

const work = mkdtempSync(join(tmpdir(), 'ksb-answer-mutations-'));

/** Replace one embedded JSON block's content. Fails loudly if the block moved,
 * because a mutation applied to nothing is a mutation that proves nothing.
 * @param {string} html @param {string} id @param {(text: string) => string} change
 */
function editBlock(html, id, change) {
  const open = html.indexOf(`id="${id}"`);
  if (open < 0) throw new Error(`block #${id} not found - the page layout changed`);
  const tagEnd = html.indexOf('>', open);
  const close = html.indexOf('</script>', tagEnd);
  if (tagEnd < 0 || close < 0) throw new Error(`block #${id} is not a closed script tag`);
  const before = html.slice(tagEnd + 1, close);
  const after = change(before);
  if (after === before) throw new Error(`mutation of #${id} changed nothing`);
  return html.slice(0, tagEnd + 1) + after + html.slice(close);
}

/** @param {string} pagePath */
function runGate(pagePath) {
  const r = spawnSync(process.execPath, [runner, '--page', pagePath, '--questions', questions], {
    encoding: 'utf-8',
  });
  return { code: r.status, out: (r.stdout || '') + (r.stderr || '') };
}

const mutations = [
  {
    name: 'the topic briefs are gone',
    why: 'a merge that rejected every brief, or a page built before topics merged',
    apply: (html) => editBlock(html, 'topics', () => '{}'),
  },
  {
    name: 'the deep dives are gone',
    why: 'same failure one layer over',
    apply: (html) => editBlock(html, 'dives', () => '{}'),
  },
  {
    name: 'the ticket layer is gone',
    why: 'intent never ran, or its artefact was not embedded',
    apply: (html) => editBlock(html, 'tickets', () => '{}'),
  },
  {
    name: 'the file-to-ticket join matched nothing',
    why: 'the canonical miss: 0 of 70,655 joined on a real estate with the build green, '
      + 'both layers present, every count healthy',
    apply: (html) => editBlock(html, 'data', (text) => {
      const rows = JSON.parse(text);
      if (!rows.some((r) => (r[7] || []).length)) {
        throw new Error('no row carried ticket evidence to begin with');
      }
      for (const r of rows) r[7] = [];
      return JSON.stringify(rows);
    }),
  },
  {
    name: 'the graph index is empty',
    why: 'an explorer build from a graph that failed to load',
    apply: (html) => editBlock(html, 'data', () => '[]'),
  },
];

let failures = 0;
let caught = 0;

// The control. Without it a runner that fails everything would score 5 of 5.
const control = runGate(page);
if (control.code !== 0) {
  failures++;
  console.error('FAIL  control: the gate does not pass the unmutated page');
  console.error(control.out.split('\n').map((l) => `      ${l}`).join('\n'));
} else {
  console.log('ok    control: the unmutated page passes');
}

for (const m of mutations) {
  let mutatedPath;
  try {
    const mutated = m.apply(source);
    mutatedPath = join(work, `${m.name.replace(/\W+/g, '-')}.html`);
    writeFileSync(mutatedPath, mutated);
  } catch (e) {
    failures++;
    console.error(`FAIL  could not apply mutation "${m.name}": ${e.message}`);
    continue;
  }
  const { code, out } = runGate(mutatedPath);
  if (code === 0) {
    failures++;
    console.error(`FAIL  survived: ${m.name}`);
    console.error(`      ${m.why}`);
    console.error('      the gate passed a store this broken, so it is not checking it');
  } else {
    caught++;
    console.log(`ok    caught:   ${m.name}`);
  }
  void out;
}

// Counted, not derived from the failure total - which also carries the control and
// any mutation that could not be applied, so arithmetic over it reports a number
// that is not the one the sentence claims.
console.log(`\n${caught} of ${mutations.length} mutations caught.`);
if (failures) {
  console.error(`${failures} problem(s) - the answer gate is not protecting what it claims to`);
  process.exit(1);
}
process.exit(0);
