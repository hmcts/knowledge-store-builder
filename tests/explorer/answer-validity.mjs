// Is each question in a set a probe that could have failed for the layer it
// declares - and does the set observe every block at all?
//
// The answer gate passes a question when ANY accepted mode is produced, so a
// question declaring `brief, graph` passes on `graph` with the topics block
// blanked out. Nothing recorded that, and the pass rate read as coverage of a
// layer nothing checked (#311). This drives the real runner against the real
// fixture page and pins the verdict.
//
// **The pair is the point.** A rule that voids everything looks identical to a
// rule that voids the right things: both make the void count go up. So the void
// cases and the control cases are asserted in the same run, and the file ends by
// requiring the two verdicts to differ - which is what lets it report that it can
// no longer tell a valid probe from a void one, rather than only passing or
// failing.
//
// Every question below is invented against the fixture estate's dozen-word
// vocabulary. None is a real question from any store.
//
// Run: python3 tests/explorer/fixture.py && node tests/explorer/answer-validity.mjs

import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

import { loadPage } from '../../src/knowledgestore/assets/explorer_harness.mjs';
import {
  MODES, MODE_SOURCE, BLOCK_SOURCE, run, parseQuestions, probeVerdict,
} from '../../src/knowledgestore/assets/answer_regression.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const runner = join(root, 'src', 'knowledgestore', 'assets', 'answer_regression.mjs');
const page = process.env.KSB_FIXTURE_PAGE
  || join(root, '.fixture-store', 'graphify-out', 'explorer.html');

let api;
try {
  ({ api } = loadPage(page));
} catch (e) {
  console.error(`FAIL  no usable page at ${page}: ${e instanceof Error ? e.message : e}`);
  console.error('      run: python3 tests/explorer/fixture.py');
  process.exit(1);
}

let failures = 0;
/** @param {string} name @param {boolean} condition @param {string} detail */
function assert(name, condition, detail = '') {
  if (condition) {
    console.log(`ok    ${name}`);
  } else {
    failures++;
    console.error(`FAIL  ${name}${detail ? ` - ${detail}` : ''}`);
  }
}

/** Drive the real runner over an inline question set.
 * @param {string} text @returns {ReturnType<typeof run>}
 */
function report(text) {
  const { questions, problems } = parseQuestions(text);
  if (problems.length) throw new Error(`the test's own question set is malformed: ${problems}`);
  return run(api, questions, {});
}

/** @param {any[]} results @param {string} question */
const forQuestion = (results, question) => {
  const found = results.find((r) => r.question === question);
  if (!found) throw new Error(`no result for ${question}`);
  return found;
};

// ---------------------------------------------------------------------------
// Catches: the void rule removed or inverted. A question declared for the topics
// layer, answered only out of the data block, would be counted as a pass - and
// the rate would report coverage of a layer that was never consulted, which is
// exactly what a blanked topics block looks like today.
// ---------------------------------------------------------------------------
const wrongLayer = 'which repositories implement PayContainer?';
const control = 'how are addresses formatted?';
const setA = report(`${wrongLayer} | brief, graph\n${control} | brief\n`);
const voidedA = forQuestion(setA.results, wrongLayer);
const controlA = forQuestion(setA.results, control);

assert('a question satisfied by a layer it was not written for is voided',
  voidedA.pass === true && voidedA.voided === true,
  JSON.stringify({ pass: voidedA.pass, voided: voidedA.voided, modes: voidedA.modes }));
assert('the void names the block that did not answer',
  voidedA.carriers.join() === 'data' && voidedA.missingBlocks.join() === 'topics',
  JSON.stringify({ carriers: voidedA.carriers, missing: voidedA.missingBlocks }));
assert('a voided question is excluded from both halves of the pass rate',
  setA.validity.rate.total === 2
  && setA.validity.rate.counted === 1
  && setA.validity.rate.passed === 1
  && setA.validity.rate.voided === 1,
  JSON.stringify(setA.validity.rate));
assert('the voided question is reported by name, not just counted',
  setA.validity.voided.length === 1 && setA.validity.voided[0].question === wrongLayer,
  JSON.stringify(setA.validity.voided.map((v) => v.question)));

// ---------------------------------------------------------------------------
// The over-correction guard. Catches: "void the wrong ones" broadened into "void
// everything" - most plausibly by voiding any question that produced a mode
// outside its accept list. This control question declares `brief`, is answered
// from the topics block, and ALSO produces `tickets` and `graph`; a rule keyed on
// unaccepted modes would void it, the rate would collapse, and the output would
// be indistinguishable from the gate working.
// ---------------------------------------------------------------------------
assert('a question satisfied by its intended layer is not voided',
  controlA.pass === true && controlA.voided === false,
  JSON.stringify({ modes: controlA.modes, voided: controlA.voided }));
assert('extra modes beyond the declaration do not void the question',
  controlA.modes.length > 1 && controlA.carriers.join() === 'topics',
  JSON.stringify({ modes: controlA.modes, carriers: controlA.carriers }));

// ---------------------------------------------------------------------------
// Catches: counting modes where the verdict must count blocks. `tickets` and
// `ticket` are two shapes of answer read from one block, so declaring both
// declares one layer - and that layer's death fails the question. A mode-keyed
// rule voids it, which would void a legitimate declaration and inflate the count
// the gating decision will be taken on.
// ---------------------------------------------------------------------------
const twoModesOneBlock = 'what changed in DEMO-1?';
const setB = report(`${twoModesOneBlock} | ticket, tickets\n`);
const sameBlock = forQuestion(setB.results, twoModesOneBlock);
assert('two modes reading the same block are one declared layer, not two',
  sameBlock.pass === true
  && sameBlock.voided === false
  && sameBlock.declaredBlocks.join() === 'tickets',
  JSON.stringify({ declared: sameBlock.declaredBlocks, voided: sameBlock.voided }));

// ---------------------------------------------------------------------------
// Catches: a failing question being voided. The void must not become a way for a
// genuine failure to leave the denominator - a multi-block question that answered
// with none of its declared modes is a miss, and it has to stay one.
// ---------------------------------------------------------------------------
const missed = 'what does demo-core do?';
const setC = report(`${missed} | brief, tickets\n`);
const failed = forQuestion(setC.results, missed);
assert('a failing question is not voided out of the rate',
  failed.pass === false
  && failed.voided === false
  && setC.validity.rate.counted === 1
  && setC.validity.rate.passed === 0,
  JSON.stringify({ pass: failed.pass, voided: failed.voided, rate: setC.validity.rate }));

// ---------------------------------------------------------------------------
// Catches: the block distribution dropped, or counting declarations instead of
// what carried an answer. Without it a set that observes two of five blocks
// reports a clean total and says nothing about the three it never touches -
// the drift the issue describes, where no single edit looks wrong.
// ---------------------------------------------------------------------------
const setD = report(`which repositories implement PaymentService? | graph\n${control} | brief\n`);
const observedD = Object.fromEntries(setD.validity.blocks.map((b) => [b.block, b.observed]));
assert('the distribution counts the block that carried each valid question',
  observedD.topics === 1 && observedD.data === 1,
  JSON.stringify(observedD));
assert('the report names every block no question observes',
  setD.validity.unobserved.map((b) => b.block).join() === 'dives,tickets,abstention',
  JSON.stringify(setD.validity.unobserved.map((b) => b.block)));
assert('an unobserved block names the artefact nothing read',
  setD.validity.unobserved.every((b) => b.source.length > 0)
  && setD.validity.unobserved.some((b) => b.source.includes('docs/deep-dives')),
  JSON.stringify(setD.validity.unobserved.map((b) => b.source)));
assert('an unobserved block nothing declares is distinguished from one that failed',
  setD.validity.unobserved.every((b) => b.declared === 0),
  JSON.stringify(setD.validity.unobserved.map((b) => [b.block, b.declared])));

// ---------------------------------------------------------------------------
// The sharpest one. Catches: crediting a voided question's carrier to the block,
// or deriving the distribution from `byMode`. Here two questions declare `brief`,
// both pass, and the mode floor reads a healthy 2 of 2 - while nothing in the set
// observes the topics block, because either declared block dying leaves the other
// to answer. A healthy floor over an unobserved block is the false testimony this
// whole gate exists for.
// ---------------------------------------------------------------------------
const setE = report(`${control} | brief, graph\nhow is the postcode brief written? | brief, graph\n`);
const observedE = Object.fromEntries(setE.validity.blocks.map((b) => [b.block, b.observed]));
assert('the per-mode floor can read healthy while the block has no observer',
  setE.byMode.brief.declared === 2 && setE.byMode.brief.passed === 2 && observedE.topics === 0,
  JSON.stringify({ brief: setE.byMode.brief, topics: observedE.topics }));
assert('a block whose only questions were voided is reported as declared but unobserved',
  setE.validity.unobserved.some((b) => b.block === 'topics' && b.declared === 2),
  JSON.stringify(setE.validity.unobserved.map((b) => [b.block, b.declared])));
assert('a set of voided questions has an empty pass rate rather than a full one',
  setE.validity.rate.counted === 0
  && setE.validity.rate.voided === 2
  && setE.validity.rate.percent === 0,
  JSON.stringify(setE.validity.rate));

// ---------------------------------------------------------------------------
// Catches: a mode added to MODES without a block. It would map to `undefined`,
// contribute to no block, and become invisible to both halves of this gate -
// incidental coverage that cannot self-report, which is the failure the house
// rule about naming what a gate covers exists for.
// ---------------------------------------------------------------------------
const unmapped = MODES.filter(
  (m) => probeVerdict({ accept: [m], modes: [m], pass: true }).declaredBlocks.length !== 1,
);
assert('every declarable mode maps to exactly one block', unmapped.length === 0,
  `unmapped: ${unmapped.join(', ')}`);

// ---------------------------------------------------------------------------
// These strings reach the reader: every finding prints `read from: <source>`, so
// each is user-visible output. Nothing pinned them, and one changed under this
// change - `ticket` gained `(knowledge/intent)` when the two tables were derived
// from one. Catches: any edit to BLOCK_SOURCE or MODE_SOURCE silently moving what
// an operator reads. Written out by hand, not composed from BLOCK_SOURCE, because
// re-evaluating the table under test would pass under any edit to it.
// ---------------------------------------------------------------------------
const EXPECTED_MODE_SOURCE = {
  brief: 'topics block (docs/topics -> briefs.json)',
  dive: 'dives block (docs/deep-dives -> dives.json)',
  tickets: 'tickets block (knowledge/intent)',
  graph: 'data + edges blocks (graphify-out/graph.json)',
  ticket: 'tickets block (knowledge/intent), by ticket id',
  abstain: 'no block answered, which is the engine abstaining',
};
const sourceDrift = MODES
  .filter((m) => MODE_SOURCE[m] !== EXPECTED_MODE_SOURCE[m])
  .map((m) => `${m}: ${JSON.stringify(MODE_SOURCE[m])}`);
assert('every mode names the artefact it reads, in the words the reader sees',
  sourceDrift.length === 0
  && Object.keys(MODE_SOURCE).length === MODES.length,
  sourceDrift.length ? `drifted - ${sourceDrift.join('; ')}`
    : `MODE_SOURCE has ${Object.keys(MODE_SOURCE).length} keys for ${MODES.length} modes`);

// ---------------------------------------------------------------------------
// Catches: the two tables drifting apart. `MODE_SOURCE` is derived from
// `BLOCK_SOURCE` so a block's artefact is named in one place - but a value
// hand-written back into `MODE_SOURCE` looks identical until someone edits
// `BLOCK_SOURCE`, at which point the block report and the per-question report
// name different artefacts for the same block. The literal pin above cannot see
// that: it agrees with a hand-written copy of the right string. This is the
// invariant that change claimed, so it is checked rather than asserted in prose.
// ---------------------------------------------------------------------------
const blockSources = Object.values(BLOCK_SOURCE);
const underived = MODES.filter((m) => !blockSources.some(
  (source) => MODE_SOURCE[m] === source || MODE_SOURCE[m].startsWith(`${source},`),
));
assert('every mode source is a block source, or one with a suffix',
  underived.length === 0,
  underived.map((m) => `${m}: ${JSON.stringify(MODE_SOURCE[m])}`).join('; '));

// ---------------------------------------------------------------------------
// The design constraint, asserted end to end. Catches: the void wired to the exit
// code. Report-only is deliberate - a store going red on its own question file
// rather than on its data is the "always red so nobody reads it" failure the mode
// design was chosen to avoid - so a run whose every question is voided must still
// exit 0, and must say so in the output a person reads.
// ---------------------------------------------------------------------------
const work = mkdtempSync(join(tmpdir(), 'ksb-answer-validity-'));
const questionFile = join(work, 'questions.txt');
writeFileSync(questionFile, `${wrongLayer} | brief, graph\n${control} | brief\n`);
const cli = spawnSync(process.execPath, [runner, '--page', page, '--questions', questionFile],
  { encoding: 'utf-8' });
const output = (cli.stdout || '') + (cli.stderr || '');

assert('a voided question does not fail the run', cli.status === 0, output);
assert('the human output marks the voided question', /^void {2}/m.test(cli.stdout || ''),
  cli.stdout || '');
assert('the rate line says how many were voided',
  (cli.stdout || '').includes('1 of 1 questions answered as declared, 1 of 2 voided'),
  cli.stdout || '');
assert('the human output reports the block distribution',
  (cli.stdout || '').includes('blocks observed by a valid question:'),
  cli.stdout || '');

const asJson = spawnSync(
  process.execPath, [runner, '--page', page, '--questions', questionFile, '--json'],
  { encoding: 'utf-8' },
);
let parsed;
try {
  // The JSON is the first value printed, before the human report.
  parsed = JSON.parse((asJson.stdout || '').slice(0, (asJson.stdout || '').indexOf('\n}\n') + 3));
} catch (e) {
  parsed = null;
  console.error(`      could not parse --json output: ${e instanceof Error ? e.message : e}`);
}
assert('the JSON output carries the validity region for consumers',
  Boolean(parsed?.validity?.rate)
  && parsed.validity.rate.voided === 1
  && parsed.validity.voided[0].question === wrongLayer
  && Array.isArray(parsed.validity.blocks),
  JSON.stringify(parsed?.validity?.rate));

// ---------------------------------------------------------------------------
// The file's own discriminating power, in the same run. A rule that voids nothing
// and a rule that voids everything both leave every assertion above either all
// passing or all failing in a way that reads as one broken expectation. This says
// which of the two happened, in the words a reader needs.
// ---------------------------------------------------------------------------
if (voidedA.voided === controlA.voided) {
  failures++;
  console.error('FAIL  this check can no longer tell a void probe from a valid one');
  console.error(`      both verdicts came back ${voidedA.voided}`);
  console.error(voidedA.voided
    ? '      everything is being voided, which is not the same as voiding the right things'
    : '      nothing is being voided, so the gate is decoration');
} else {
  console.log('ok    the void verdict discriminates: one voided, one not, same run');
}

console.log(`\n${failures ? `${failures} failure(s)` : 'the question-set validity gate holds'}`);
process.exit(failures ? 1 : 0);
