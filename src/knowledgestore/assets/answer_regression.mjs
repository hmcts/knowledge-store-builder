// Does this store still answer the questions it exists to answer?
//
// Two store operators built this independently, neither knowing the other had
// (#134). The library owns the runner; the estate owns the questions - because a
// question like "what is crime case readiness?" means nothing on another estate,
// and generalising the questions makes every store fight the result.
//
// Usage (normally via `knowledgestore check-answers`):
//
//   node answer_regression.mjs --page PATH --questions PATH [--baseline PATH]
//                             [--write-baseline] [--json]
//
// ## What it asserts, and what it deliberately does not
//
// **Answer shapes, not answer text.** A harness pinning prose is red after every
// refresh that legitimately reworded something, and a harness that is always red
// is one nobody reads. So a question declares the KIND of answer it should get -
// `brief`, `dive`, `tickets`, `graph`, `ticket`, `abstain` - and any of several
// alternatives can be acceptable.
//
// **It drives the shipped scorer.** An earlier attempt at this approximated the
// router in Python with keyword overlap and had to be discarded: at one shared
// term "what is the data retention policy?" routed to tickets on the word "data";
// at two, a genuine graph question collapsed to nothing. The failures were in
// opposite directions, so it was not a tuning problem - it was a second
// implementation of routing. This calls `matchTopic`, `matchDive`,
// `ticketEvidence` and `rankNodes` from `app.js` itself, in the same order
// `runAsk` calls them, and then calls `runAsk` too so that "the answer composes
// at all" is asserted by the real thing rather than inferred.
//
// **The join-cardinality floor is not here.** `build_explorer` already asserts it
// at build time, per extraction layer, restricted to nodes where the join could
// have happened (#149) - and it uses `_origin`, which exists in the graph and not
// in the page. Reimplementing it against the page would be a second
// implementation of the check whose whole point is that it agrees with the build.
//
// What this runner owes that constraint instead is its own aggregate:
// **a pass rate is decomposed by expected mode, and each mode carries a zero
// floor.** A composite is a weighted average of parts that fail independently, so
// a healthy majority always masks a dead minority - 18 of 20 passing reads fine
// while every `graph` question in the set abstains. The floor needs no
// estate-shaped threshold: if a mode has questions declared and none of them
// pass, that is a finding whatever the total says.
//
// **The same argument, one level down: a question can be a valid probe or not**
// (#311). The mode floors stop a dead MODE hiding behind a healthy total. They do
// not stop a dead LAYER hiding behind a question that another layer happens to
// answer - because a question may declare several acceptable modes, and passes on
// any one of them. A question declaring `brief, graph` passes on `graph` alone
// with the topics block blanked out, and the run reports it as a pass without
// anything recording that the layer it was written for was never consulted. The
// sharp case is a question whose terms are all bare label matches: `rankNodes`
// finds those whatever state the semantic and intent layers are in.
//
// So each question also gets a verdict on whether it could have failed for the
// layer it was written for, and the set gets a report of which blocks it actually
// observes. **This is reported, not gated**, and that is a decision rather than an
// omission: a voided question is a finding about the question FILE, and failing
// the run on it would put a store red on its own probes rather than on its data -
// the "always red so nobody reads it" failure the mode design was chosen to avoid.
// The counts are in the human output and the JSON so the gating decision can be
// taken on real numbers.
//
// **Every finding names the artefact it read.** Each of the four misses across
// both estates that motivated #134 was false testimony rather than silence -
// something was counted, and the number meant something other than it appeared
// to. None of them could have been written down naming its source.

import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { loadPage, strip } from './explorer_harness.mjs';

/** The answer shapes a question may declare. Names are the reader's, not internal. */
export const MODES = ['brief', 'dive', 'tickets', 'graph', 'ticket', 'abstain'];

/** The embedded blocks a question set can observe, and the artefact behind each.
 *
 * Ordered, because the report prints them and two runs on the same inputs must be
 * byte-identical. `abstention` is not a block on the page: it is what the engine
 * does when no block answers, and it belongs here because "nothing observes
 * abstention" is the same finding as "nothing observes the dives block".
 * @type {Record<string, string>} */
export const BLOCK_SOURCE = {
  topics: 'topics block (docs/topics -> briefs.json)',
  dives: 'dives block (docs/deep-dives -> dives.json)',
  tickets: 'tickets block (knowledge/intent)',
  data: 'data + edges blocks (graphify-out/graph.json)',
  abstention: 'no block answered, which is the engine abstaining',
};

/** Which block each mode's evidence comes from.
 *
 * Coarser than the mode, and deliberately: `tickets` and `ticket` are two shapes
 * of answer read from the same block, so a question declaring both declares one
 * layer, not two. The validity verdict below counts blocks rather than modes for
 * exactly that reason.
 * @type {Record<string, string>} */
const MODE_BLOCK = {
  brief: 'topics',
  dive: 'dives',
  tickets: 'tickets',
  graph: 'data',
  ticket: 'tickets',
  abstain: 'abstention',
};

/** Which embedded block each mode is evidence from, for the house rule above.
 * Every value is `BLOCK_SOURCE`'s, optionally with a suffix - `ticket` adds how it
 * was read, because the id lookup is the assertion that mode exists for. That
 * invariant is what stops the two tables drifting apart, so it is asserted rather
 * than left as a convention: `answer-validity.mjs` pins both the concrete strings
 * and the derivation, because these strings reach the reader.
 * @type {Record<string, string>} */
export const MODE_SOURCE = {
  brief: BLOCK_SOURCE.topics,
  dive: BLOCK_SOURCE.dives,
  tickets: BLOCK_SOURCE.tickets,
  graph: BLOCK_SOURCE.data,
  ticket: `${BLOCK_SOURCE.tickets}, by ticket id`,
  abstain: BLOCK_SOURCE.abstention,
};

const TICKET_ID = /\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b/;

/** Parse an estate's question file.
 *
 * `question | mode[, mode...]`, `#` comments, blank lines ignored. Several modes
 * mean any of them is acceptable - which is how a question survives a refresh
 * that legitimately moves it from a graph traversal to a written brief.
 *
 * @param {string} text
 * @returns {{ questions: {question: string, accept: string[], line: number}[], problems: string[] }}
 */
export function parseQuestions(text) {
  /** @type {{question: string, accept: string[], line: number}[]} */
  const questions = [];
  /** @type {string[]} */
  const problems = [];
  text.split('\n').forEach((raw, i) => {
    // indexOf rather than /#.*$/: the regex was flagged for super-linear
    // backtracking, and a comment strip needs no pattern matching.
    const hash = raw.indexOf('#');
    const line = (hash < 0 ? raw : raw.slice(0, hash)).trim();
    if (!line) return;
    const bar = line.lastIndexOf('|');
    if (bar < 0) {
      problems.push(`line ${i + 1}: no "|" separating the question from its expected mode(s)`);
      return;
    }
    const question = line.slice(0, bar).trim();
    const accept = line.slice(bar + 1).split(',').map((m) => m.trim()).filter(Boolean);
    if (!question) { problems.push(`line ${i + 1}: empty question`); return; }
    if (!accept.length) { problems.push(`line ${i + 1}: no expected mode`); return; }
    const unknown = accept.filter((m) => !MODES.includes(m));
    if (unknown.length) {
      problems.push(`line ${i + 1}: unknown mode(s) ${unknown.join(', ')} - known: ${MODES.join(', ')}`);
      return;
    }
    questions.push({ question, accept, line: i + 1 });
  });
  return { questions, problems };
}

/** Classify one question by driving the shipped engine.
 *
 * The order below is `runAsk`'s own order, deliberately: a topic brief suppresses
 * the dive match and the no-evidence abstention, so classifying in a different
 * order would report a shape the reader would never be shown.
 *
 * @param {any} api the engine API from explorer_harness
 * @param {string} question
 * @returns {{ modes: string[], composed: boolean, meta: string, chars: number,
 *            carried: string[] }} `carried` names the terms the estate does have
 *            evidence for, which is what makes a failed `abstain` actionable.
 */
export function classify(api, question) {
  // Reset the rendered surfaces so a previous question cannot be read as this one's.
  api.out.innerHTML = '';
  api.meta.textContent = '';
  api.q.value = question;

  const idInQuestion = TICKET_ID.exec(question);
  const terms = api.queryTerms(question);
  const expansions = api.expandTerms(terms);
  const ranked = api.rankNodes(terms, expansions);
  const topic = api.matchTopic(question.toLowerCase(), expansions);
  const dive = topic ? null : api.matchDive(question.toLowerCase());
  const evidence = api.ticketEvidence(terms);
  // Which terms the estate has nothing for. The engine abstains only when EVERY
  // term is unevidenced, so this is what makes a failed `abstain` expectation
  // actionable rather than puzzling: it names the term that carried the answer.
  const unevidenced = api.unevidencedTerms ? api.unevidencedTerms(terms) : [];
  const carried = terms.filter((/** @type {string} */ t) => !unevidenced.includes(t));

  /** @type {string[]} */
  const modes = [];
  // A ticket id in the question is not evidence the store holds that ticket -
  // `TICKET_ID` matches any string of that shape, so claiming `ticket` from it
  // alone reported success with the entire tickets layer blanked out. Found by
  // mutation, which is the only way this class of miss surfaces: something was
  // counted, and it was not what the label said. `vTicket` takes its evidence
  // from exactly these two structures, so this asks the same question it does.
  const ticketId = idInQuestion ? idInQuestion[0] : '';
  const inTicketLayer = Boolean(ticketId) && Boolean(api.TICKET_INFO?.[ticketId]);
  /** @param {any[]} row */
  const rowNames = (row) => (row[7] || []).includes(ticketId) || row[0] === ticketId;
  // A row of kind `ticket` IS the ticket, so it is not evidence that the ticket
  // reaches anything - and counting it let the dead-join mutation pass. `vTicket`
  // draws the same line: it counts such a row as a hit but excludes it from the
  // entries it shows the reader (`DATA[i][4] !== 'ticket'`). The join is a code or
  // concept node whose files that ticket touched, so that is what is required.
  const reachesGraph = Boolean(ticketId)
    && (/** @type {any[][]} */ (api.DATA || [])).some((row) => row[4] !== 'ticket' && rowNames(row));
  // AND, not OR, and this took three attempts to get right.
  //
  // First it claimed `ticket` from the id appearing in the question, which is
  // true of any string of that shape. Then it accepted either source - and
  // blanking either one alone still passed, because the other carried it. The
  // second version reproduced, inside the runner built to catch it, the exact
  // trap this harness exists for: a composite of parts that fail independently,
  // where the healthy part masks the dead one.
  //
  // The conjunction is not a strictness preference. The file-to-ticket join IS
  // the conjunction - a ticket the store has a record of, reached from graph
  // entries - and its canonical failure was 0 of 70,655 joined with the build
  // green, every count healthy and the layer present on both sides. Either half
  // alone is exactly the testimony that failure gives.
  //
  // Consequence worth knowing before declaring this mode: a ticket whose files
  // were all filtered out of the index is real but unjoined, so it will not
  // satisfy `ticket`. Declare `tickets` for a question like that - which is a
  // different assertion, a text match over the commit layer, and one that
  // legitimately survives a dead join.
  if (inTicketLayer && reachesGraph) modes.push('ticket');
  if (topic) modes.push('brief');
  if (dive) modes.push('dive');
  if (evidence.length) modes.push('tickets');
  if (ranked.length) modes.push('graph');

  // Abstention is a real answer shape here, not a failure: "no evidence in this
  // estate" is a finding the engine is designed to give. It is only counted when
  // nothing else answered, matching what a reader would actually see.
  if (!modes.length) modes.push('abstain');

  // And now the real thing, so "does an answer still compose at all" is asserted
  // by the shipped path rather than inferred from its parts.
  let composed = true;
  try {
    api.runAsk();
  } catch {
    composed = false;
  }
  const rendered = strip(api.out.innerHTML || '');
  return {
    modes,
    composed,
    meta: String(api.meta.textContent || ''),
    chars: rendered.length,
    carried,
  };
}

/** @param {string[]} observed @param {string[]} accept */
const met = (observed, accept) => accept.some((m) => observed.includes(m));

/** The blocks these modes read from, deduplicated and in `BLOCK_SOURCE` order.
 * Sorted by the table rather than by first appearance, because the order a
 * question happens to list its modes in must not reach the output.
 * @param {string[]} modes @returns {string[]}
 */
const blocksOf = (modes) => Object.keys(BLOCK_SOURCE)
  .filter((block) => modes.some((m) => MODE_BLOCK[m] === block));

/** Is this question a probe that could have failed for the layer it declares?
 *
 * A question passes on ANY accepted mode, so one declaring two blocks passes
 * while either of them is dead: kill the topics block and `brief, graph` still
 * answers on `graph`. Such a question observes neither block - it is answerable
 * another way - and counting it as a pass reports coverage of a layer nothing
 * checked. So a passing question is VOID when it declares more than one block.
 *
 * Two shapes reach that verdict and the reader needs to be told which:
 *
 *   - more than one declared block answered, so none of them has to be alive;
 *   - only one did, so the question was satisfied by a layer it declared but was
 *     not written for, and the others were never consulted.
 *
 * A single-block declaration is never void: exactly one block carries it, and
 * that block's death fails the question. A failing question is not void either -
 * it failed, and a failure is not a coverage claim.
 *
 * @param {{accept: string[], modes: string[], pass: boolean}} result
 * @returns {{declaredBlocks: string[], carriers: string[], missingBlocks: string[],
 *            voided: boolean, voidReason: string}}
 */
export function probeVerdict(result) {
  const declaredBlocks = blocksOf(result.accept);
  // Only the accepted modes that actually answered: those are what carried the
  // pass, and killing anything else would leave the question passing.
  const carriers = result.pass
    ? blocksOf(result.accept.filter((m) => result.modes.includes(m)))
    : [];
  const missingBlocks = declaredBlocks.filter((b) => !carriers.includes(b));
  const voided = Boolean(result.pass) && declaredBlocks.length > 1;
  let voidReason = '';
  if (voided && carriers.length > 1) {
    voidReason = `${carriers.join(' and ')} each answered a declared mode, `
      + 'so none of them has to be alive for this to pass';
  } else if (voided) {
    voidReason = `only ${carriers.join(' and ')} answered; `
      + `${missingBlocks.join(', ')} did not, so nothing here observes `
      + `${missingBlocks.length > 1 ? 'them' : 'it'}`;
  }
  return { declaredBlocks, carriers, missingBlocks, voided, voidReason };
}

/** The question set's own validity: the rate with voids excluded, and which
 * blocks the set genuinely observes.
 *
 * The second half is the repository's rule that a gate must name what it covers,
 * applied to the question set instead of the code. A block with no observer is
 * something the mode floors cannot report: `byMode` is keyed on modes questions
 * DECLARE, so a block nothing declares has no entry to have a floor, and a block
 * whose only questions were voided has a floor that the void made vacuous.
 *
 * @param {any[]} results each carrying the fields `probeVerdict` returns
 */
export function assessValidity(results) {
  const voided = results.filter((r) => r.voided);
  const counted = results.length - voided.length;
  const passed = results.filter((r) => r.pass && !r.voided).length;
  /** @type {Record<string, {observed: number, declared: number}>} */
  const tally = {};
  for (const block of Object.keys(BLOCK_SOURCE)) tally[block] = { observed: 0, declared: 0 };
  for (const r of results) {
    for (const block of r.declaredBlocks) tally[block].declared++;
    if (r.voided) continue;
    for (const block of r.carriers) tally[block].observed++;
  }
  const blocks = Object.entries(tally)
    .map(([block, c]) => ({ block, source: BLOCK_SOURCE[block], ...c }));
  return {
    rate: {
      passed,
      counted,
      voided: voided.length,
      total: results.length,
      // Named so nobody has to re-derive it from two numbers that mean different
      // things: this is the rate over probes that could have failed, not over the
      // question file.
      percent: counted ? Math.round((passed / counted) * 100) : 0,
    },
    voided: voided.map((r) => ({
      question: r.question,
      line: r.line,
      accept: r.accept,
      modes: r.modes,
      declaredBlocks: r.declaredBlocks,
      carriers: r.carriers,
      missingBlocks: r.missingBlocks,
      carried: r.carried,
      voidReason: r.voidReason,
    })),
    blocks,
    unobserved: blocks.filter((b) => b.observed === 0),
  };
}

/** Run every question and return a report. Pure enough to test.
 *
 * @param {any} api
 * @param {{question: string, accept: string[], line: number}[] } questions
 * @param {Record<string, string[]>} previous baseline: question -> modes last seen
 */
export function run(api, questions, previous = {}) {
  const results = questions.map((q) => {
    const { modes, composed, meta, chars, carried } = classify(api, q.question);
    const wanted = met(modes, q.accept);
    const wasAnswered = previous[q.question] && !previous[q.question].includes('abstain');
    const nowAbstains = modes.includes('abstain') && modes.length === 1;
    const answered = {
      ...q,
      modes,
      composed,
      meta,
      chars,
      carried,
      pass: wanted && composed,
      // A question the baseline says was answered and that now abstains is the
      // regression this gate exists for, reported separately from a declared
      // expectation being unmet so the two are never confused.
      lostAnswer: Boolean(wasAnswered && nowAbstains),
      moved: Boolean(previous[q.question] && !sameSet(previous[q.question], modes)),
    };
    return { ...answered, ...probeVerdict(answered) };
  });

  // The decomposition. By expected mode, because modes are produced by different
  // layers that fail independently; NOT by repository, which was measured on this
  // shape and rejected - a working layer makes every repository non-zero and
  // masks the floor exactly where it matters.
  /** @type {Record<string, {declared: number, passed: number}>} */
  const byMode = {};
  for (const r of results) {
    for (const m of r.accept) {
      byMode[m] ??= { declared: 0, passed: 0 };
      byMode[m].declared++;
      if (r.pass) byMode[m].passed++;
    }
  }
  const floors = Object.entries(byMode)
    .filter(([, c]) => c.declared > 0 && c.passed === 0)
    .map(([mode, c]) => ({ mode, declared: c.declared }));

  // `byMode` above deliberately still counts a voided question's pass. It carries
  // the exit code through the floors, and the validity gate is report-only by
  // design (#311) - subtracting voids from it would gate on the question file
  // through the back door, which is the one thing that decision rules out.
  return { results, byMode, floors, validity: assessValidity(results) };
}

/** @param {string[]} a @param {string[]} b */
function sameSet(a, b) {
    const ordered = (/** @type {string[]} */ xs) => [...xs].sort((x, y) => x.localeCompare(y));
  return a.length === b.length && ordered(a).join() === ordered(b).join();
}

/** @param {string[]} argv */
export function parseArgs(argv) {
  /** @type {{page: string, questions: string, baseline: string, write: boolean, json: boolean}} */
  const args = { page: '', questions: '', baseline: '', write: false, json: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--page') args.page = argv[++i];
    else if (a === '--questions') args.questions = argv[++i];
    else if (a === '--baseline') args.baseline = argv[++i];
    else if (a === '--write-baseline') args.write = true;
    else if (a === '--json') args.json = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  return args;
}

/** Read and validate the question set. Null means it already reported why.
 * @param {string} path
 */
function loadQuestions(path) {
  const { questions, problems } = parseQuestions(readFileSync(path, 'utf-8'));
  if (problems.length) {
    console.error(`FAIL  ${path} could not be read as a question set:`);
    for (const p of problems) console.error(`      ${p}`);
    return null;
  }
  if (!questions.length) {
    // A question set that parses to nothing would otherwise pass every assertion
    // below and report success - the emptiest possible false testimony.
    console.error(`FAIL  ${path} declares no questions, so this gate would assert nothing`);
    return null;
  }
  return questions;
}

/** The modes last recorded for each question, from the estate's baseline region.
 * @param {string} path
 * @returns {Record<string, string[]>}
 */
function readBaseline(path) {
  if (!path || !existsSync(path)) return {};
  const doc = JSON.parse(readFileSync(path, 'utf-8'));
  return doc.estate?.answers || {};
}

/** Write the baseline's two regions.
 *
 * They carry different authority. The estate owns its answers and may rewrite
 * them; the library region records what the runner asserted and is not the
 * estate's to silence - a failure only the library can fix must not be
 * silenceable from here, and a failure the estate owns must not block a library
 * upgrade.
 *
 * @param {string} path @param {any[]} results
 */
function writeBaseline(path, results) {
  const doc = {
    library: {
      runner: 'answer_regression.mjs',
      modes: MODES,
      note: 'Derived from the library. Rewritten by --write-baseline; not an estate override.',
    },
    estate: {
      note: 'The estate owns this region. Review its diff like any other change.',
      answers: Object.fromEntries(results.map((r) => [r.question, r.modes])),
    },
  };
  writeFileSync(path, JSON.stringify(doc, null, 2) + '\n');
  console.log(`wrote baseline -> ${path}  (${results.length} questions)`);
}

/** Report one question. Returns 1 if it is a failure, 0 otherwise.
 * @param {any} r @param {Record<string, string[]>} previous
 */
function reportOne(r, previous) {
  const source = r.modes.map((/** @type {string} */ m) => MODE_SOURCE[m] || m).join('; ');
  if (!r.composed) {
    console.error(`FAIL  ${r.question}`);
    console.error('      the engine threw while composing the answer');
    return 1;
  }
  if (!r.pass) {
    console.error(`FAIL  ${r.question}`);
    console.error(`      expected ${r.accept.join(' or ')}, got ${r.modes.join(' + ')}`);
    console.error(`      read from: ${source}`);
    if (r.accept.includes('abstain') && r.carried.length) {
      // Measured on a real estate: "how is quantum chromodynamics configured
      // here?" answered, because `configured` expanded to settings/setup and
      // matched. The engine abstains only when EVERY term is unevidenced, so a
      // question meant to test abstention must contain no term the estate has.
      console.error(
        `      abstention needs EVERY term unevidenced; the estate has: ${r.carried.join(', ')}`,
      );
    }
    if (r.meta) console.error(`      engine said: ${r.meta.slice(0, 160)}`);
    return 1;
  }
  if (r.lostAnswer) {
    console.error(`FAIL  ${r.question}`);
    console.error('      the baseline has this answered; it now abstains');
    return 1;
  }
  if (r.voided) {
    // Not a failure, and stdout rather than stderr for that reason: the finding
    // is about the question file, and the exit code is deliberately not wired to
    // it (#311). It is reported before `moved` because a question that has moved
    // between two accepted shapes is exactly the question this voids, and the
    // reason it can move is the thing worth reading.
    console.log(`void  ${r.question}  ->  ${r.modes.join(' + ')}`);
    console.log(`      declared ${r.accept.join(' or ')}: ${r.voidReason}`);
    console.log('      excluded from the pass rate; it is not evidence about a layer');
    if (r.carried.length) {
      console.log(`      the terms that carried it: ${r.carried.join(', ')}`);
    }
    return 0;
  }
  if (r.moved) {
    // A note, not a failure. Movement between two accepted shapes is what a
    // refresh legitimately does; a harness that fails on it gets switched off.
    const was = previous[r.question].join(' + ');
    console.log(`note  ${r.question}  ->  ${r.modes.join(' + ')} (was ${was})`);
    return 0;
  }
  console.log(`ok    ${r.question}  ->  ${r.modes.join(' + ')}`);
  return 0;
}

/** The per-mode zero floors, which a pass rate cannot show.
 * @param {{mode: string, declared: number}[]} floors
 */
function reportFloors(floors) {
  for (const f of floors) {
    console.error(`FAIL  no question expecting "${f.mode}" passed, of ${f.declared} declared`);
    console.error(`      read from: ${MODE_SOURCE[f.mode] || f.mode}`);
    console.error('      a pass rate hides this: the other modes carry the total');
  }
  return floors.length;
}

/** Which blocks this question set actually observes.
 *
 * Report-only, and it returns nothing a caller could add to a failure count -
 * deliberately, so a later change cannot wire it to the exit code by accident.
 * The floors above cannot report this: `byMode` is keyed on the modes questions
 * declare, so a block nothing declares has no entry to carry a floor at all.
 *
 * @param {ReturnType<typeof assessValidity>} v
 */
function reportValidity(v) {
  const parts = v.blocks.map((b) => `${b.block} ${b.observed}`).join(', ');
  console.log(`blocks observed by a valid question: ${parts}`);
  for (const b of v.unobserved) {
    console.log(`warn  no valid question is carried by ${b.block}, so nothing here observes it`);
    console.log(`      read from: ${b.source}`);
    console.log(b.declared
      ? `      ${b.declared} question(s) declare it, and none carried it as a valid probe`
      : '      no question declares it, which no per-mode floor can report');
  }
}

/** @param {string[]} argv */
function main(argv) {
  let args;
  try {
    args = parseArgs(argv);
  } catch (e) {
    console.error(`FAIL  ${e instanceof Error ? e.message : String(e)}`);
    return 2;
  }
  if (!args.page || !args.questions) {
    console.error('usage: answer_regression.mjs --page PATH --questions PATH [--baseline PATH]');
    console.error('                             [--write-baseline] [--json]');
    return 2;
  }

  const questions = loadQuestions(args.questions);
  if (!questions) return 2;

  let api;
  try {
    // Not requireVerbatim: a store's published page was built by whichever
    // library version built it, and demanding it match the installed app.js
    // would turn every version difference into a spurious answer failure.
    ({ api } = loadPage(args.page));
  } catch (e) {
    console.error(`FAIL  ${e instanceof Error ? e.message : String(e)}`);
    return 2;
  }

  const previous = readBaseline(args.baseline);
  const { results, byMode, floors, validity } = run(api, questions, previous);

  if (args.write) writeBaseline(args.baseline, results);
  if (args.json) console.log(JSON.stringify({ results, byMode, floors, validity }, null, 2));

  let failures = 0;
  for (const r of results) failures += reportOne(r, previous);
  failures += reportFloors(floors);

  const parts = Object.entries(byMode)
    .map(([m, c]) => `${m} ${c.passed}/${c.declared}`)
    .sort((x, y) => x.localeCompare(y))
    .join(', ');
  // The denominator is the questions that could have failed for the layer they
  // declare, not the size of the question file: a voided probe contributes to
  // neither half of a rate people read as coverage. The clause naming how many
  // were voided appears only when some were, so a set of single-block questions
  // reads exactly as it did before this existed.
  const { passed, counted, voided, total } = validity.rate;
  const voidedClause = voided ? `, ${voided} of ${total} voided` : '';
  console.log(`\n${passed} of ${counted} questions answered as declared${voidedClause}  (${parts})`);
  reportValidity(validity);
  console.log(`page: ${args.page}`);
  return failures ? 1 : 0;
}

// Only when run directly, so the tests can import the pieces above.
if (process.argv[1]?.endsWith('answer_regression.mjs')) {
  process.exit(main(process.argv.slice(2)));
}
