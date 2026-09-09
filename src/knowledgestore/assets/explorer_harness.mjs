// Load a built explorer page and expose the shipped engine's own API.
//
// This is the one place that knows how to get from `explorer.html` to callable
// scorer functions, and it ships with the library because two things need it:
// the library's page regression, and `check-answers`, which a store operator
// runs against their own store.
//
// It exists as a shared module for a specific reason. This codebase has been
// bitten four times by a second implementation of something the library already
// ships - whatever skips `merge-graphs` reimplements it and drifts (#115, #146),
// a store's combine step reimplemented collision handling, and a route check
// approximated the scorer it existed to protect (#134). Page loading is only
// mechanical, but a harness with its own private copy of the extraction rules is
// the same shape of mistake, so there is one copy and both callers use it.
//
// No dynamic code execution: app.js is `require`d as a plain module and hands
// its API back on a namespaced global.

import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));

/** Where app.js sits relative to this module. They ship side by side. */
export const APP_PATH = join(here, 'app.js');

/** The data blocks a page must carry for the engine to answer anything. */
export const REQUIRED_BLOCKS = [
  'data', 'edges', 'titles', 'summaries', 'synonyms', 'tickets', 'config', 'topics', 'dives',
];

/** Blocks the engine reads if they are there and does without if they are not.
 *
 * `dicts` carries the per-column dictionary tables for `data`. It is optional
 * rather than required for a reason a store meets directly: `check-answers` runs
 * against a store's own PUBLISHED page, which was built by whichever library
 * version built it, and a page built before interning carries no such block.
 * Demanding it would turn every such page into a load failure naming a block its
 * builder never wrote. An absent block leaves the rows already plain, which is
 * exactly what a page from before this format is.
 */
export const OPTIONAL_BLOCKS = ['dicts'];

/** The page formats this loader understands, as `#config`'s `pageFormat` (#332).
 *
 * The two sides of a published page are installed separately: a store commits
 * `explorer.html`, and another environment - a CI job, an operator's stale
 * virtualenv - runs `check-answers` against it with whatever library version it
 * happens to have. So the page states its format and the reader states what it
 * reads, and a reader meeting a format not in this list stops.
 *
 * 1 is every page built before data-block interning; 2 is the interned block.
 * Both are here because this loader genuinely reads both - the `#dicts` block is
 * optional above and `decodeRows` is the identity without it.
 *
 * Without this, a format-2 page read by a format-1 library failed on
 * `a.localeCompare is not a function` - a sort over a column that now held
 * integers. Loud, but incidental: on a path not reaching that sort first, the
 * integers would have been ranked as labels and the run would have reported a
 * plausible score instead of a failure. The number the build writes is
 * `PAGE_FORMAT` in build_explorer.py.
 */
export const READS_PAGE_FORMATS = [1, 2];

/** The format a page declares: its `pageFormat`, or 1 where it declares none.
 *
 * An unmarked page is format 1, NOT an error. Every page published before the
 * marker existed carries no `pageFormat`, and reading absence as a mismatch
 * would turn all of them into a refusal - the failure this check exists to
 * prevent, reintroduced by the check itself.
 *
 * A `#config` block that will not parse returns 1 rather than throwing here.
 * The engine parses the same block moments later and fails on it by name, and a
 * format refusal over a page whose config is simply broken would name the wrong
 * problem.
 *
 * @param {string|undefined} configText
 * @returns {unknown} whatever the page declared, unvalidated
 */
export function declaredPageFormat(configText) {
  if (!configText) return 1;
  try {
    const config = JSON.parse(configText);
    return config?.pageFormat ?? 1;
  } catch {
    return 1;
  }
}

/** Why this page cannot be read, or null when it can.
 *
 * A refusal rather than a warning, and the choice is the harness's to make:
 * this module is what `check-answers` loads a store's published page with, and
 * its whole purpose is to fail on a store that is broken. A warning here would
 * be printed above a full set of scores computed over indices treated as
 * labels, and a reader cannot tell that from a healthy run.
 *
 * The browser is deliberately not the same decision. A page inlines the app.js
 * that built it, so the engine a browser runs is always the engine of that
 * page's own format and cannot mismatch - which is why app.js carries no copy of
 * this check. A second copy of the format number, kept in step by hand, to guard
 * a case that cannot arise is a drift risk rather than a safety net.
 *
 * @param {string} pagePath
 * @param {string|undefined} configText
 * @returns {string|null}
 */
export function pageFormatRefusal(pagePath, configText) {
  const declared = declaredPageFormat(configText);
  if (READS_PAGE_FORMATS.includes(/** @type {any} */ (declared))) return null;
  return `page at ${pagePath} was built with page format ${JSON.stringify(declared)}; `
    + `this library reads page format ${READS_PAGE_FORMATS.join(' and ')}. Upgrade `
    + 'hmcts-knowledge-store-builder, or read the page with the version that built it.';
}

/** Pull the embedded JSON blocks out of a built page.
 *
 * Linear-time by splitting rather than a regex over a potentially very large
 * page. The build escapes `</` inside the data, so splitting on the close tag is
 * safe - but an opening `<script` can appear in the data itself (a commit body
 * quoting markup), so this takes the FIRST one in each part, which is the
 * block's own tag, never a later one from the data.
 *
 * @param {string} html
 * @returns {Record<string, string>}
 */
export function extractJsonBlocks(html) {
  /** @type {Record<string, string>} */
  const blocks = {};
  for (const part of html.split('</script>')) {
    const open = part.indexOf('<script');
    if (open < 0) continue;
    const tagEnd = part.indexOf('>', open);
    const idMatch = /id="(\w+)"/.exec(part.slice(open, tagEnd));
    if (idMatch) blocks[idMatch[1]] = part.slice(tagEnd + 1);
  }
  return blocks;
}

/** A DOM small enough for the engine to run against, and no smaller. */
function makeEl() {
  return {
    textContent: '', innerHTML: '', value: '', placeholder: '', style: {},
    /** @param {string} position @param {string} markup */
    insertAdjacentHTML(position, markup) {
      this.innerHTML = position === 'afterbegin' ? markup + this.innerHTML : this.innerHTML + markup;
    },
    checked: true,
    classList: { toggle() {} },
    addEventListener() {}, add() {},
  };
}

/** Visible text of rendered markup, tags removed without a parser.
 * @param {string} h
 */
export function strip(h) {
  let text = '';
  let inTag = false;
  for (const ch of h) {
    if (ch === '<') inTag = true;
    else if (ch === '>') { inTag = false; text += ' '; }
    else if (!inTag) text += ch;
  }
  return text.replace(/\s+/g, ' ').trim();
}

/** Load a page and return the engine API plus what was read to get it.
 *
 * @param {string} pagePath
 * @param {{ appPath?: string, requireVerbatim?: boolean }} [options]
 *   `requireVerbatim` asserts the page inlines the CURRENT app.js byte for byte.
 *   The library's own regression wants that - it is testing the shipped code. A
 *   store checking its published page does NOT: their page was built by whichever
 *   library version they built with, and demanding it match the installed one
 *   turns every version difference into a spurious answer failure.
 * @returns {{ api: any, blocks: Record<string, string>, verbatim: boolean }}
 */
export function loadPage(pagePath, options = {}) {
  const appPath = options.appPath || APP_PATH;
  const html = readFileSync(pagePath, 'utf-8');
  const appSource = readFileSync(appPath, 'utf-8');
  const verbatim = html.includes(appSource);
  if (options.requireVerbatim && !verbatim) {
    throw new Error(`page at ${pagePath} does not inline the current app.js`);
  }

  const blocks = extractJsonBlocks(html);
  // Before the missing-block check and before app.js is required, both
  // deliberately. A page from a future format may be missing a block this
  // version demands, or may crash the engine while it is being loaded, and
  // either would report a symptom over a cause an operator can act on. There is
  // nothing to do about a page this library cannot read except say so.
  const refusal = pageFormatRefusal(pagePath, blocks.config);
  if (refusal) throw new Error(refusal);
  const missing = REQUIRED_BLOCKS.filter((id) => !blocks[id]);
  if (missing.length) {
    throw new Error(`page at ${pagePath} is missing embedded JSON block(s): ${missing.join(', ')}`);
  }

  /** @type {Record<string, any>} */
  const elements = {};
  for (const id of REQUIRED_BLOCKS) {
    elements[id] = { textContent: blocks[id] };
  }
  // The optional blocks after the required ones, and only where the page has
  // them: a stub carrying `undefined` would parse to nothing useful, and the
  // engine's own fallback for an absent block is the behaviour being preserved.
  for (const id of OPTIONAL_BLOCKS) {
    if (blocks[id]) elements[id] = { textContent: blocks[id] };
  }

  // Cast once, deliberately: this DOM is as small as the engine can run against,
  // and typing it as a real Document would mean stubbing an interface nothing here
  // uses. app.js casts the same way where it hands its API back.
  /** @type {any} */ (globalThis).document = {
    /** @param {string} id */
    getElementById: (id) => (elements[id] ??= makeEl()),
    // The kind filters, which search mode reads before it will show anything. A
    // stub answering nothing leaves every kind unticked, so every entry is
    // filtered out and an assertion passes or fails for the wrong reason.
    /** @param {string} sel */
    querySelectorAll: (sel) => (sel === '.k'
      ? ['code', 'concept', 'feature', 'scenario', 'ticket']
        .map((value) => ({ value, checked: true, addEventListener() {} }))
      : []),
  };
  // The engine builds <option>s for the repository selector.
  /** @type {any} */ (globalThis).Option = function Option() {};

  const require = createRequire(import.meta.url);
  require(appPath);
  const api = /** @type {any} */ (globalThis).__explorerApi;
  if (!api) throw new Error(`app.js at ${appPath} exposed no __explorerApi`);
  return { api, blocks, verbatim };
}
