// Did the answer get worse, or only survive?
//
// The answer gate decided its `graph` mode with `if (ranked.length)`, which
// asserts non-emptiness and not relevance (#310). The row a reader wants can
// slide from rank 1 to rank 40, or past the window the renderer shows, and the
// gate stays green for as long as `rankNodes` returns a single row - it only goes
// red at the cliff. Ranking quality degrades continuously, so a boolean over a
// ranked list can report none of it.
//
// There is no ground truth to measure rank against: `questions.txt` declares an
// expected MODE rather than an id, deliberately, because pinning ids makes a
// harness that is red after every legitimate refresh. So the baseline supplies
// the ground truth instead - the row that ranked first last build - and this
// pins the recording and the comparison built on that.
//
// **The pair is the point, twice over.** A comparison that reports every question
// looks identical to one that reports the right ones, and a comparison that
// reports none looks identical to a build with no regressions. So a fall and a
// hold are asserted in the same run, and the file ends by requiring the two to
// differ - which is what lets it say it can no longer tell a fall from a hold,
// rather than only passing or failing.
//
// Every question below is invented against the fixture estate's dozen-word
// vocabulary. None is a real question from any store. Every expected value is a
// hand-written literal: the ranks are derived from the fixture's own artefacts -
// its twelve DATA rows and its one community summary - and never from the code
// being checked.
//
// Run: python3 tests/explorer/fixture.py && node tests/explorer/answer-rank.mjs

import { writeFileSync, readFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';

import {
  MARGIN_DROP_POINTS, evidenceKey, rankFindings, run, parseQuestions,
} from '../../src/knowledgestore/assets/answer_regression.mjs';
// The page loading, the assertion recorder and the total live in one place, so
// two explorer harnesses cannot come to disagree about what a failure looks like.
import {
  equal, fail, conclude, fixtureApi, root, fixturePagePath as page, runnerPath as runner,
} from './harness.mjs';

const fixtureQuestions = join(root, 'tests', 'explorer', 'fixtures', 'questions.txt');
const api = fixtureApi();

/** Drive the real runner over an inline question set and baseline.
 * @param {string} text @param {Record<string, any>} previousRanking
 */
function report(text, previousRanking = {}) {
  const { questions, problems } = parseQuestions(text);
  if (problems.length) throw new Error(`the test's own question set is malformed: ${problems}`);
  return run(api, questions, {}, previousRanking);
}

// Every `previous` and `current` below is a COMPLETE record, exactly the six
// fields `rankingOf` builds - including `top` and `runnerUp`, which the correct
// comparison never reads. A partial stand-in makes an assertion vacuous against
// the defect it was written for: omit those two and a comparison of raw score
// differences computes NaN, reports nothing, and passes the test that exists to
// catch it. Measured, not reasoned about - the first version of the scale check
// below did exactly that.

/** @param {any[]} results @param {string} question */
const forQuestion = (results, question) => {
  const found = results.find((r) => r.question === question);
  if (!found) throw new Error(`no result for ${question}`);
  return found;
};

// The fixture's twelve DATA rows, read out of the built page rather than
// restated: these are the identities the assertions below are written against.
// Two of them share the label `AddressPipe`, which is why an identity cannot be
// the label.
const ADDRESS_PIPE_A = 'code | demo-app-a | src/pipes/address.pipe.ts | AddressPipe';
const ADDRESS_PIPE_B = 'code | demo-app-b | src/pipes/address.pipe.ts | AddressPipe';
const ADDRESS_ENTRY = 'code | demo-app-b | src/address/address-entry.component.ts'
  + ' | AddressEntryComponent';
const PAY_CONTAINER = 'code | demo-app-b | src/pay/pay.container.ts | PayContainer';

// ---------------------------------------------------------------------------
// Catches: an identity keyed on the label alone. The fixture holds `AddressPipe`
// twice, in demo-app-a and demo-app-b, and both rank for an address question - so
// a label key looks up the wrong row's rank and a genuine fall reads as rank 1.
// The page carries no node id, so this is the only thing that can distinguish
// them.
// ---------------------------------------------------------------------------
equal('two rows sharing a label are two identities',
  [evidenceKey(api.DATA[2]), evidenceKey(api.DATA[9])],
  [ADDRESS_PIPE_A, ADDRESS_PIPE_B]);

// ---------------------------------------------------------------------------
// Catches: a key that several structural rows share. Newer graphify emits
// package-hierarchy nodes with neither a label nor a source file, so a key built
// from what is left is shared - and a rank looked up under a shared key is the
// FIRST such row's rank, which is a different quantity from the one being
// claimed. `''` means "no identity", and nothing is compared for it.
// ---------------------------------------------------------------------------
equal('a row with neither label nor source file has no identity',
  evidenceKey(['', 'demo-deploy', '', 'Payments', 'concept', 1, [], [], 3, '']),
  '');

// ---------------------------------------------------------------------------
// A fall reported, and a hold not, in the same run.
//
// `how are addresses formatted?` ranks four rows in the fixture, all four tied:
// DATA rows 2, 6, 8 and 9 - AddressPipe (demo-app-a), AddressFormComponent,
// AddressEntryComponent, AddressPipe (demo-app-b) - in that order, because a tie
// keeps the page's own index order. So `AddressEntryComponent` is rank 3 of 4,
// and a baseline naming it as last build's top result is a fall from 1 to 3.
//
// Catches (the fall): non-emptiness left as the only signal, which is the whole
// of #310 - a row sliding from rank 1 to rank 3 reported as a clean pass.
// Catches (the hold): a comparison that reports whenever a baseline entry
// exists, which makes every question a finding and reads exactly like the check
// working.
// ---------------------------------------------------------------------------
const addresses = 'how are addresses formatted?';
const payments = 'which repositories implement PaymentService?';
const set = report(`${addresses} | brief\n${payments} | graph\n`, {
  [addresses]: { evidence: ADDRESS_ENTRY, ranked: 4, shown: 4, top: 31.2063, runnerUp: 31.2063, marginPct: 0 },
  [payments]: {
    evidence: 'code | demo-core | src/payment.service.ts | PaymentService',
    ranked: 1, shown: 1, top: 216.2122, runnerUp: 0, marginPct: 100,
  },
});
const fell = forQuestion(set.results, addresses);
const held = forQuestion(set.results, payments);

equal('a row that fell from rank 1 to rank 3 is reported, with the rank and the row',
  fell.rankDrift,
  [
    'the row that ranked first last build is now rank 3 of 4',
    `it is: ${ADDRESS_ENTRY}`,
    'its lead over the runner-up was 0%; the row now first leads by 0%',
  ]);

equal('a row still ranking first is not reported', held.rankDrift, []);

equal('the summary names the population it compared, not just the finding count',
  set.drift, { total: 2, compared: 2, regressed: 1 });

// ---------------------------------------------------------------------------
// Catches: a probe that no longer ranks at all read as "nothing to compare" and
// skipped. That is the cliff the old boolean could see, and it must still be a
// finding when the ranking is non-empty for a different row.
// ---------------------------------------------------------------------------
const gone = report(`${payments} | graph\n`, {
  [payments]: { evidence: PAY_CONTAINER, ranked: 1, shown: 1, top: 216.2122, runnerUp: 0, marginPct: 100 },
});
equal('a probe that no longer ranks at all is reported as such',
  forQuestion(gone.results, payments).rankDrift,
  [
    'the row that ranked first last build does not rank at all, of 1 that do',
    `it is: ${PAY_CONTAINER}`,
    'its lead over the runner-up was 100%; the row now first leads by 100%',
  ]);

// ---------------------------------------------------------------------------
// Catches: the record taken from `rankNodes` without the community-summary boost
// `runAsk` applies before it routes - an ordering no reader is ever shown, and so
// a different quantity from the one the finding claims.
//
// Derived from the fixture's one summary, which reads (lowercased) "the payment
// path in demo-core and both applications: paymentservice is called from the
// checkout containers in demo-app-a and demo-app-b, so a change to it reaches
// both user-facing flows." It is community 3. The question below contributes the
// terms payment, reach, checkout and container, all four of which the summary
// holds, so community 3 is boosted. `PayContainer` is in community 3 and
// `Card payment succeeds` is in community 4, so the boost lifts PayContainer past
// it: rank 4 on `rankNodes` alone, rank 3 in the ordering the renderer receives.
// ---------------------------------------------------------------------------
const routed = 'how does payment reach the checkout containers?';
const boosted = report(`${routed} | graph\n`, {
  [routed]: { evidence: PAY_CONTAINER, ranked: 5, shown: 5, top: 1, runnerUp: 1, marginPct: 0 },
});
equal('the rank recorded is the one the renderer receives, after the summary boost',
  forQuestion(boosted.results, routed).rankDrift[0],
  'the row that ranked first last build is now rank 3 of 5');

// ---------------------------------------------------------------------------
// Catches: an inequality comparison instead of a directional one, which fires on
// an improvement and reads exactly like the check working. A build that ranks the
// same row first, shows more of the ranking and leads by more has got better; a
// harness that calls that a regression is one nobody reads.
// ---------------------------------------------------------------------------
equal('a build that ranks better is not reported',
  rankFindings(
    { evidence: ADDRESS_PIPE_A, ranked: 4, shown: 4, top: 10, runnerUp: 8, marginPct: 20 },
    { evidence: ADDRESS_PIPE_A, ranked: 9, shown: 9, top: 50, runnerUp: 20, marginPct: 60 },
    1,
  ),
  []);

// ---------------------------------------------------------------------------
// Catches: comparing the raw score difference. Every score moves wholesale when
// the corpus grows and IDF shifts, so a raw margin reports questions after a
// refresh that changed nothing about ranking. Below, the ordering is identical
// and every score is a tenth of what it was: the raw margin has fallen from 20 to
// 2 - past `MARGIN_DROP_POINTS`, so a raw comparison reports it - and the top
// result's lead over its runner-up has not moved at all.
// ---------------------------------------------------------------------------
equal('scores a tenth of the size with the same ordering report nothing',
  rankFindings(
    { evidence: ADDRESS_PIPE_A, ranked: 4, shown: 4, top: 100, runnerUp: 80, marginPct: 20 },
    { evidence: ADDRESS_PIPE_A, ranked: 4, shown: 4, top: 10, runnerUp: 8, marginPct: 20 },
    1,
  ),
  []);

// ---------------------------------------------------------------------------
// Catches: `MARGIN_DROP_POINTS` ignored, so every wobble in the top result's lead
// is a finding; or the comparison inverted. The two calls differ only in the
// current lead, one either side of the bound, so the bound has to be real.
// ---------------------------------------------------------------------------
const wasLeading = {
  evidence: ADDRESS_PIPE_A, ranked: 4, shown: 4, top: 100, runnerUp: 20, marginPct: 80,
};
equal('a lead that narrowed past the bound is reported',
  rankFindings(wasLeading,
    { evidence: ADDRESS_PIPE_A, ranked: 4, shown: 4, top: 100, runnerUp: 30,
      marginPct: 80 - MARGIN_DROP_POINTS },
    1),
  ['the top result leads its runner-up by 70%, was 80% - 10 points narrower']);
equal('a lead that narrowed less than the bound is not reported',
  rankFindings(wasLeading,
    { evidence: ADDRESS_PIPE_A, ranked: 4, shown: 4, top: 100, runnerUp: 29,
      marginPct: 80 - MARGIN_DROP_POINTS + 1 },
    1),
  []);

// ---------------------------------------------------------------------------
// Catches: a record that keeps only rank. A build can put the right row first and
// still show the reader fewer results - the renderer takes a window off the
// ranking, and a window that shrank is a worse answer that rank alone reads as
// clean.
// ---------------------------------------------------------------------------
equal('a window that shrank is reported even though the top row held',
  rankFindings(
    { evidence: ADDRESS_PIPE_A, ranked: 9, shown: 9, top: 100, runnerUp: 80, marginPct: 20 },
    { evidence: ADDRESS_PIPE_A, ranked: 9, shown: 3, top: 100, runnerUp: 80, marginPct: 20 },
    1,
  ),
  ['3 ranked result(s) reach the reader, was 9']);

// ---------------------------------------------------------------------------
// Catches: the escalation dropped. "rank 12 of 400" reads as a mild move; that
// the renderer shows ten of them, so the reader is shown none of it, is the part
// that makes it actionable - and it is the case #310 names first.
// ---------------------------------------------------------------------------
equal('a probe past the window says the reader is not shown it',
  rankFindings(
    { evidence: ADDRESS_PIPE_A, ranked: 400, shown: 10, top: 100, runnerUp: 80, marginPct: 20 },
    { evidence: ADDRESS_PIPE_A, ranked: 400, shown: 10, top: 100, runnerUp: 80, marginPct: 20 },
    12,
  ),
  [
    'the row that ranked first last build is now rank 12 of 400',
    `it is: ${ADDRESS_PIPE_A}`,
    'the renderer shows 10, so a reader is not shown it at all',
    'its lead over the runner-up was 20%; the row now first leads by 20%',
  ]);

// ---------------------------------------------------------------------------
// Catches: `''` treated as a probe. An unidentifiable top row - a structural node
// with neither label nor source file - would then be looked for, found nowhere,
// and reported as fallen on every build. A finding on every such question is
// indistinguishable from the check working and would bury the real ones.
// ---------------------------------------------------------------------------
equal('a probe with no identity is compared against nothing',
  rankFindings({ evidence: '', ranked: 4, shown: 4, top: 0, runnerUp: 0, marginPct: 0 },
    { evidence: '', ranked: 4, shown: 4, top: 0, runnerUp: 0, marginPct: 0 }, 0),
  []);

// ---------------------------------------------------------------------------
// The rest drives the shipped CLI, because the exit code and the human output are
// the contract a store consumes.
// ---------------------------------------------------------------------------
const work = mkdtempSync(join(tmpdir(), 'ksb-answer-rank-'));
const baseline = join(work, 'baseline.json');

const written = spawnSync(
  process.execPath,
  [runner, '--page', page, '--questions', fixtureQuestions, '--baseline', baseline,
    '--write-baseline'],
  { encoding: 'utf-8' },
);
equal('--write-baseline succeeds on the fixture', written.status, 0,
  (written.stdout || '') + (written.stderr || ''));
const firstWrite = readFileSync(baseline, 'utf-8');

// ---------------------------------------------------------------------------
// Catches: a comparison result written into the baseline. The baseline is what
// the NEXT build measures against, so a rank or a finding recorded in it makes
// that measurement a function of the last comparison. The key set is pinned
// exactly, so an added field fails rather than passing unnoticed.
// ---------------------------------------------------------------------------
const doc = JSON.parse(firstWrite);
equal('the baseline records values only, no comparison result',
  Object.keys(doc.estate.ranking[payments]),
  ['evidence', 'ranked', 'shown', 'top', 'runnerUp', 'marginPct']);
equal('the baseline records what answered, not merely that something did',
  doc.estate.ranking[payments],
  {
    evidence: 'code | demo-core | src/payment.service.ts | PaymentService',
    ranked: 1,
    shown: 1,
    top: 216.2122,
    runnerUp: 0,
    marginPct: 100,
  });

// ---------------------------------------------------------------------------
// Catches: anything unordered reaching the record - a set, a dict keyed on
// unordered data, a sort with no explicit tiebreak. Determinism is a stated
// feature of this library's outputs, and the fixture's four-way tie on the
// address question is exactly where an unstable order would show.
// ---------------------------------------------------------------------------
const rewritten = spawnSync(
  process.execPath,
  [runner, '--page', page, '--questions', fixtureQuestions, '--baseline', baseline,
    '--write-baseline'],
  { encoding: 'utf-8' },
);
equal('two --write-baseline runs on the same inputs are byte-identical',
  readFileSync(baseline, 'utf-8') === firstWrite, true,
  (rewritten.stdout || '') + (rewritten.stderr || ''));

/** @param {string} baselinePath */
const jsonRun = (baselinePath) => spawnSync(
  process.execPath,
  [runner, '--page', page, '--questions', fixtureQuestions, '--baseline', baselinePath, '--json'],
  { encoding: 'utf-8' },
).stdout || '';

/** The ranking region of every result, in order, as the report emitted it.
 * @param {string} out */
function rankingRegion(out) {
  const parsed = JSON.parse(out.slice(0, out.indexOf('\n}\n') + 3));
  return JSON.stringify(parsed.results.map((/** @type {any} */ r) => r.ranking));
}
// Two separate subprocesses, named rather than compared inline: the two calls are
// textually identical, which reads - to a person and to a static analyser alike -
// as an expression compared with itself. Naming them also puts both regions in
// the failure output, where a diff is what a reader needs.
const firstJsonRun = rankingRegion(jsonRun(baseline));
const secondJsonRun = rankingRegion(jsonRun(baseline));
equal('two --json runs record the same ranking, byte for byte',
  secondJsonRun, firstJsonRun);

// ---------------------------------------------------------------------------
// Catches: a baseline written before this existed reported as a total regression.
// `estate.ranking` is absent from every baseline a store already holds, and an
// absent region read as "every probe is gone" would put every upgrading store red
// on its own baseline. It must report nothing, and must say that it compared
// nothing rather than falling silent - the two have different fixes.
// ---------------------------------------------------------------------------
const oldShape = join(work, 'old-baseline.json');
writeFileSync(oldShape, `${JSON.stringify({
  library: { runner: 'answer_regression.mjs' },
  estate: { answers: JSON.parse(firstWrite).estate.answers },
}, null, 2)}\n`);
const onOld = spawnSync(
  process.execPath, [runner, '--page', page, '--questions', fixtureQuestions,
    '--baseline', oldShape],
  { encoding: 'utf-8' },
);
const onOldOut = onOld.stdout || '';
equal('a baseline with no ranking region does not fail the run', onOld.status, 0,
  onOldOut + (onOld.stderr || ''));
equal('a baseline with no ranking region reports no question as regressed',
  /^rank {2}/m.test(onOldOut), false, onOldOut);
equal('a baseline with no ranking region says it compared nothing',
  onOldOut.includes('the baseline records no ranking for any of these 5 question(s)'),
  true, onOldOut);

// ---------------------------------------------------------------------------
// The design constraint, asserted end to end. Catches: the rank report wired to
// the exit code. Whether rank movement is stable enough to gate on is a
// measurement on a real refresh, and a harness that goes red on ordinary churn is
// one nobody reads - so a fall must still exit 0, and must land on the output a
// person reads rather than only in JSON.
// ---------------------------------------------------------------------------
const moved = join(work, 'moved-baseline.json');
const movedDoc = JSON.parse(firstWrite);
movedDoc.estate.ranking[addresses].evidence = ADDRESS_ENTRY;
writeFileSync(moved, `${JSON.stringify(movedDoc, null, 2)}\n`);
const onMoved = spawnSync(
  process.execPath,
  [runner, '--page', page, '--questions', fixtureQuestions, '--baseline', moved],
  { encoding: 'utf-8' },
);
const movedOut = onMoved.stdout || '';
equal('a rank regression does not fail the run', onMoved.status, 0,
  movedOut + (onMoved.stderr || ''));
equal('a rank regression is on stdout, where a reader sees it',
  movedOut.includes(`rank  ${addresses}`), true, movedOut);
equal('a rank regression names the artefact it was read from',
  movedOut.includes('read from: data + edges blocks (graphify-out/graph.json)'), true, movedOut);
equal('the run says the report is not gated, and why',
  movedOut.includes('reported, not gated'), true, movedOut);
equal('the summary counts the compared population, not the question file',
  movedOut.includes('rank drift: 1 of 4 compared question(s) rank worse than the baseline, '
    + 'of 5 in the set'), true, movedOut);

// ---------------------------------------------------------------------------
// This file's own discriminating power, in the same run. A comparison that
// reports nothing and one that reports everything both leave the assertions above
// either all passing or all failing in a way that reads as one broken
// expectation. This says which of the two happened.
// ---------------------------------------------------------------------------
if (Boolean(fell.rankDrift.length) === Boolean(held.rankDrift.length)) {
  fail('this check can no longer tell a fall from a hold',
    `the fallen row reported ${fell.rankDrift.length} finding(s) and the held one `
      + `${held.rankDrift.length}`,
    fell.rankDrift.length
      ? 'everything is being reported, which is not the same as reporting the right things'
      : 'nothing is being reported, so the comparison is decoration');
} else {
  console.log('ok    the rank comparison discriminates: one fell, one held, same run');
}

conclude('the rank baseline holds');
