// Does the answer gate's `graph` mode say what the reader is shown?
//
// The gate decided it with `if (ranked.length)` over what `rankNodes` returned,
// and `runAsk` - the path a reader drives - puts the ranking through two more
// gates before anything reaches the page (#326):
//
//   - `applySummaryBoost` ADDS rows for a community whose summary matches the
//     question's vocabulary, so a question with no bare-ranked rows can still be
//     rendered an answer;
//   - `reportUnevidenced` RETURNS before routing when every term is unevidenced,
//     so a ranking the boost pushed rows into can reach nobody.
//
// They fail in opposite directions, which is why this file drives both. Reading
// the boost alone swaps one misreport for another: the boost matches
// community-summary prose, which `unevidencedTerms` does not consult, so the
// questions it pushes rows for are exactly the ones the abstention can withhold.
//
// **The pair is the point.** A harness that says `graph` for everything and one
// that says it for the right questions both make the graph count go up, and the
// same is true one level down of the abstention guard. So a question of each
// shape is driven in the same run, the reader's own rendering is read separately
// from the harness's verdict for each, and the file ends by requiring the two to
// differ - which is what lets it report that it can no longer tell a
// boost-answered question from an abstained one.
//
// Every question below is invented against the fixture estate's dozen-word
// vocabulary. None is a real question from any store. Every expected value is a
// hand-written literal derived from the fixture's own artefacts - its twelve DATA
// rows, its four community labels and its one community summary - never from the
// code being checked.
//
// Run: python3 tests/explorer/fixture.py && node tests/explorer/answer-graph-mode.mjs

import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';

import { run, parseQuestions } from '../../src/knowledgestore/assets/answer_regression.mjs';
import { strip } from '../../src/knowledgestore/assets/explorer_harness.mjs';
// The page loading, the assertion recorder and the total live in one place, so
// two explorer harnesses cannot come to disagree about what a failure looks like.
import {
  equal, fail, conclude, fixtureApi, root, fixturePagePath as page, runnerPath as runner,
} from './harness.mjs';

const fixtureQuestions = join(root, 'tests', 'explorer', 'fixtures', 'questions.txt');
const api = fixtureApi();

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

/** What the READER is shown for one question, read from the shipped renderer.
 *
 * Deliberately not the harness's own verdict, and deliberately not a second
 * classifier over the output: the two markers below are the renderer's own
 * strings, one from `vWhichRepos` and one from `reportUnevidenced`, and they are
 * mutually exclusive by construction because the abstention returns before
 * routing. Asserting the pair is what makes "the reader got a graph answer" a
 * fact about the page rather than a restatement of what is being tested.
 *
 * @param {string} question
 * @returns {{composedFromGraph: boolean, abstained: boolean, text: string,
 *            bareRanked: number, boostedRanked: number}}
 */
function readerSees(question) {
  const terms = api.queryTerms(question);
  const expansions = api.expandTerms(terms);
  const bare = api.rankNodes(terms, expansions);
  /** @type {[number, number][]} */
  const boosted = bare.map((/** @type {[number, number]} */ r) => [r[0], r[1]]);
  api.applySummaryBoost(boosted, terms, expansions);
  api.out.innerHTML = '';
  api.meta.textContent = '';
  api.q.value = question;
  api.runAsk();
  const text = strip(String(api.out.innerHTML || ''));
  return {
    // `vWhichRepos`'s own answer line for this fixture: the graph route composed
    // an answer and rendered it.
    composedFromGraph: text.includes('PaymentService appears in 1 repositories'),
    // `reportUnevidenced`'s card, which it renders as the WHOLE answer.
    abstained: text.includes('No evidence in this estate'),
    text,
    bareRanked: bare.length,
    boostedRanked: boosted.length,
  };
}

// ---------------------------------------------------------------------------
// The divergence itself. `business` is only in the community label "Business
// Features: Payments", so `unevidencedTerms` finds it and the abstention does not
// fire; `flow`, `reach`, `both` and `application` are in no label and no source
// file, so `rankNodes` scores nothing. All four ARE in the community-3 summary,
// which is 8 hits, so the boost pushes community 3's highest-degree row -
// PaymentService - into an otherwise empty ranking and the reader is shown an
// answer composed from the graph.
//
// Catches (the reader half): the premise going stale. If the fixture's vocabulary
// or the boost's threshold moves so that the bare ranking is no longer empty, or
// the reader no longer gets a graph answer, every assertion below is about a
// question that no longer has the shape they were written for - and would pass
// for the wrong reason.
// Catches (the harness half): `if (ranked.length)` restored, or the mode decision
// moved back onto the pre-boost ranking. The harness records `abstain` while the
// store answers the question - a false negative that PASSES an `abstain`
// declaration, so nothing else in this suite can notice it.
// ---------------------------------------------------------------------------
const boostAnswers = 'which business flows reach both applications?';
const boostSeen = readerSees(boostAnswers);
equal('the reader is shown a graph-composed answer where the bare ranking is empty',
  { bareRanked: boostSeen.bareRanked, boostedRanked: boostSeen.boostedRanked,
    composedFromGraph: boostSeen.composedFromGraph, abstained: boostSeen.abstained },
  { bareRanked: 0, boostedRanked: 1, composedFromGraph: true, abstained: false },
  boostSeen.text);

const boostSet = report(`${boostAnswers} | graph\n`);
const boosted = forQuestion(boostSet.results, boostAnswers);
equal('the harness reports the graph layer answered, exactly as the reader was shown',
  { modes: boosted.modes, pass: boosted.pass },
  { modes: ['graph'], pass: true });

// ---------------------------------------------------------------------------
// The consequence, stated where a store will meet it. Catches: the change made
// without its cost being real - if this passes, the mode decision has not
// actually moved. A question declaring `abstain` on a question the store answers
// used to pass, and that is the false negative #326 is about.
// ---------------------------------------------------------------------------
const declaredAbstain = forQuestion(report(`${boostAnswers} | abstain\n`).results, boostAnswers);
equal('a question declaring abstain fails when the store answers it',
  { modes: declaredAbstain.modes, pass: declaredAbstain.pass },
  { modes: ['graph'], pass: false });

// ---------------------------------------------------------------------------
// The opposite direction, and the reason reading the boost alone is not the fix.
// Drop `business` and every term is unevidenced: the boost still pushes
// community 3's row, because a community summary is not part of the index
// `unevidencedTerms` consults - and `reportUnevidenced` renders its finding as the
// whole answer and returns, so the reader is shown no graph evidence at all.
//
// Catches: the mode decided on the boosted ranking alone. That reports `graph`
// here, which fails an `abstain` declaration on a question the engine really does
// abstain on - and sends someone hunting a layer the reader was told is absent.
// This is the over-correction the fix has to avoid, in its own right.
// ---------------------------------------------------------------------------
const engineAbstains = 'which flows reach both applications?';
const abstainSeen = readerSees(engineAbstains);
equal('the reader is shown the abstention even though the boost pushed a row',
  { bareRanked: abstainSeen.bareRanked, boostedRanked: abstainSeen.boostedRanked,
    composedFromGraph: abstainSeen.composedFromGraph, abstained: abstainSeen.abstained },
  { bareRanked: 0, boostedRanked: 1, composedFromGraph: false, abstained: true },
  abstainSeen.text);

const abstainSet = report(`${engineAbstains} | abstain\n`);
const abstained = forQuestion(abstainSet.results, engineAbstains);
equal('the harness abstains too, and does not claim the pushed row answered',
  { modes: abstained.modes, pass: abstained.pass },
  { modes: ['abstain'], pass: true });

// ---------------------------------------------------------------------------
// The over-correction guard, the other way round. Catches: a guard broad enough
// to withhold the mode from ordinary questions - most plausibly by requiring
// every term to be evidenced rather than requiring not every term to be
// unevidenced. Each question below has at least one unevidenced term or none at
// all, and each keeps the modes it had before the decision moved: the values are
// the fixture's own routes, and `answer-validity.mjs` pins the first of them
// independently as `brief, tickets, graph`.
// ---------------------------------------------------------------------------
const fixtureSet = report(
  'how are addresses formatted?                    | brief\n'
  + 'what does demo-core do?                         | dive\n'
  + 'which repositories implement PaymentService?    | graph\n'
  + 'what changed in DEMO-1?                         | ticket\n'
  + 'how is quantum entanglement configured?         | abstain\n',
);
equal('a question no boost decides keeps the modes it had, exactly',
  fixtureSet.results.map((r) => [r.question, r.modes]),
  [
    ['how are addresses formatted?', ['brief', 'tickets', 'graph']],
    ['what does demo-core do?', ['dive', 'graph']],
    ['which repositories implement PaymentService?', ['graph']],
    ['what changed in DEMO-1?', ['ticket', 'graph']],
    ['how is quantum entanglement configured?', ['abstain']],
  ]);
equal('and none of them is reported as decided differently',
  fixtureSet.graphMode, { total: 5, gained: 0, lost: 0 });

// ---------------------------------------------------------------------------
// #310's record, unchanged. Catches: the mode decision taken by boosting the
// caller's array in place. That is the obvious way to write this fix, and it
// corrupts two things at once - `rankingOf` would boost an already-boosted array,
// and the pre-boost count that measures the divergence would read the boosted
// one, so the run would print a confident zero. The six fields are pinned whole,
// against the same literal `answer-rank.mjs` records for this question.
// ---------------------------------------------------------------------------
const unboosted = forQuestion(fixtureSet.results, 'which repositories implement PaymentService?');
equal('the rank record for an unboosted question is untouched',
  unboosted.ranking,
  {
    evidence: 'code | demo-core | src/payment.service.ts | PaymentService',
    ranked: 1,
    shown: 1,
    top: 216.2122,
    runnerUp: 0,
    marginPct: 100,
  });
equal('the pre-boost count is still the pre-boost one, so the divergence is measurable',
  { graphOnBareRanking: boosted.graphOnBareRanking, readerRows: boosted.ranking.ranked },
  { graphOnBareRanking: false, readerRows: 1 });
// The boosted row's score, derived by hand from the fixture's one summary: four
// of the question's terms appear in it at two points each, and `applySummaryBoost`
// scores a pushed row at `hits * 40` and then boosts every row of its community,
// the pushed one now included - 8 * 40 twice.
equal('the boosted record is the ordering the renderer receives, scored as it scores it',
  { top: boosted.ranking.top, evidence: boosted.ranking.evidence },
  { top: 640, evidence: 'code | demo-core | src/payment.service.ts | PaymentService' });

// ---------------------------------------------------------------------------
// The rest drives the shipped CLI, because the exit code and the human output are
// the contract a store consumes.
// ---------------------------------------------------------------------------
const work = mkdtempSync(join(tmpdir(), 'ksb-answer-graph-mode-'));

/** @param {string} text */
function runGate(text) {
  const path = join(work, `${text.length}-questions.txt`);
  writeFileSync(path, text);
  const r = spawnSync(process.execPath, [runner, '--page', page, '--questions', path],
    { encoding: 'utf-8' });
  return { code: r.status, out: r.stdout || '', err: r.stderr || '' };
}

// ---------------------------------------------------------------------------
// Catches: the divergence count wired to the exit code. It is a fact about how a
// pre-#326 run misreported the set, not a failure - the modes themselves already
// carry the exit code, and failing a store twice for one fact would put it red on
// a correction it has no action for.
// ---------------------------------------------------------------------------
const gated = runGate(`${boostAnswers} | graph\n`);
equal('a question decided differently than the bare ranking does not fail the run',
  gated.code, 0, gated.out + gated.err);
equal('the divergence is on stdout, where a reader sees it',
  gated.out.includes('graph mode: 1 of 1 question(s) are decided differently by the ordering '
    + 'the reader receives than by the bare ranking'), true, gated.out);
equal('it says which direction, and names the artefact it read',
  gated.out.includes('1 gained the mode, 0 lost it; read from: data + edges blocks '
    + '(graphify-out/graph.json)'), true, gated.out);
equal('it names the question and both orderings, not just a count',
  gated.out.includes(`mode  ${boostAnswers}  ->  graph`)
    && gated.out.includes('the bare ranking was empty, the ordering the reader receives holds 1, '
      + 'and the engine routes them'), true, gated.out);

// ---------------------------------------------------------------------------
// Catches: the summary line printed only when something diverged. Silence would
// then have two meanings - nothing diverges, and nothing measured it - and a
// store reading a green run could not tell which it had. The fixture's own
// question set diverges on nothing, so this is the case that must still speak.
// ---------------------------------------------------------------------------
const quiet = spawnSync(process.execPath,
  [runner, '--page', page, '--questions', fixtureQuestions], { encoding: 'utf-8' });
const quietOut = quiet.stdout || '';
equal('the fixture question set still passes unchanged', quiet.status, 0,
  quietOut + (quiet.stderr || ''));
equal('a set that diverges on nothing says so rather than falling silent',
  quietOut.includes('graph mode: 0 of 5 question(s) are decided differently'), true, quietOut);
equal('and prints no per-question line when nothing diverged',
  /^mode {2}/m.test(quietOut), false, quietOut);

// ---------------------------------------------------------------------------
// This file's own discriminating power, in the same run. A decision that says
// `graph` for every question and one that says it for the right questions both
// leave the assertions above either all passing or all failing in a way that
// reads as one broken expectation. This says which of the two happened - and it
// is the only thing here that can report that the two shapes have become
// indistinguishable.
// ---------------------------------------------------------------------------
if (boosted.modes.includes('graph') === abstained.modes.includes('graph')) {
  fail('this check can no longer tell a boost-answered question from an abstained one',
    `the boost-answered question reported ${boosted.modes.join(' + ')} and the abstained one `
      + `${abstained.modes.join(' + ')}`,
    boosted.modes.includes('graph')
      ? 'both claim the graph layer answered, so the abstention gate is not being read'
      : 'neither does, so the boost is not being read and the mode is the bare ranking again');
} else if (boostSeen.composedFromGraph === abstainSeen.composedFromGraph) {
  fail('the two questions no longer differ in what the READER is shown',
    'both renderings agree, so the pair above asserts one case twice and the fixture '
      + 'no longer carries a question of each shape');
} else {
  console.log('ok    the graph mode discriminates: one boost-answered, one abstained, same run');
}

conclude('the graph mode is the reader\'s, in both directions');
