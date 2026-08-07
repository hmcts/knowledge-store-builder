// @ts-check
/**
 * Crime UI Estate Explorer - page application.
 *
 * Inlined into graphify-out/explorer.html by scripts/build_explorer.py.
 * The page embeds JSON blocks this script reads at startup:
 *   #data      - Entry[] (see typedef below), sorted by degree descending
 *   #edges     - flat [source, target, source, target, ...] index pairs
 *   #titles    - Jira titles (offline CSV import), #tickets - commit-mined detail
 *   #summaries - community summaries, #synonyms - semantic token neighbours
 *
 * Ask-mode ranking is a port of graphify's own query scorer (serve.py):
 * IDF-weighted terms, exact/prefix/substring/source tiers, full-query
 * bonus, squared term-coverage scaling. Deterministic - no AI.
 *
 * Type-check (optional, needs Node): npx tsc --checkJs --noEmit scripts/explorer/app.js
 * Regression test (after a build):  node scripts/explorer/explorer-regression.mjs
 */

/**
 * One searchable graph entry:
 * [label, repo, sourceFile, communityLabel, kind, degree, connections, tickets, communityId]
 * @typedef {[string, string, string, string, string, number, string[], string[], number]} Entry
 */

/**
 * A ticket's record in the committed ticket artefact: `d` the curated
 * description, `s` the commit subjects as written, `b` the body prose. `s` and
 * `b` are absent when the commits offered no such evidence.
 * `t` and `x` come from the tracker rather than the commits: the ticket's own
 * title, and an opening extract of its description. Both are absent for a ticket
 * the tracker has never been asked about, or one it reported as absent.
 * @typedef {{d: string[], s?: string[], b?: string[], t?: string, x?: string, c?: string[],
 *            first: string, last: string, repos: string[], n: number}} TicketInfo
 */

/** @type {Record<string, string>} */
const TITLES = JSON.parse(getEl('titles').textContent || '{}');
/** Build-time (GraphRAG-style) community summaries, keyed by community id.
 * @type {Record<string, string>} */
const SUMMARIES = JSON.parse(getEl('summaries').textContent || '{}');
/** Build-time semantic token neighbours (MiniLM cosine, computed offline).
 * @type {Record<string, [string, number][]>} */
const SYN = JSON.parse(getEl('synonyms').textContent || '{}');
/** Per-ticket detail mined from commit messages.
 * @type {Record<string, TicketInfo>} */
const TICKET_INFO = JSON.parse(getEl('tickets').textContent || '{}');
/** @type {Entry[]} */
const DATA = JSON.parse(getEl('data').textContent || '[]');
/** @type {number[]} */
const EDGE_FLAT = JSON.parse(getEl('edges').textContent || '[]');
const N = DATA.length;
const labelLower = DATA.map((e) => e[0].toLowerCase());
const hay = DATA.map(
  (e) => (e[0] + ' ' + e[1] + ' ' + e[2] + ' ' + e[3] + ' ' + e[7].join(' ')).toLowerCase()
);
/** Ticket ids in a stable order, with one lowercased evidence haystack each -
 * the ticket's description, its commit subjects and its body prose together.
 *
 * Indexed per ticket, and deliberately not folded into `hay`: `hay` is built per
 * indexed entry, and the same ticket is carried by many entries, so evidence
 * inlined there is multiplied by every entry citing it - measured at hundreds of
 * megabytes of haystack in the browser on a real estate. One index over tickets
 * is a couple of megabytes of text, and scanning it linearly is the same order
 * of work the page already does for every question.
 * @type {string[]} */
const TICKET_IDS = Object.keys(TICKET_INFO).sort(cmpId);
/** @type {string[]} */
const ticketHay = TICKET_IDS.map((t) => {
  const info = TICKET_INFO[t];
  // `t` and `x` are the tracker's own title and description extract. Without
  // them here a real title would display and be unfindable - the page would
  // show better evidence than it could search.
  return (info.d || []).concat(info.s || [], info.b || [],
    info.t ? [info.t] : [], info.x ? [info.x] : [], info.c || []).join('\n').toLowerCase();
});
/** One ticket's evidence runs from a six-word subject to four thousand
 * characters of body prose, and a long text matches ordinary words by sheer
 * surface area. Scoring needs the mean to normalise against. */
const ticketLen = ticketHay.map((text) => text.length);
const avgTicketLen = ticketLen.reduce((a, b) => a + b, 0) / (ticketLen.length || 1);
/** @type {number[][]} */
const ADJ = DATA.map(() => []);
for (let i = 0; i < EDGE_FLAT.length; i += 2) {
  ADJ[EDGE_FLAT[i]].push(EDGE_FLAT[i + 1]);
  ADJ[EDGE_FLAT[i + 1]].push(EDGE_FLAT[i]);
}
/** community id -> its highest-degree entry index (DATA is degree-sorted) */
const communityFirst = /** @type {Record<number, number>} */ ({});
for (let i = 0; i < N; i++) {
  const c = DATA[i][8];
  if (c >= 0 && !(c in communityFirst)) communityFirst[c] = i;
}
/** @type {[number, string][]} community id + lowercased summary text */
const SUMMARY_LIST = Object.entries(SUMMARIES).map(([cid, s]) => [Number(cid), s.toLowerCase()]);
const STOP = new Set(
  ('a an and any are as at be but by can could do does did for from how i in is it its'
   + ' me my of on or our shall should show so tell that the their them there these this those to us we'
   + ' what when where which who whose why will with would you your'
   + ' exist exists existed thing things way ways going want wants').split(' ')
);

/** @param {string} id @returns {HTMLElement} */
function getEl(id) {
  const el = document.getElementById(id);
  if (!el) throw new Error('missing element: ' + id);
  return el;
}
const q = /** @type {HTMLInputElement} */ (getEl('q'));
const out = getEl('out');
const meta = getEl('meta');
const repoSel = /** @type {HTMLSelectElement} */ (getEl('repo'));
/** @type {'search'|'ask'} */
let mode = 'search';

[...new Set(DATA.map((e) => e[1]).filter(Boolean))]
  .sort((a, b) => a.localeCompare(b))
  .forEach((r) => repoSel.add(new Option(r, r)));

/** HTML meta-characters, plus the newline where line breaks are being kept.
 * @type {Record<string, string>} */
const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', '\n': '<br>' };
/** @param {string} s */
const esc = (s) => s.replace(/[&<>"]/g, (c) => ESCAPES[c] || c);
/** @param {string} s @returns {string[]} */
const tok = (s) => s.toLowerCase().match(/[a-z0-9]+/g) || [];

/** Append value to the list stored under key, creating the list on first use.
 * @template K, V
 * @param {Map<K, V[]>} map @param {K} key @param {V} value */
function pushGroup(map, key, value) {
  const list = map.get(key);
  if (list) {
    list.push(value);
  } else {
    map.set(key, [value]);
  }
}

/** Best available business detail for a ticket: Jira title if imported,
 * otherwise the description mined from commit messages.
 * @param {string} t @returns {string} */
function ticketDetail(t) {
  // Tracker first: fetched from the tracker itself, so fresher than a CSV
  // somebody exported by hand, and both beat a commit subject.
  return TICKET_INFO[t]?.t || TITLES[t] || TICKET_INFO[t]?.d?.[0] || '';
}

/** Date-range suffix for a ticket, marking commit-message provenance when
 * no real Jira title exists.
 * @param {TicketInfo|undefined} info @param {boolean} hasTitle */
function ticketDates(info, hasTitle) {
  if (!info) return '';
  let range = esc(info.first);
  if (info.last !== info.first) range += ' → ' + esc(info.last);
  const source = hasTitle ? '' : ', from commit messages';
  return ' <span class="tdates">(' + range + source + ')</span>';
}

/** Page configuration embedded at build time (see build_explorer.py;
 * override with env vars, e.g. JIRA_BROWSE_URL for other Jira instances).
 * @type {{jiraBrowseUrl?: string, briefRequestUrl?: string}} */
const CONFIG = JSON.parse(getEl('config').textContent || '{}');
const TICKET_BROWSE_URL = CONFIG.jiraBrowseUrl || '';

/** Topic briefs (GraphRAG phase 3): pre-written narratives composed at
 * build time from graph evidence, keyed by slug with matching keywords.
 * @type {Record<string, {title: string, keywords: string[], html: string, source: string}>} */
const TOPICS = JSON.parse(getEl('topics').textContent || '{}');

/** Deep dives: build-time dossiers on individual repositories, keyed by
 * repository name, served when a question names the repository.
 * @type {Record<string, {title: string, html: string, source: string, sha: string}>} */
const DIVES = JSON.parse(getEl('dives').textContent || '{}');

/** A ticket id as a link to the real Jira ticket.
 * @param {string} t */
function ticketLink(t) {
  if (!TICKET_BROWSE_URL) return esc(t);   // no tracker configured: plain text
  // The tracker URL is estate configuration, read from the page's embedded
  // config block, so it is escaped like any other embedded text before it goes
  // into an attribute: a quote in it would otherwise close the attribute and
  // whatever followed would be live markup.
  return '<a href="' + esc(TICKET_BROWSE_URL) + encodeURIComponent(t)
    + '" target="_blank" rel="noopener">' + esc(t) + '</a>';
}

/** The tracker comments worth showing for a query: those carrying the question's
 * words, at most three. A 122-comment thread pasted into an answer is not an answer,
 * and the whole thread is in knowledge/intent/ticket-tracker.json.gz for anyone who
 * wants it. With no query terms - a bare ticket lookup - the opening two stand in.
 * @param {string[]|undefined} comments @param {string[]} terms */
function commentRows(comments, terms) {
  if (!comments?.length) return '';
  const wanted = terms?.length
    ? comments.filter((c) => terms.some((t) => c.toLowerCase().includes(t)))
    : comments.slice(0, 2);
  const shown = (wanted.length ? wanted : comments.slice(0, 1)).slice(0, 3);
  return shown.map((c) => '<div class="trow"><span class="tid">comment</span> '
    + esc(clipWords(c, 400)) + '</div>').join('');
}

/** Cut to at most limit characters, at a word boundary, without a regex.
 * `/\s+\S*$/` reads naturally and backtracks super-linearly on a long tail of
 * whitespace, which Sonar rightly flags: this runs over comment text nobody
 * controls. lastIndexOf is linear and says the same thing.
 * @param {string} text @param {number} limit */
function clipWords(text, limit) {
  if (text.length <= limit) return text;
  const head = text.slice(0, limit);
  const cut = head.lastIndexOf(' ');
  return (cut > 0 ? head.slice(0, cut) : head).trimEnd() + '…';
}

/** @param {string} t */
function ticketChip(t) {
  const detail = ticketDetail(t);
  return '<span title="' + (detail ? esc(detail) : 'no description found') + '">' + ticketLink(t) + '</span>';
}

/** Inline ticket rows - id, business description, dates - for answers where
 * the detail should be readable without hovering.
 * @param {string[]} tickets @param {number} max */
function ticketRows(tickets, max) {
  let html = '';
  let shown = 0;
  for (const t of tickets) {
    const detail = ticketDetail(t);
    if (!detail) continue;
    html += '<div class="trow"><span class="tid">' + ticketLink(t) + '</span> ' + esc(detail)
      + ticketDates(TICKET_INFO[t], Boolean(TICKET_INFO[t]?.t || TITLES[t])) + '</div>';
    if (++shown === max) break;
  }
  return html;
}

/* ---- commit evidence: what a ticket's own commits said ----
 * Each field is shown under its own name. A reader has to be able to tell a
 * tracker title from a commit subject from a commit body: the store's grounding
 * contract turns on which of the three a statement came from, and a body
 * presented as a title would be a false attribution. */

const LABEL_DESCRIPTION = 'description, from commit messages';
const LABEL_SUBJECT = 'commit subject';
const LABEL_TRACKER = 'from the tracker';
const LABEL_BODY = 'commit body, as written';

/** Escaped for display, with line breaks kept. Bodies are bulleted lists as
 * often as prose, and lose their meaning as one run-on paragraph.
 *
 * One pass, so the order cannot be got wrong: inserting the breaks first and
 * escaping afterwards would escape the breaks, and that mistake is invisible
 * until someone reads a body.
 * @param {string} s */
const escLines = (s) => s.replace(/[&<>"\n]/g, (c) => ESCAPES[c] || c);

/** One labelled row per text, so the reader knows which field they are reading.
 * @param {string} label @param {string[]|undefined} texts */
const evidenceRows = (label, texts) =>
  (texts || [])
    .map((t) => '<div class="ev"><span class="ev-l">' + label + '</span> ' + escLines(t) + '</div>')
    .join('');

/** Commit bodies behind a disclosure: a body runs to thousands of characters,
 * so it belongs in the technical-detail idiom rather than a summary line.
 * @param {string[]|undefined} bodies */
const bodyDetails = (bodies) =>
  bodies?.length
    ? '<details class="tech"><summary>' + LABEL_BODY + ' (' + bodies.length + ')</summary>'
      + bodies.map((b) => '<div class="cbody">' + escLines(b) + '</div>').join('')
      + '</details>'
    : '';

/** @param {number} n @param {string} one @param {string} many */
const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many);

/** Whether a text carries any of the query terms. A single-character term never
 * counts: it matches almost any prose, so a hit on one is an accident.
 * @param {string} text @param {string[]} terms */
const carriesTerm = (text, terms) => {
  const l = text.toLowerCase();
  return terms.some((t) => t.length > 1 && l.includes(t));
};

/** @param {string} s reduced for comparing two texts by what they say */
const normText = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();

/** The commit subjects that say something the shown description does not. The
 * description is curated from the same subjects, so an exact repeat is the
 * ordinary case and showing it twice would be noise.
 * @param {TicketInfo|undefined} info @param {string[]} shown texts already on screen
 * @returns {string[]} */
function extraSubjects(info, shown) {
  const keys = shown.filter(Boolean).map(normText);
  /** @type {string[]} */
  const extra = [];
  for (const s of info?.s || []) {
    const key = normText(s);
    if (key && !keys.some((k) => k.includes(key))) {
      keys.push(key);
      extra.push(s);
    }
  }
  return extra;
}

/** @param {Entry} e */
function card(e) {
  return (
    '<div class="card"><h3>' + esc(e[0]) + ' <span class="kind">' + e[4] + '</span></h3>'
    + (e[2] ? '<div class="path">' + esc(e[1]) + ' &middot; ' + esc(e[2]) + '</div>' : '')
    + (e[3] ? '<div class="row"><b>community:</b> ' + esc(e[3]) + '</div>' : '')
    + (e[6].length
        ? '<div class="row"><b>connects to:</b> ' + e[6].map(esc).join(', ')
          + ' <span style="color:var(--muted)">(' + e[5] + ' connections)</span></div>'
        : '')
    + (e[7].length
        ? '<div class="row tickets"><b>tickets:</b> ' + e[7].map(ticketChip).join('') + '</div>'
        : '')
    + '</div>'
  );
}

/** @returns {Set<string>} the currently ticked kind filters */
function searchKinds() {
  const kinds = new Set();
  for (const c of document.querySelectorAll('.k')) {
    const input = /** @type {HTMLInputElement} */ (c);
    if (input.checked) kinds.add(input.value);
  }
  return kinds;
}

/** @param {string} label @param {string[]} terms */
function searchScore(label, terms) {
  let score = 0;
  if (label === terms.join(' ')) score += 3;
  if (terms.some((t) => label.startsWith(t))) score += 2;
  if (terms.every((t) => label.includes(t))) score += 1;
  return score;
}

function runSearch() {
  const terms = q.value.toLowerCase().split(/\s+/).filter(Boolean);
  const repo = repoSel.value;
  const kinds = searchKinds();
  if (!terms.length) {
    out.innerHTML = '';
    meta.textContent = N.toLocaleString() + ' searchable entries - start typing';
    return;
  }
  /** @type {[number, Entry][]} */
  const scored = [];
  for (let i = 0; i < N && scored.length < 400; i++) {
    const e = DATA[i];
    const skip = (repo && e[1] !== repo) || !kinds.has(e[4])
      || !terms.every((t) => hay[i].includes(t));
    if (!skip) scored.push([searchScore(labelLower[i], terms), e]);
  }
  scored.sort((a, b) => b[0] - a[0]);
  const top = scored.slice(0, 100);
  meta.textContent =
    scored.length + (scored.length === 400 ? '+' : '') + ' matches'
    + (top.length < scored.length ? ', showing ' + top.length : '');
  out.innerHTML = top.map((s) => card(s[1])).join('');
}

/* ---- Ask mode: JS port of graphify's query ranking ---- */

/** @param {string} t */
const stem = (t) => (t.length > 4 ? t.replace(/ies$/, 'y').replace(/e?s$/, '') : t);

/** @param {string} question @returns {string[]} */
function queryTerms(question) {
  const all = tok(question);
  const content = all.filter((t) => !STOP.has(t) && t.length > 1);
  return [...new Set((content.length ? content : all).map(stem))];
}

/**
 * Semantic expansion: neighbours of the query terms from the build-time
 * MiniLM index, weighted down so they inform but never outrank lexical hits.
 * @param {string[]} terms
 * @returns {[string, number][]} [neighbourToken, weight]
 */
function expandTerms(terms) {
  const seen = new Set(terms);
  /** @type {[string, number][]} */
  const expansions = [];
  for (const t of terms) {
    for (const [n, sim] of (SYN[t] || []).slice(0, 3)) {
      if (!seen.has(n)) {
        seen.add(n);
        expansions.push([n, Math.max(0.1, sim - 0.35)]);
      }
    }
  }
  return expansions;
}

/** @type {Map<string, number>} */
const idfCache = new Map();
/** @param {string[]} terms @returns {Record<string, number>} */
function idfFor(terms) {
  const missing = terms.filter((t) => !idfCache.has(t));
  if (missing.length) {
    /** @type {Record<string, number>} */
    const df = {};
    missing.forEach((t) => (df[t] = 0));
    for (let i = 0; i < N; i++) {
      const l = labelLower[i];
      for (const t of missing) if (l.includes(t)) df[t]++;
    }
    for (const t of missing) idfCache.set(t, Math.log(1 + N / (1 + df[t])));
  }
  /** @type {Record<string, number>} */
  const w = {};
  for (const t of terms) w[t] = idfCache.get(t) || 1;
  return w;
}

const T_EXACT = 1000, T_PREFIX = 100, T_SUB = 1, T_SRC = 0.5;
// A semantic neighbour is inferred, not evidence, so its tiers are capped at
// the direct substring tier and below - never the prefix tier. See
// expansionBonus for the measurement that forced this.
const T_EXPAND = T_SUB, T_EXPAND_SUB = T_SRC;

/** Whole-query tier: a multi-word query equal to (or prefixing) a label
 * must dominate the per-token sums (graphify serve.py parity).
 * @param {string} norm @param {string} bare @param {string} joined @param {number} joinedW */
function fullQueryBonus(norm, bare, joined, joinedW) {
  const ltoks = tok(norm).join(' ');
  if (joined === norm || joined === bare || joined === ltoks) return T_EXACT * 10 * joinedW;
  if (norm.startsWith(joined) || ltoks.startsWith(joined)) return T_PREFIX * 10 * joinedW;
  return 0;
}

/** Per-term tiers: strongest tier per term, coverage counted on label hits.
 * @param {string} norm @param {string} bare @param {string} src
 * @param {string[]} terms @param {Record<string, number>} w */
function termTiers(norm, bare, src, terms, w) {
  let matched = 0, tiered = 0, flat = 0;
  for (const t of terms) {
    const tw = w[t];
    if (t === norm || t === bare) {
      tiered += T_EXACT * tw; matched++;
    } else if (norm.startsWith(t) || bare.startsWith(t)) {
      tiered += T_PREFIX * tw; matched++;
    } else if (norm.includes(t)) {
      flat += T_SUB * tw; matched++;
    }
    if (src.includes(t)) flat += T_SRC * tw;
  }
  return { matched, tiered, flat };
}

/**
 * Query terms for which the index holds no evidence at all.
 *
 * "Absence of evidence is a finding" is a stated promise of this store, and
 * the ranker alone cannot keep it: a question about something the estate does
 * not contain still scores, because its ordinary words ("storage", "work",
 * "rate") match thousands of entries. Measured on a real estate, an
 * out-of-domain question outscored a legitimate one - so a score or
 * term-coverage threshold would silence real questions while letting these
 * through. What separates them cleanly is whether the distinctive words match
 * anything at all: an absent term has zero hits across the whole index, while
 * every in-domain question had at least one hit for each of its terms.
 *
 * Both indexes are consulted, the entries and the ticket evidence. A word that
 * only a commit body holds is evidenced - the page would otherwise report "no
 * evidence for: X" directly above X's own commit body.
 *
 * @param {string[]} terms @returns {string[]} terms with no match anywhere
 */
function unevidencedTerms(terms) {
  const missing = [];
  for (const t of terms) {
    let seen = false;
    for (let i = 0; i < N && !seen; i++) if (hay[i].includes(t)) seen = true;
    for (let i = 0; i < ticketHay.length && !seen; i++) if (ticketHay[i].includes(t)) seen = true;
    if (!seen) missing.push(t);
  }
  return missing;
}

const MAX_EVIDENCE_TICKETS = 3;
/**
 * A word counts as decisive when it is at least this informative a share of the
 * most informative word the corpus holds for the question, and a ticket has to
 * match a decisive word to be shown at all. Ordering alone is not enough: the
 * page shows three tickets under a heading saying they answer the question, so a
 * ticket that matched none of its decisive words must not appear, rather than
 * appearing third.
 *
 * Measured on a real estate: for a question whose subject word scored 5.90, its
 * ordinary words scored 5.27 and 3.96, so a bar at nine tenths of the best
 * admits the subject and excludes the rest.
 */
const DECISIVE_SHARE = 0.9;
/** BM25's constants at their usual values: `K1` caps what repeating a word can
 * add, `B` how much a text's length discounts it. */
const BM25_K1 = 1.2, BM25_B = 0.75;

/** Lexicographic order, independent of locale. @param {string} a @param {string} b */
function cmpId(a, b) {
  if (a < b) return -1;
  return a > b ? 1 : 0;
}

/** @type {Map<string, number>} */
const ticketDfCache = new Map();

/**
 * How many tickets' evidence holds each term.
 *
 * Cached per term and computed only for terms not seen before: a question has a
 * handful of terms and the corpus has thousands of tickets, so the scan is worth
 * doing once. Deliberately not `idfFor`, which counts document frequency over
 * node labels: a word can be everywhere in labels and nowhere in commit text, or
 * the reverse, and borrowing the wrong denominator would mis-weight silently.
 *
 * @param {string[]} terms @returns {Record<string, number>}
 */
function ticketDfFor(terms) {
  const missing = terms.filter((t) => !ticketDfCache.has(t));
  if (missing.length) {
    /** @type {Record<string, number>} */
    const counts = {};
    missing.forEach((t) => (counts[t] = 0));
    for (const text of ticketHay) {
      for (const t of missing) if (text.includes(t)) counts[t]++;
    }
    for (const t of missing) ticketDfCache.set(t, counts[t]);
  }
  /** @type {Record<string, number>} */
  const df = {};
  for (const t of terms) df[t] = ticketDfCache.get(t) || 0;
  return df;
}

/**
 * How informative each of the question's words is within the ticket corpus, and
 * the weight of the most informative word the corpus actually holds.
 *
 * @param {string[]} terms
 * @returns {{w: Record<string, number>, strongest: number}}
 */
function ticketWeights(terms) {
  const df = ticketDfFor(terms);
  const total = ticketHay.length;
  /** @type {Record<string, number>} */
  const w = {};
  let strongest = 0;
  for (const t of terms) {
    w[t] = Math.log(1 + total / (1 + df[t]));
    // A word the corpus does not hold at all scores as the rarest of them by
    // this measure, and must not set the bar for the words it does hold - or a
    // question with one absent word would suppress every real match.
    if (df[t] > 0 && t.length > 1 && w[t] > strongest) strongest = w[t];
  }
  return { w, strongest };
}

/** How much of the question one ticket's evidence carries.
 *
 * A term's contribution is its rarity weight, scaled by how often the ticket
 * repeats it and discounted by how long the ticket's evidence is - BM25's
 * standard form. Length matters here more than in most indexes: this corpus
 * mixes one-line subjects with thousands of characters of body prose, and
 * without the discount a long body wins simply by covering more words.
 *
 * @param {string} text @param {number} len @param {string[]} terms
 * @param {Record<string, number>} w @param {number} floor
 * @returns {{weight: number, hits: number, decisive: boolean}} summed
 *   contribution, total occurrences, and whether any matched term was decisive
 *   enough to justify showing the ticket at all */
function evidenceScore(text, len, terms, w, floor) {
  let weight = 0, hits = 0;
  let decisive = false;
  const norm = BM25_K1 * (1 - BM25_B + BM25_B * (len / avgTicketLen));
  for (const t of terms) {
    const n = text.split(t).length - 1;
    if (!n) continue;
    weight += w[t] * (n * (BM25_K1 + 1)) / (n + norm);
    hits += n;
    // a single character is in almost every body ever written, so a match on
    // one is an accident however rare the corpus makes it look
    if (t.length > 1 && w[t] >= floor) decisive = true;
  }
  return { weight, hits, decisive };
}

/**
 * Tickets whose commit evidence carries the question's words.
 *
 * Ranked by how much the matched words tell you, not by how many matched.
 * Measured on a real estate, counting matches put the generic above the
 * distinctive: a question with one distinctive word and two ordinary ones
 * returned three tickets that had matched only the ordinary two, while none of
 * the 34 tickets whose evidence held the distinctive word appeared at all. That
 * is the same defect already recorded against the node ranker - a scorer blind
 * to rarity ranks the generic first - and this page states its result as
 * evidence, so being wrong here is worse than being silent.
 *
 * Rarity alone did not fix it, and the reason is worth keeping. In a corpus of
 * terse commit subjects, ordinary English is *rare*: on that estate the word
 * "was" appeared in 65 of 12,742 tickets, scoring almost as distinctive as the
 * subject word it was competing with, so two ordinary words still outvoted the
 * one that carried the question. Two further measures separate them, and both
 * are ordinary information retrieval:
 *
 * - **length normalisation** (BM25, in `evidenceScore`). The tickets that won
 *   on ordinary words had long prose bodies, which match anything by surface
 *   area; the ones that deserved to win were one-line subjects naming the thing
 *   asked about.
 * - **a decisive-word bar** (`DECISIVE_SHARE`). A ticket that matched none of
 *   the question's most informative words is not shown at all, however much
 *   ordinary vocabulary it accumulated.
 *
 * No stopword list, deliberately: which words are ordinary depends on the
 * corpus, no fixed list is ever complete, and both measures here recalibrate
 * themselves from the data as an estate grows.
 *
 * Ordered by summed contribution, then total occurrences, then ticket id. The
 * last is not cosmetic - two runs of the same question have to agree, and equal
 * scores would otherwise come out in whatever order the artefact was written in.
 *
 * @param {string[]} terms
 * @returns {{id: string, weight: number, hits: number}[]} the strongest few
 */
function ticketEvidence(terms) {
  const { w, strongest } = ticketWeights(terms);
  const floor = strongest * DECISIVE_SHARE;
  /** @type {{id: string, weight: number, hits: number}[]} */
  const scored = [];
  for (let i = 0; i < TICKET_IDS.length; i++) {
    const { weight, hits, decisive } = evidenceScore(ticketHay[i], ticketLen[i], terms, w, floor);
    if (decisive) scored.push({ id: TICKET_IDS[i], weight, hits });
  }
  scored.sort((a, b) => b.weight - a.weight || b.hits - a.hits || cmpId(a.id, b.id));
  return scored.slice(0, MAX_EVIDENCE_TICKETS);
}

/** Discounted contribution of semantic-neighbour terms.
 * @param {string} norm @param {string} bare
 * @param {[string, number][]} expansions @param {Record<string, number>} w */
/**
 * Bonus for semantic neighbours of the query terms.
 *
 * Capped at the direct substring tier deliberately. These tiers were once
 * T_PREFIX/T_SUB - the same as a direct term match - and expansion bonuses are
 * added outside the coverage scaling that `termTiers` results pass through
 * (cov^2). On a multi-term question that combination let one weak neighbour
 * dominate: measured on a real estate, "what handles video transcription of
 * hearings?" returned ten Helm-chart `image` fields, because `image` is a
 * neighbour of `video`, and an exact label match on it scored 229 against 13
 * for the entry whose own label contains "transcription".
 *
 * A neighbour is a guess about vocabulary; a term match is evidence in the
 * index. Ranking the guess above the evidence inverts the store's grounding
 * contract, so expansions now break ties and bridge vocabulary without ever
 * outranking a direct hit.
 *
 * @param {string} norm @param {string} bare
 * @param {[string, number][]} expansions @param {Record<string, number>} w
 */
function expansionBonus(norm, bare, expansions, w) {
  let bonus = 0;
  for (const [t, wgt] of expansions) {
    const tw = w[t] * wgt;
    if (t === norm || t === bare || norm.startsWith(t)) bonus += T_EXPAND * tw;
    else if (norm.includes(t)) bonus += T_EXPAND_SUB * tw;
  }
  return bonus;
}

/**
 * Rank every entry against the query terms, graphify-style.
 * @param {string[]} terms
 * @param {[string, number][]} [expansions] semantic neighbours with weights
 * @returns {[number, number][]} [score, entryIndex] sorted descending
 */
function rankNodes(terms, expansions = []) {
  const w = idfFor(terms.concat(expansions.map((x) => x[0])));
  const joined = terms.join(' ');
  const joinedW = Math.max(1, ...terms.map((t) => w[t]));
  const multiTerm = terms.length > 1;
  /** @type {[number, number][]} */
  const ranked = [];
  for (let i = 0; i < N; i++) {
    const norm = labelLower[i];
    const bare = norm.replace(/\(\)$/, '');
    let score = multiTerm ? fullQueryBonus(norm, bare, joined, joinedW) : 0;
    const { matched, tiered, flat } = termTiers(norm, bare, DATA[i][2].toLowerCase(), terms, w);
    const cov = matched / terms.length;
    // graphify parity: only the exact/prefix tier is coverage-scaled;
    // substring/source bonuses stay unscaled (serve.py #1602)
    score += tiered * cov * cov + flat + expansionBonus(norm, bare, expansions, w);
    if (score > 0) ranked.push([score, i]);
  }
  ranked.sort((a, b) => b[0] - a[0]);
  return ranked;
}

/**
 * @param {[number, number][]} ranked
 * @param {string[]} terms
 * @returns {number[]} seed entry indexes
 */
function pickSeeds(ranked, terms) {
  const seeds = ranked.slice(0, 10).map((r) => r[1]);
  for (const t of terms) {
    if (!seeds.some((i) => labelLower[i].includes(t))) {
      const hit = ranked.find((r) => labelLower[r[1]].includes(t));
      if (hit && !seeds.includes(hit[1])) seeds.push(hit[1]);
    }
  }
  return seeds.slice(0, 14);
}

/**
 * @param {number[]} seedIdx
 * @param {number} depth
 * @param {number} cap
 * @returns {Map<number, number>} entryIndex -> hop distance
 */
function bfs(seedIdx, depth, cap) {
  const reached = new Map(seedIdx.map((i) => [i, 0]));
  let frontier = seedIdx;
  for (let d = 1; d <= depth; d++) {
    /** @type {number[]} */
    const next = [];
    for (const i of frontier)
      for (const n of ADJ[i]) {
        if (!reached.has(n)) {
          reached.set(n, d);
          next.push(n);
        }
      }
    frontier = next;
    if (reached.size > cap) break;
  }
  return reached;
}

/**
 * Pre-written community summaries for the areas the given entries live in.
 * @param {number[]} idxs entry indexes, most relevant first
 * @param {number} max
 */
function summariesFor(idxs, max) {
  /** @type {Set<number>} */
  const seen = new Set();
  let html = '';
  for (const i of idxs) {
    const cid = DATA[i][8];
    if (cid == null || cid < 0 || seen.has(cid)) continue;
    const s = SUMMARIES[cid];
    if (!s) continue;
    seen.add(cid);
    html += '<div class="summary"><b>' + esc(DATA[i][3] || 'Community ' + cid) + ':</b> ' + esc(s) + '</div>';
    if (seen.size === max) break;
  }
  return html ? '<h2>What these areas do</h2>' + html : '';
}

/** @param {string} t */
const answer = (t) => '<div class="answer">' + t + '</div>';
/** set by runAsk when semantic expansion contributed */
let expansionNote = '';
/** @param {string} t */
const modeNote = (t) => {
  meta.textContent = 'question type: ' + t + ' - deterministic answer composed from the graph' + expansionNote;
};

/* ---- composed answer views ----
 * Composition policy: business language leads (community summaries, Gherkin
 * features, ticket change history); code-level evidence renders inside a
 * collapsed "Technical evidence" disclosure so a non-technical reader gets
 * a readable answer and a developer can still expand the detail. */

/** Collapsed code-evidence section.
 * @param {string} inner @param {number} count */
const techDetails = (inner, count) =>
  inner
    ? '<details class="tech"><summary>Technical evidence (' + count + ' items)</summary>'
      + inner + '</details>'
    : '';

/** Business-feature cards drawn from the given entry indexes.
 * @param {Iterable<number>} idxs @param {number} max */
function featureCards(idxs, max) {
  /** @type {number[]} */
  const feats = [];
  for (const i of idxs) if (DATA[i][4] === 'feature') feats.push(i);
  feats.sort((a, b) => DATA[b][5] - DATA[a][5]);
  const cards = feats.slice(0, max).map((i) => card(DATA[i])).join('');
  return cards ? '<h2>Business features in this area</h2>' + cards : '';
}

/** Commit-history meta line for a ticket-lookup answer.
 * @param {TicketInfo|undefined} info @param {boolean} hasTitle */
function ticketMetaLine(info, hasTitle) {
  if (!info) return '';
  let range = esc(info.first);
  if (info.last !== info.first) range += ' → ' + esc(info.last);
  const source = hasTitle ? '' : '; description from commit messages';
  return ' <span style="color:var(--muted)">' + info.n + ' commits, ' + range + source + '.</span>';
}

/** Which indexed entries carry a ticket, as a phrase.
 * @param {string} id */
function ticketReach(id) {
  /** @type {number[]} */
  const idxs = [];
  for (let i = 0; i < N; i++) if (DATA[i][7].includes(id)) idxs.push(i);
  if (!idxs.length) return ' — carried by no indexed entry.';
  const repos = [...new Set(idxs.map((i) => DATA[i][1]).filter(Boolean))];
  return ' — ' + plural(idxs.length, 'entry', 'entries')
    + ' in ' + plural(repos.length, 'repository', 'repositories') + ': '
    + idxs.slice(0, 4).map((i) => esc(DATA[i][0])).join(', ')
    + (idxs.length > 4 ? ' …' : '');
}

/** The pieces of one ticket's evidence that carry any of the query terms, each
 * under the name of the field it came from.
 * @param {string} id @param {string[]} terms */
function matchingEvidence(id, terms) {
  const info = TICKET_INFO[id];
  if (!info) return '';
  /** @param {string[]|undefined} texts */
  const carrying = (texts) => (texts || []).filter((x) => carriesTerm(x, terms));
  return evidenceRows(LABEL_DESCRIPTION, carrying(info.d))
    + evidenceRows(LABEL_SUBJECT, carrying(info.s))
    + bodyDetails(carrying(info.b))
    // The tracker's own words, after what the commits said. Comments are where
    // somebody explains why a change was made, so a question whose words appear
    // in one should surface it rather than stop at the commit subject.
    + evidenceRows(LABEL_TRACKER, carrying(info.x ? [info.x] : []).concat(carrying(info.c)));
}

/** What the commits themselves said, for the tickets whose evidence carries the
 * question's words. An extra section: the composed answer above it is untouched.
 * @param {{id: string}[]} matches @param {string[]} terms */
function ticketEvidenceHtml(matches, terms) {
  return '<h2>What the commits say about these words</h2>'
    + matches
      .map((m) => '<div class="trow"><span class="tid">' + ticketLink(m.id) + '</span>'
        + ticketReach(m.id) + matchingEvidence(m.id, terms) + '</div>')
      .join('');
}

/** Add the commit-evidence section, and name it in the meta line when that
 * evidence is the only thing that answered the question.
 * @param {{id: string}[]} matches @param {string[]} terms */
function appendTicketEvidence(matches, terms) {
  if (!matches.length) return;
  out.insertAdjacentHTML('beforeend', ticketEvidenceHtml(matches, terms));
  if (!meta.textContent) modeNote('commit evidence');
}

/** @param {string} id */
function vTicket(id) {
  modeNote('Jira ticket lookup');
  /** @type {number[]} */
  const hits = [];
  for (let i = 0; i < N; i++) if (DATA[i][7].includes(id) || DATA[i][0] === id) hits.push(i);
  const repos = [...new Set(hits.map((i) => DATA[i][1]).filter(Boolean))];
  const detail = ticketDetail(id);
  const info = TICKET_INFO[id];
  // Every mined description the headline is not already carrying. This was
  // `d[1]` alone, which was right only while the headline was always `d[0]`.
  // Once the tracker's own title took precedence, `d[0]` became evidence that
  // nothing rendered: on one estate that silently dropped the description
  // saying a production revert had added a local copy of the address pipe,
  // from an answer that still presented itself as the commit evidence.
  const detailKey = normText(detail);
  const descriptions = (info?.d || []).filter((x) => normText(x) !== detailKey);
  const shown = hits.slice(0, 30).filter((i) => DATA[i][4] !== 'ticket');
  out.innerHTML =
    answer('<b>' + ticketLink(id) + '</b>' + (detail ? ' — “' + esc(detail) + '”' : '')
      + ' touches <b>' + hits.length + '</b> graph entries across <b>' + repos.length + '</b> repositories.'
      + ticketMetaLine(info, Boolean(info?.t || TITLES[id])))
    + (info?.x ? '<div class="trow"><span class="tid">tracker</span> ' + esc(info.x) + '</div>' : '')
    + commentRows(info?.c, [])
    + evidenceRows(LABEL_DESCRIPTION, descriptions)
    + evidenceRows(LABEL_SUBJECT, extraSubjects(info, [detail, ...descriptions]))
    + bodyDetails(info?.b)
    + summariesFor(hits, 2)
    + featureCards(hits, 4)
    + techDetails(shown.map((i) => card(DATA[i])).join(''), shown.length);
}

/** @param {number[]} seeds */
function vWhichRepos(seeds) {
  modeNote('which repositories');
  const primary = DATA[seeds[0]][0];
  const pl = labelLower[seeds[0]];
  /** @type {number[]} */
  const matches = [];
  for (let i = 0; i < N; i++) if (labelLower[i] === pl) matches.push(i);
  /** @type {Map<string, number[]>} */
  const byRepo = new Map();
  for (const i of matches) pushGroup(byRepo, DATA[i][1] || '(none)', i);
  const rows = [...byRepo.entries()]
    .map(([r, idxs]) =>
      '<tr><td><b>' + esc(r) + '</b></td><td class="mono">'
      + idxs.map((i) => esc(DATA[i][2])).join('<br>') + '</td><td class="tickets">'
      + [...new Set(idxs.flatMap((i) => DATA[i][7]))].slice(0, 5).map(ticketChip).join('') + '</td></tr>')
    .join('');
  const others = [...new Set(seeds.map((i) => DATA[i][0]).filter((l) => l.toLowerCase() !== pl))].slice(0, 6);
  out.innerHTML =
    answer('<b>' + esc(primary) + '</b> appears in <b>' + byRepo.size
      + '</b> repositories (' + matches.length + ' matching nodes).'
      + (byRepo.size > 1 ? ' No edges connect them - these are independent implementations.' : ''))
    + summariesFor(matches, 2)
    + '<table class="rt"><tr><th>repository</th><th>defined in</th><th>tickets</th></tr>' + rows + '</table>'
    + (others.length
        ? '<h2>Related names in the graph</h2><div class="chips">'
          + others.map((o) => '<span>' + esc(o) + '</span>').join('') + '</div>'
        : '');
}

/** @param {number[]} seeds */
function vWhereUsed(seeds) {
  modeNote('where is it used');
  const s = seeds[0], e = DATA[s];
  const neigh = [...ADJ[s]].sort((a, b) => DATA[b][5] - DATA[a][5]);
  /** @type {Map<string, number[]>} */
  const byRepo = new Map();
  for (const i of neigh) pushGroup(byRepo, DATA[i][1] || '(none)', i);
  const repoRows = [...byRepo.entries()]
    .map(([r, idxs]) => '<tr><td><b>' + esc(r) + '</b></td><td>'
      + idxs.length + ' connected nodes</td></tr>')
    .join('');
  const tech = card(e)
    + [...byRepo.entries()]
      .map(([r, idxs]) =>
        '<div class="group"><div class="g-title">' + esc(r) + ' (' + idxs.length + ')</div><div class="g-items">'
        + idxs.slice(0, 12).map((i) => esc(DATA[i][0])).join(' &middot; ') + '</div></div>')
      .join('');
  out.innerHTML =
    answer('<b>' + esc(e[0]) + '</b> (' + esc(e[1]) + ') is directly connected to <b>'
      + neigh.length + '</b> nodes'
      + (byRepo.size > 1 ? ' across <b>' + byRepo.size + '</b> repositories' : '') + '.')
    + summariesFor([s, ...neigh], 2)
    + featureCards(neigh, 3)
    + '<h2>Used from</h2><table class="rt"><tr><th>repository</th><th>reach</th></tr>' + repoRows + '</table>'
    + techDetails(tech, neigh.length + 1);
}

/** @param {number[]} seeds */
function vImpact(seeds) {
  modeNote('impact analysis');
  const s = seeds[0], e = DATA[s];
  const reached = bfs([s], 2, 2000);
  /** @type {Map<string, number[]>} */
  const byComm = new Map();
  const repos = new Set();
  for (const i of reached.keys()) {
    if (i === s) continue;
    repos.add(DATA[i][1]);
    pushGroup(byComm, DATA[i][3] || 'unlabelled', i);
  }
  const grouped = [...byComm.entries()].sort((a, b) => b[1].length - a[1].length).slice(0, 10);
  const commRows = grouped
    .map(([k, idxs]) => '<tr><td><b>' + esc(k) + '</b></td><td>' + idxs.length + ' nodes</td></tr>')
    .join('');
  const tech = card(e)
    + grouped
      .map(([k, idxs]) =>
        '<div class="group"><div class="g-title">' + esc(k) + ' (' + idxs.length + ')</div><div class="g-items">'
        + idxs.slice(0, 10).map((i) => esc(DATA[i][0])).join(' &middot; ') + '</div></div>')
      .join('');
  out.innerHTML =
    answer('Changing <b>' + esc(e[0]) + '</b> (' + esc(e[1]) + ') could reach <b>'
      + (reached.size - 1) + '</b> connected nodes within two hops, spanning <b>' + byComm.size
      + '</b> communities in <b>' + repos.size + '</b> repositories.')
    + summariesFor([s, ...reached.keys()], 3)
    + featureCards(reached.keys(), 3)
    + '<h2>Affected areas</h2><table class="rt"><tr><th>community</th><th>reach</th></tr>' + commRows + '</table>'
    + techDetails(tech, reached.size - 1);
}

/** @param {number[]} seeds */
function vWhy(seeds) {
  modeNote('business intent');
  // Among same-named seeds, prefer the instance with the richest ticket
  // evidence (then highest degree) - "why does X exist" should anchor on
  // the copy with history, not an arbitrary one.
  let s = seeds[0];
  for (const i of seeds) {
    if (labelLower[i] !== labelLower[seeds[0]]) continue;
    const richer = DATA[i][7].length > DATA[s][7].length
      || (DATA[i][7].length === DATA[s][7].length && DATA[i][5] > DATA[s][5]);
    if (richer) s = i;
  }
  const e = DATA[s];
  const reached = bfs([s], 2, 1200);
  /** @type {Map<string, number>} */
  const tickets = new Map();
  /** @type {[number, number][]} */
  const features = [];
  for (const [i, d] of reached) {
    for (const t of DATA[i][7]) tickets.set(t, (tickets.get(t) || 0) + (2 - d));
    if (DATA[i][4] === 'feature' && i !== s) features.push([DATA[i][5], i]);
  }
  features.sort((a, b) => b[0] - a[0]);
  const topT = [...tickets.entries()].sort((a, b) => b[1] - a[1]).slice(0, 14).map((t) => t[0]);
  out.innerHTML =
    answer('<b>' + esc(e[0]) + '</b> (' + esc(e[1]) + ') is linked to <b>' + topT.length
      + '</b> Jira tickets and <b>' + features.length + '</b> business features in its neighbourhood.'
      + ' Hover a ticket for its title; commit subjects live in the history datasets.')
    + summariesFor([s], 1)
    + (features.length
        ? '<h2>Business features exercising this area</h2>'
          + features.slice(0, 6).map((f) => card(DATA[f[1]])).join('')
        : '')
    + (topT.length
        ? '<h2>What changed here, per the commit history</h2>' + ticketRows(topT, 8)
          + '<div class="chips">' + topT.map(ticketChip).join('') + '</div>'
        : '')
    + techDetails(card(e), 1);
}

/** @param {[number, number][] & {terms?: string[]}} ranked */
function vJourney(ranked) {
  modeNote('user journey');
  const feats = ranked.filter((r) => DATA[r[1]][4] === 'feature').slice(0, 6).map((r) => r[1]);
  if (!feats.length) return vCluster(ranked);
  const blocks = feats.map((f) => {
    const scen = ADJ[f].filter((i) => DATA[i][4] === 'scenario');
    const steps = ADJ[f].filter((i) => /stepdef/i.test(DATA[i][0])).map((i) => DATA[i][0]);
    return (
      card(DATA[f])
      + (scen.length
          ? '<div class="group"><div class="g-title">scenarios (' + scen.length + ')</div><div class="g-items">'
            + scen.slice(0, 8).map((i) => esc(DATA[i][0])).join('<br>') + '</div></div>'
          : '')
      + (steps.length
          ? '<div class="group"><div class="g-title">step definitions</div><div class="g-items">'
            + steps.slice(0, 6).map(esc).join(' &middot; ') + '</div></div>'
          : '')
    );
  });
  out.innerHTML =
    answer('<b>' + feats.length + '</b> scripted business features match this journey - '
      + 'each is a real E2E test written in business language, with its scenarios and step definitions.')
    + summariesFor(feats, 2)
    + blocks.join('');
}

/** @param {[number, number][] & {terms?: string[]}} ranked */
function vCluster(ranked) {
  modeNote('open question - evidence cluster');
  const terms = ranked.terms || [];
  const seeds = pickSeeds(ranked, terms);
  const reached = bfs(seeds, 2, 900);
  /** @type {Map<string, number[]>} */
  const groups = new Map();
  /** @type {Map<string, number>} */
  const tickets = new Map();
  for (const [i, d] of reached) {
    for (const t of DATA[i][7]) tickets.set(t, (tickets.get(t) || 0) + (2 - d));
    if (seeds.includes(i)) continue;
    pushGroup(groups, DATA[i][3] || (DATA[i][1] ? DATA[i][1] + ' (unlabelled)' : 'other'), i);
  }
  const topT = [...tickets.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12).map((t) => t[0]);
  const grouped = [...groups.entries()].sort((a, b) => b[1].length - a[1].length).slice(0, 8);
  const shownSeeds = seeds.slice(0, 8);
  const tech =
    '<h2>Strongest matches</h2>' + shownSeeds.map((i) => card(DATA[i])).join('')
    + '<h2>Connected evidence by community</h2>'
    + grouped
      .map(([k, idxs]) =>
        '<div class="group"><div class="g-title">' + esc(k) + ' (' + idxs.length + ')</div><div class="g-items">'
        + idxs.slice(0, 10).map((i) => esc(DATA[i][0])).join(' &middot; ') + '</div></div>')
      .join('');
  out.innerHTML =
    answer('Closest evidence: <b>' + reached.size + '</b> connected nodes across <b>'
      + groups.size + '</b> communities.')
    + summariesFor(seeds.concat([...groups.values()].sort((a, b) => b.length - a.length).flat()), 3)
    + featureCards(reached.keys(), 4)
    + (topT.length
        ? '<h2>What changed here, per the commit history</h2>' + ticketRows(topT, 5)
          + '<div class="chips">' + topT.map(ticketChip).join('') + '</div>'
        : '')
    + techDetails(tech,
        shownSeeds.length + grouped.reduce((n, g) => n + Math.min(g[1].length, 10), 0));
}

/**
 * Community-summary matching: prose summaries are a retrieval surface. A
 * community whose summary mentions the question's vocabulary gets its
 * entries boosted (and represented, if ranking missed it entirely).
 * @param {[number, number][]} ranked
 * @param {string[]} terms
 * @param {[string, number][]} expansions
 */
function applySummaryBoost(ranked, terms, expansions) {
  const commBoost = matchSummaries(terms, expansions);
  if (!commBoost.size) return;
  const present = new Set(ranked.map((r) => DATA[r[1]][8]));
  for (const [cid, hits] of commBoost) {
    if (!present.has(cid) && communityFirst[cid] !== undefined) {
      ranked.push([hits * 40, communityFirst[cid]]);
    }
  }
  for (const r of ranked) {
    const b = commBoost.get(DATA[r[1]][8]);
    if (b) r[0] += b * 40;
  }
  ranked.sort((a, b) => b[0] - a[0]);
}

/** Communities whose summary prose mentions the question's vocabulary.
 * @param {string[]} terms @param {[string, number][]} expansions
 * @returns {Map<number, number>} community id -> hit score (>= 3 only) */
function matchSummaries(terms, expansions) {
  /** @type {Map<number, number>} */
  const commBoost = new Map();
  for (const [cid, text] of SUMMARY_LIST) {
    let hits = 0;
    for (const t of terms) if (text.includes(t)) hits += 2;
    for (const [t] of expansions) if (text.includes(t)) hits += 1;
    if (hits >= 3) commBoost.set(cid, hits);
  }
  return commBoost;
}

/**
 * Match the question against the topic-brief library. A topic wins when its
 * configured keywords appear in the question (a direct keyword hit scores 2,
 * an expansion-term hit 1); at least one direct hit is required so semantic
 * drift alone can never surface a brief. Highest score wins.
 * @param {string} lq lowercase question
 * @param {[string, number][]} expansions
 * @returns {{slug: string, title: string, html: string, source: string}|null}
 */
function matchTopic(lq, expansions) {
  let best = null;
  let bestScore = 0;
  for (const [slug, topic] of Object.entries(TOPICS)) {
    let score = 0;
    let direct = 0;
    for (const k of topic.keywords) {
      if (lq.includes(k)) { score += 2; direct += 1; }
      else if (expansions.some(([t]) => t === k)) score += 1;
    }
    if (direct && score > bestScore) { best = { slug, ...topic }; bestScore = score; }
  }
  return best;
}

/** @param {{title: string, html: string, source: string}} topic */
const topicBriefHtml = (topic) =>
  '<div class="brief">' + topic.html +
  '<div class="b-src">Pre-written topic brief (' + esc(topic.source) +
  ') - composed with Claude at build time from graph evidence, then reviewed; ' +
  'live evidence follows below.</div></div>';

/** The dive whose repository name the question mentions; longest name wins.
 * @param {string} lq lowercase question
 * @returns {{repo: string, title: string, html: string, source: string, sha: string}|null} */
function matchDive(lq) {
  let best = null;
  for (const [repo, dive] of Object.entries(DIVES)) {
    if (lq.includes(repo.toLowerCase()) && (!best || repo.length > best.repo.length)) {
      best = { repo, ...dive };
    }
  }
  return best;
}

/** @param {{repo: string, title: string, html: string, source: string, sha: string}} dive */
const diveHtml = (dive) =>
  '<div class="brief">' + dive.html +
  '<div class="b-src">Deep dive (' + esc(dive.source) + ')'
  + (dive.sha ? ' - evidence measured at build ' + esc(dive.sha) : '')
  + '; live evidence follows below.</div></div>';

/** Offer to request a new brief when no pre-written one covers the question.
 * @param {string} question */
const requestBriefHtml = (question) => {
  if (!CONFIG.briefRequestUrl) return '';
  const url = CONFIG.briefRequestUrl + '?title='
    + encodeURIComponent('Topic brief request: ' + question);
  return '<div class="req-brief">No pre-written brief covers this question - '
    + '<a href="' + esc(url) + '" target="_blank" rel="noopener">request one</a> '
    + 'and the maintainers can compose it from the graph evidence.</div>';
};

/** Question-shape router: pick the composed answer view for the question.
 * @param {string} lq padded lowercase question
 * @param {[number, number][] & {terms?: string[]}} ranked
 * @param {number[]} seeds
 */
function routeQuestion(lq, ranked, seeds) {
  if (/\bjourney\b|\bscreens?\b|\bsteps\b|walk me|how (do|does|would) (a |an |the )?(user|adviser|prosecutor|clerk|legal)/.test(lq)) {
    return vJourney(ranked);
  }
  if (/\b(which|what|how many)\b[^?]*\b(repo|repositor|app|application|service)/.test(lq)) {
    return vWhichRepos(seeds);
  }
  if (/\b(where|who|what)\b[^?]*\b(use|uses|used|consume|consumes|depend|depends|reference|references|call|calls)\b/.test(lq)) {
    return vWhereUsed(seeds);
  }
  if (/\b(impact|impacted|affect|affected|break|breaks|blast|changing|changed?)\b/.test(lq)) {
    return vImpact(seeds);
  }
  if (/\bwhy\b|\bintent\b|\bpurpose\b|\brationale\b|\bbusiness reason\b/.test(lq)) {
    return vWhy(seeds);
  }
  return vCluster(ranked);
}

/** Name the query words the index holds no evidence for, and return true when
 *  none of them are evidenced - in which case the finding has been rendered as
 *  the whole answer.
 *
 *  Disclose, do not silence. An earlier design abstained when the rarest term
 *  was absent, which looked right on a large estate and then silenced four
 *  legitimate questions on a small one: ordinary question words ("used",
 *  "taken", "walk") are themselves absent from a small corpus, so one of them
 *  becomes the "rarest" term. Nothing distinguishes a question word from a
 *  subject without an English lexicon, and a blocklist of them would never be
 *  complete. So the note is additive - the reader is told which of their words
 *  the graph holds nothing for, and still gets whatever did match.
 *
 *  Pre-written prose (a topic brief or deep dive) is exempt: it was written for
 *  the question rather than matched against the index.
 *  @param {string[]} terms
 *  @param {boolean} prewritten
 *  @returns {boolean}
 */
function reportUnevidenced(terms, prewritten) {
  const missing = prewritten ? [] : unevidencedTerms(terms);
  if (missing.length) {
    expansionNote += ' | no evidence for: ' + missing.join(', ');
  }
  if (!terms.length || missing.length < terms.length) return false;
  meta.textContent =
    'question type: no evidence - the graph holds nothing for ' +
    missing.map((t) => '"' + t + '"').join(', ');
  out.innerHTML =
    '<div class="card"><h3>No evidence in this estate</h3><p>Nothing in the graph matches ' +
    missing.map((t) => '<code>' + esc(t) + '</code>').join(', ') +
    '. That is a finding rather than a ranking problem: this estate contains no such ' +
    "component, schema or feature. Try the estate's own vocabulary." +
    '</p></div>';
  return true;
}

/**
 * Put pre-written prose above the composed answer, or invite one where none
 * exists. A topic brief outranks a deep dive because it was written to answer a
 * question, while a dive was written about a repository.
 * @param {{title: string, html: string, source: string}|null} topic
 * @param {{repo: string, title: string, html: string, source: string, sha: string}|null} dive
 * @param {string} raw the question as typed
 */
function prependPrewritten(topic, dive, raw) {
  if (topic) {
    out.innerHTML = topicBriefHtml(topic) + out.innerHTML;
    meta.textContent = 'topic brief: ' + topic.title
      + (meta.textContent ? ' | ' + meta.textContent : '');
  } else if (dive) {
    out.innerHTML = diveHtml(dive) + out.innerHTML;
    meta.textContent = 'deep dive: ' + dive.repo
      + (meta.textContent ? ' | ' + meta.textContent : '');
  } else {
    out.innerHTML += requestBriefHtml(raw);
  }
}

function runAsk() {
  const raw = q.value.trim();
  if (!raw) {
    out.innerHTML = '';
    meta.textContent =
      'ask about a capability, journey or component - e.g. "which repositories implement AppState?"';
    return;
  }
  const ticket = /\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b/.exec(raw);
  if (ticket) return vTicket(ticket[0]);
  const terms = queryTerms(raw);
  const expansions = expandTerms(terms);
  expansionNote = expansions.length
    ? ' | expanded: ' + expansions.slice(0, 5).map((x) => x[0]).join(', ')
    : '';
  /** @type {[number, number][] & {terms?: string[]}} */
  const ranked = rankNodes(terms, expansions);
  ranked.terms = terms;
  applySummaryBoost(ranked, terms, expansions);
  const topic = matchTopic(raw.toLowerCase(), expansions);
  const dive = topic ? null : matchDive(raw.toLowerCase());
  const evidence = ticketEvidence(terms);
  if (reportUnevidenced(terms, Boolean(topic || dive))) return;
  if (!ranked.length && !topic && !dive && !evidence.length) {
    meta.textContent = 'nothing in the graph matches those words - try different terms';
    out.innerHTML = '';
    return;
  }
  if (ranked.length) {
    routeQuestion(' ' + raw.toLowerCase() + ' ', ranked, pickSeeds(ranked, terms));
  } else {
    out.innerHTML = '';
    meta.textContent = '';
  }
  appendTicketEvidence(evidence, terms);
  prependPrewritten(topic, dive, raw);
}

function run() { (mode === 'ask' ? runAsk : runSearch)(); }

/** @param {'search'|'ask'} m */
function setMode(m) {
  mode = m;
  getEl('tab-search').classList.toggle('on', m === 'search');
  getEl('tab-ask').classList.toggle('on', m === 'ask');
  getEl('search-filters').style.display = m === 'ask' ? 'none' : 'flex';
  q.placeholder = m === 'ask'
    ? 'e.g. which repositories implement AppState?  why does AddressPipe exist?  what is impacted if ApiState changes?'
    : 'e.g. address, SJP decision, AppState, CRC-12016…';
  run();
}

getEl('tab-search').addEventListener('click', () => setMode('search'));
getEl('tab-ask').addEventListener('click', () => setMode('ask'));
/** @type {ReturnType<typeof setTimeout>|undefined} */
let timer;
q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 200); });
repoSel.addEventListener('change', run);
document.querySelectorAll('.k').forEach((c) => c.addEventListener('change', run));
run();

// Expose the engine API for the Node test harnesses, which require() this
// file as a plain module (no dynamic code execution). One namespaced key;
// harmless in the browser.
/** @type {any} */ (globalThis).__explorerApi = {
  queryTerms, idfFor, expandTerms, rankNodes, pickSeeds, bfs,
  matchTopic, matchDive, runAsk, runSearch, q, out, meta,
  ticketEvidence, unevidencedTerms, extraSubjects,
};
