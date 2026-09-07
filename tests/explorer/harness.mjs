// The shared scaffolding for the explorer test files: load the fixture page,
// record an assertion, report a total.
//
// Extracted because two files carried it verbatim (#310). A copied harness
// drifts, and the way it drifts is the dangerous one - an improvement to one
// copy's failure message leaves two gates disagreeing about what a failure looks
// like, and the weaker one is the last place anybody looks.
//
// It deliberately holds no assertions of its own. Everything here is scaffolding
// the test files drive; what each of them is protecting is stated in that file,
// next to the assertion protecting it.

import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { loadPage } from '../../src/knowledgestore/assets/explorer_harness.mjs';

const here = dirname(fileURLToPath(import.meta.url));

/** The repository root, from this file rather than from the caller's cwd. */
export const root = join(here, '..', '..');

/** The shipped answer gate, which several files drive as a subprocess. */
export const runnerPath = join(root, 'src', 'knowledgestore', 'assets', 'answer_regression.mjs');

/** The page `fixture.py` builds. Overridable, because CI builds it elsewhere. */
export const fixturePagePath = process.env.KSB_FIXTURE_PAGE
  || join(root, '.fixture-store', 'graphify-out', 'explorer.html');

let failures = 0;

/** @param {string} text */
const indented = (text) => text.split('\n').map((line) => `      ${line}`).join('\n');

/** The engine API from the built fixture page, or a clear exit saying how to
 * build one. A missing page is a setup mistake rather than a failing assertion,
 * so it does not run the file's checks against nothing and report a total.
 */
export function fixtureApi() {
  try {
    return loadPage(fixturePagePath).api;
  } catch (e) {
    console.error(`FAIL  no usable page at ${fixturePagePath}: `
      + `${e instanceof Error ? e.message : e}`);
    console.error('      run: python3 tests/explorer/fixture.py');
    return process.exit(1);
  }
}

/** Record one assertion by comparing what happened against what should have.
 *
 * Deliberately not a name and a verdict. A pre-computed boolean and a separately
 * written detail string are two expressions that can disagree, so a failure can
 * print evidence about a different check than the one that failed - and this
 * repository's own history is of correct code answering a neighbouring question.
 * Comparing the two values makes the complaint the difference itself, and it puts
 * every expected value in the test as a hand-written literal, which is where the
 * house rules want them.
 *
 * @param {string} name the break this catches, in the words a reader needs
 * @param {unknown} observed what the product did
 * @param {unknown} expected what it should have done, derived by hand
 * @param {string} context further output worth reading when this fails
 */
export function equal(name, observed, expected, context = '') {
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
  if (context) console.error(indented(context));
}

/** Record a failure that is not a value comparison - the discriminating-power
 * check at the end of each file, which reports which way the check broke.
 * @param {string} name @param {string[]} lines
 */
export function fail(name, ...lines) {
  failures++;
  console.error(`FAIL  ${name}`);
  for (const line of lines) console.error(`      ${line}`);
}

/** Print the total and exit. `held` is what to say when nothing failed - name
 * what the file protects, so a green run is a claim about something.
 * @param {string} held
 */
export function conclude(held) {
  // Not a nested template literal. The JavaScript analyser flags one and honours
  // no suppression comment, so the construct has to go rather than be annotated -
  // twice already in this directory's history.
  const summary = failures ? `${failures} failure(s)` : held;
  console.log(`\n${summary}`);
  process.exit(failures ? 1 : 0);
}
