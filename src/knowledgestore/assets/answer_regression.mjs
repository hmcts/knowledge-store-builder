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
// **Every finding names the artefact it read.** Each of the four misses across
// both estates that motivated #134 was false testimony rather than silence -
// something was counted, and the number meant something other than it appeared
// to. None of them could have been written down naming its source.

import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { loadPage, strip } from './explorer_harness.mjs';

/** The answer shapes a question may declare. Names are the reader's, not internal. */
export const MODES = ['brief', 'dive', 'tickets', 'graph', 'ticket', 'abstain'];

/** Which embedded block each mode is evidence from, for the house rule above.
 * @type {Record<string, string>} */
const MODE_SOURCE = {
  brief: 'topics block (docs/topics -> briefs.json)',
  dive: 'dives block (docs/deep-dives -> dives.json)',
  tickets: 'tickets block (knowledge/intent)',
  graph: 'data + edges blocks (graphify-out/graph.json)',
  ticket: 'tickets block, by ticket id',
  abstain: 'no block answered',
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
    const line = raw.replace(/#.*$/, '').trim();
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
  const inTicketLayer = Boolean(ticketId) && Boolean(api.TICKET_INFO && api.TICKET_INFO[ticketId]);
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
    return {
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

  return { results, byMode, floors };
}

/** @param {string[]} a @param {string[]} b */
function sameSet(a, b) {
  return a.length === b.length && [...a].sort().join() === [...b].sort().join();
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

  const { questions, problems } = parseQuestions(readFileSync(args.questions, 'utf-8'));
  if (problems.length) {
    console.error(`FAIL  ${args.questions} could not be read as a question set:`);
    for (const p of problems) console.error(`      ${p}`);
    return 2;
  }
  if (!questions.length) {
    // A question set that parses to nothing would otherwise pass every assertion
    // below and report success - the emptiest possible false testimony.
    console.error(`FAIL  ${args.questions} declares no questions, so this gate would assert nothing`);
    return 2;
  }

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

  /** @type {Record<string, string[]>} */
  let previous = {};
  let baselineRegion = null;
  if (args.baseline && existsSync(args.baseline)) {
    const doc = JSON.parse(readFileSync(args.baseline, 'utf-8'));
    baselineRegion = doc;
    previous = (doc.estate && doc.estate.answers) || {};
  }

  const { results, byMode, floors } = run(api, questions, previous);

  if (args.write) {
    const doc = {
      // Two regions with different authority. The estate owns its answers and
      // may rewrite them; the library region records what the runner asserted
      // and is not the estate's to silence - a failure only the library can fix
      // must not be silenceable from here, and a failure the estate owns must
      // not block a library upgrade.
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
    writeFileSync(args.baseline, JSON.stringify(doc, null, 2) + '\n');
    console.log(`wrote baseline -> ${args.baseline}  (${results.length} questions)`);
  }

  if (args.json) {
    console.log(JSON.stringify({ results, byMode, floors }, null, 2));
  }

  let failures = 0;
  for (const r of results) {
    const source = r.modes.map((m) => MODE_SOURCE[m] || m).join('; ');
    if (!r.composed) {
      failures++;
      console.error(`FAIL  ${r.question}`);
      console.error(`      the engine threw while composing the answer`);
    } else if (!r.pass) {
      failures++;
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
    } else if (r.lostAnswer) {
      failures++;
      console.error(`FAIL  ${r.question}`);
      console.error(`      the baseline has this answered; it now abstains`);
    } else if (r.moved) {
      // A note, not a failure. Movement between two accepted shapes is what a
      // refresh legitimately does; a harness that fails on it gets switched off.
      console.log(`note  ${r.question}  ->  ${r.modes.join(' + ')} (was ${previous[r.question].join(' + ')})`);
    } else {
      console.log(`ok    ${r.question}  ->  ${r.modes.join(' + ')}`);
    }
  }

  for (const f of floors) {
    failures++;
    console.error(`FAIL  no question expecting "${f.mode}" passed, of ${f.declared} declared`);
    console.error(`      read from: ${MODE_SOURCE[f.mode] || f.mode}`);
    console.error('      a pass rate hides this: the other modes carry the total');
  }

  const passed = results.filter((r) => r.pass).length;
  const parts = Object.entries(byMode)
    .map(([m, c]) => `${m} ${c.passed}/${c.declared}`)
    .sort()
    .join(', ');
  console.log(`\n${passed} of ${results.length} questions answered as declared  (${parts})`);
  console.log(`page: ${args.page}`);
  return failures ? 1 : 0;
}

// Only when run directly, so the tests can import the pieces above.
if (process.argv[1] && process.argv[1].endsWith('answer_regression.mjs')) {
  process.exit(main(process.argv.slice(2)));
}
