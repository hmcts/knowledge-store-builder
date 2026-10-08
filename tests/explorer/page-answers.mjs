// Every answer a page gives to a list of questions, as JSON on stdout.
//
// One page per process, deliberately. app.js is `require`d, and Node caches a
// module by its path, so a second page loaded in the same process would be
// answered by the first page's module - and a comparison of two pages would
// compare one page with itself and pass whatever the second one held.
//
// Both modes for every question, because they read the rows differently: search
// renders a full card per entry (every column a reader sees), ask composes
// answers from the columns each question shape reads. What is printed is the
// markup and the meta line exactly as the engine wrote them - nothing is
// stripped or normalised, so two pages agreeing here render identically.
//
// Run: node tests/explorer/page-answers.mjs <page> '<json array of questions>' [<app.js>]

import { loadPage } from '../../src/knowledgestore/assets/explorer_harness.mjs';

const [pagePath, questionsJson, appPath] = process.argv.slice(2);
if (!pagePath || !questionsJson) {
  console.error("usage: node page-answers.mjs <page> '<json array of questions>' [<app.js>]");
  process.exit(2);
}

// requireVerbatim off: a caller comparing an earlier page passes that page's own
// app.js, which is not the one in this checkout.
const { api, blocks } = loadPage(pagePath, { appPath, requireVerbatim: false });

/** @param {() => void} mode @param {string} question */
function answer(mode, question) {
  api.out.innerHTML = '';
  api.meta.textContent = '';
  api.q.value = question;
  mode();
  return { meta: String(api.meta.textContent), html: String(api.out.innerHTML) };
}

const questions = JSON.parse(questionsJson);
console.log(JSON.stringify({
  // The width the page's own block carries, so a caller can tell a page that
  // dropped a column from one that did not - without it, two pages agreeing
  // could be two full-width pages, and the comparison would assert nothing.
  rowWidth: (JSON.parse(blocks.data)[0] || []).length,
  // The rows every answer is read from, as the engine holds them after decoding.
  // Beside the answers because an answer only shows the columns its question
  // reads: a column restored to the wrong place can land where the questions
  // asked happen not to look, and the rows are where it shows regardless.
  rows: api.DATA,
  answers: questions.map((question) => ({
    question,
    ask: answer(api.runAsk, question),
    search: answer(api.runSearch, question),
  })),
}));
