# How a knowledge store works

The mechanisms behind a store, for readers who want the science rather than
the operating instructions. Every claim here is checkable: each section names
the source file that implements it, and the constants quoted are the ones in
the code. Measured economics live in the
[operator guide](building-a-knowledge-store.md) — one home per fact.

## The constraints that shape everything

Four constraints, chosen up front, decide most of the design:

1. **The LLM runs at build time, never on the page.** Whoever builds a store
   may have a licence; the people querying it may not. Everything a model
   writes is validated and committed as static text.
2. **The store is committed files.** Consumers clone and read. There is no
   server, no database, no query-time network access.
3. **Deterministic where possible.** Extraction, indexing and page
   composition are pure functions of the sources: two runs on the same inputs
   produce byte-identical output. Compression included: every gzip writer uses
   level 9 with neither timestamp nor filename in the header — the behaviour
   of `gzip -9 -n` (`src/knowledgestore/io.py`, `gzip_text`).
4. **Every claim traces to evidence.** The
   [grounding contract](grounding-and-verification.md) governs anything a
   model authored; absence of evidence is reported as a finding, not filled.

Everything below is a consequence of holding those four at once.

## The graph layer

Each repository is extracted **from inside its own clone**
([graphify](https://github.com/safishamsi/graphify), AST-based), then the
per-repository graphs are merged in one operation. The merge namespaces node
ids by repository and stamps a `repo` attribute; `source_file` stays
repository-relative, which matters because it is the join key the
file-to-ticket index matches against. Extracting from anywhere else writes
paths that silently break that join.

**BDD specifications become graph nodes**
(`src/knowledgestore/extract_gherkin.py`). Feature files contribute feature
and scenario nodes; step definitions in Java, Python and TypeScript are
linked to the scenarios that use them by normalising both sides to a common
form — Cucumber expressions (`{int}`, `{string}`), typed parameters
(`{amount:d}`), regular-expression groups, quoted values and outline
parameters all reduce to the same placeholder, so a business step matches
whichever language declared it. Data tables and doc strings are deliberately
not modelled — they are example data, not structure — and the parser is a
measured structural subset: `Rule:`, `Example:` and localised keywords appeared
zero times across the 1,266 feature files it was built against.

**Communities** are detected by modularity optimisation over the merged
graph, driven by graphify: graspologic's Leiden where that library is
installed, seeded networkx Louvain otherwise — the same seed either way, so
like-for-like reruns are stable and churn is graph-change-driven.
Community ids are not stable across runs — nothing in modularity optimisation
anchors them — so two mechanisms restore continuity:

- `remap_communities_to_previous` aligns a new clustering's ids with the
  previous one by membership overlap, so a cluster that survives keeps its
  identity;
- every saved community label carries a **membership signature** (a hash of
  its member set). A later run reuses a saved name only while the signature
  matches; a community whose membership changed gets renamed from its hub
  rather than keeping a name that now describes something else.

**Cross-repository package edges** are the one place the graph connects
repositories directly (`src/knowledgestore/build_package_edges.py`): a node
per shared npm package, citing the `package.json` that declares it, with
edges from the files that import it — each edge citing the importing file.
Deliberately declaration-level: measured on a real estate, symbol-name
collisions across repositories were template scaffolding and vendored
copies — independent implementations, which the store already answers
correctly — so symbol-level identity (SCIP) stays deferred until
package-level answers prove insufficient.

What id continuity cannot fix is **splits**: re-clustering from scratch can
shatter a stable community into fragments, and prose attached to the whole
does not describe a part. The measured cost of that, and the economics of
refreshing generally, are in the operator guide §7.

## The history layers

Commit history is exported in **one `git log` pass per repository**
(`src/knowledgestore/export_git_history.py`), with file statistics riding
along via `--numstat --diff-merges=cc` and machine-readable record
separators. The naive implementation — one `git show` per commit — was
measured spawning 195,360 extra processes on a real estate; the single-pass
rewrite is pinned by a test asserting the dataset is unchanged.

The **intent index** (`src/knowledgestore/build_intent_index.py`) mines those
datasets: ticket ids matching `[A-Z][A-Z0-9]{1,9}-\d{1,6}` (configurable) are
extracted from commit subjects, producing a file → tickets map with first and
last touch dates, and a ticket → description corpus built from the subjects
themselves. This is why a store can answer *why* a file exists without any
issue-tracker API access.

## The prose layer, and how it is checked

Community summaries are the one layer a model writes. The pipeline constrains
that in both directions
(`src/knowledgestore/build_community_summaries.py`):

- **Inputs are digests**: per-community evidence packs (label, size,
  repositories, top nodes with source files, business features, tickets).
  Authors are instructed to claim nothing the digest does not show.
- **`merge` validates shape**: unknown community ids are rejected, as is any
  summary outside 60–700 characters.
- **`remap` protects continuity** with three refusals: it carries a summary
  only where the new cluster holds at least 60% of the old one's members
  (`--bar`); it refuses to run at all on an implausibly small summary set
  (`--floor`, default 10) or when fewer than half the graph's nodes carry a
  community (`--coverage`) — both of which would silently write a bad file
  over a good one.
- **`verify` checks grounding, not shape**: identifiers cited in each
  summary's prose are normalised (case and punctuation stripped), expanded
  with spelling variants (`.java` suffixes, dotted-name parts, `Test`
  pairings), and compared against the identifiers its digest contains. A
  speculation lexicon (*probably*, *likely*, *appears to*…) flags where
  evidence ran out. A citation absent from the evidence is either fabrication
  or paraphrase, and both warrant a human read — the
  [grounding contract](grounding-and-verification.md) explains why findings
  are a starting point rather than a defect count.

## The semantic bridge

Lexical matching cannot connect "court outcomes" to "hearing results". The
fix ships no model (`src/knowledgestore/build_semantic_index.py`): at build
time, the graph's own vocabulary — node labels, summaries, business features;
tokens of 4–24 letters, the 15,000 most frequent with document frequency ≥ 3
— is embedded once with MiniLM (`all-MiniLM-L6-v2`, 384 dimensions), and each
token's nearest neighbours at cosine ≥ 0.55 are written to one committed,
gzipped map. The page expands query tokens through that map at query time
with a dictionary lookup. Heavy model at build, small artefact shipped,
nothing at query time — the shape every ML addition here has to fit.

## The page

`explorer.html` is one self-contained file
(`src/knowledgestore/build_explorer.py`): the application script plus
embedded JSON blocks — the entry index sorted by degree descending, a flat
edge-pair array, ticket titles and commit-mined detail, community summaries,
and the token-neighbour map. Inclusion is degree-gated (default: 3
connections) and test files — sources matching `.spec.`, `__tests__` or
`/test/` — are excluded, except in repositories the estate declares as
end-to-end suites, where the tests *are* the business documentation.

**Absence is disclosed, not hidden.** The ranker alone cannot keep the store's
"absence of evidence is a finding" promise: a question about something the
estate does not contain still scores, because its ordinary words match
thousands of entries. Measured on a real estate, an out-of-domain question
outscored a legitimate one, so no score or coverage threshold separates them.
What does separate them is whether a word matches anything at all — so the page
names the query words the index holds no evidence for, and answers with that
finding alone when no word is evidenced. It discloses rather than abstains
because nothing distinguishes a question word from a subject without an English
lexicon: an earlier rule that abstained when the rarest word was absent
silenced four legitimate questions, because ordinary words like "used" and
"taken" are themselves missing from a small corpus.

Ask-mode ranking is a port of graphify's own query scorer, and is documented
in the application source in one line: *IDF-weighted terms,
exact/prefix/substring/source tiers, full-query bonus, squared term-coverage
scaling. Deterministic — no AI.* Pre-written topic briefs are served whole
when a question hits their keywords; everything else is composed from the
graph by template.

Three test layers pin this: unit tests on the scorer maths
(`tests/explorer/engine-unit.mjs`), a synthetic-estate fixture built through
the real pipeline (`tests/explorer/fixture.py`), and a page regression that
drives every answer shape and asserts the built page inlines the application
source **byte-for-byte** — so the code the tests exercise is provably the
code that ships.

## Reproducibility as a discipline

The repeated pattern across the codebase: a failure that happened once
becomes a gate, not a comment. `remap` refuses rather than degrades;
labels invalidate by signature rather than trust; the install-docs check
(`knowledgestore check-install-docs`) fails CI when a documented command
cannot work as written; the page regression fails when the shipped
application diverges from the tested one. Where behaviour cannot be gated —
clustering non-determinism, refresh economics — it is measured and written
down instead, in the operator guide, with the numbers from real refreshes.

This page is itself an instance. Its determinism claim was checked against
the code before publishing and turned out to be false — the gzip writers
embedded a timestamp and filename, so identical rebuilds produced different
bytes. The claim above became true in the same change that shipped this page,
gated by a test that fails on the old writers.

## Where to go next

- [Retrieval architecture](retrieval-architecture.md) — how this differs
  from vector RAG, and where each answer layer lives.
- [Grounding and verification](grounding-and-verification.md) — what
  "evidence-backed" means, precisely.
- [Building an effective knowledge store](building-a-knowledge-store.md) —
  the operator's judgement, and the measured economics behind this page's
  claims.
