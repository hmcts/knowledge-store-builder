---
name: knowledge-store-build
description: Build or refresh a knowledge store with knowledge-store-builder — run the pipeline stages, and write the layers that need an LLM (community summaries, topic briefs). Use when the user wants to create a knowledge store, refresh one after code changes, add repositories to an estate, or fill in missing summaries or topic briefs.
---

# Building and refreshing a knowledge store

The pipeline is deterministic except for two layers — community summaries and
topic briefs — which need prose written from evidence. Those are your job. The
LLM runs at **build** time only; consumers query committed static files with no
licence, so everything you write is validated and committed.

**Before a first build, or when judging what to ingest, read**
`docs/building-a-knowledge-store.md` in this library: estate definition,
what extraction yields per content type, gating, refresh economics and the
traps. This skill is the mechanics; that document is the judgement.

## Setup

```bash
pip install hmcts-knowledge-store-builder   # or: pip install git+https://github.com/hmcts/knowledge-store-builder.git@main
pip install graphifyy                       # graph extraction
gh auth status                              # discovery needs an authenticated gh
knowledgestore                              # lists every stage in run order
```

If the store repository has a settings file (commonly `config/pipeline.sh`),
source it first — it carries estate branding such as the page title.

## Full build

```bash
knowledgestore discover        # config/repositories.txt from the GitHub org
knowledgestore sync            # clone/update every repository
knowledgestore export-history  # per-repository commit datasets
knowledgestore context         # knowledge_context.md + manifest
knowledgestore intent          # file -> ticket index, descriptions from commits
```

Then build the graph with the graphify skill (`/graphify .`, or
`/graphify . --update` to refresh), and enrich it:

```bash
knowledgestore gherkin         # features, scenarios, ticket links into the graph
knowledgestore explorer        # the self-contained search page
```

Stages are independent and idempotent — re-run one without repeating the rest.

### Adding repositories to an estate

Edit `config/repository-filters.txt` (include prefixes, explicit repositories,
exclusions; archived are always excluded), then re-run `discover` and `sync`.
Adding repositories changes clustering, so community ids move: see "when
clustering changes" below before regenerating summaries.

## Writing community summaries

Summaries are plain-English descriptions of each cluster. They are what the
explorer shows as "what these areas do", and they are also a retrieval surface,
so their vocabulary matters.

```bash
knowledgestore summaries extract    # -> knowledge/summaries/communities-input.json
```

Each digest carries an id, label, size, repositories, top nodes with source
files, business features and tickets. Then:

1. **Chunk the work.** Sort digests by size (largest first) and split into
   batches of about 50. Prioritise clusters that involve newly added
   repositories, then the largest remaining.
2. **Dispatch one subagent per batch, in parallel** — a single message with
   several agent calls. Give each the digest file path, an output path, and the
   rules below.

   **Wait for every agent to report before merging — not for its output file to
   exist.** An agent writes its JSON, then validates it, and may rewrite it. A
   file therefore appears on disk before the agent has finished with it, so a
   merge gated on "all N files present" reads some of them mid-write. This has
   happened: 141 valid summaries out of 502 were silently left out, and the only
   reason it surfaced was comparing `merge`'s "361 merged" against the 502 the
   agents had written. Re-running the merge after all agents reported took every
   one of them.
3. **Merge and validate:**

   ```bash
   knowledgestore summaries merge <written-01.json> <written-02.json> ...
   ```

   `merge` rejects unknown cluster ids and out-of-range lengths, and reports
   what it rejected. It is the guardrail — read its output, and **reconcile the
   count it merged against the count the agents wrote**. "N merged" alone does
   not tell you N was everything; the difference is where the defects hide.

Rules to give each subagent, verbatim in spirit:

- One paragraph per digest, 120–600 characters, plain prose, no markdown.
- Describe what the cluster **is** and **does**, in business or architectural
  terms, in the estate's own language (British English for HMCTS estates).
- Base every claim only on the digest: node names, paths, repository names,
  feature names. Interpreting what a field or class name implies is fine;
  inventing behaviour the names do not show is not.
- Name the repository. If the top nodes are schema properties, say it is
  schema or contract content. If tests dominate, say it is test coverage.
- Write the output as one JSON object `{"<id>": "<summary>"}` covering every
  digest id in the batch, and nothing else.

**Verify grounding, not only coverage.** A subagent given 45 digests returns 45
summaries: right length, ids matching, merge accepted, coverage green — and any
number of them may describe behaviour the digest does not show. Every mechanical
gate measures shape, not truth, and a subagent's report is evidence that it
believes it finished, not that it was right. **The dispatching agent verifies;
this is not delegable to the author.** Cheapest effective checks: set-difference
the identifiers in each summary against its digest (anything in the prose but not
the evidence is fabrication or paraphrase); grep for speculation words
(*probably*, *likely*, *appears to*) which mark where evidence ran out; and read
a random 20–30 against their digests claim by claim. See
`docs/grounding-and-verification.md`.

```bash
knowledgestore summaries verify --sample 200   # grounding, after merging
```

Compares the identifiers each summary cites against those its digest contains,
and flags speculation words. `--strict` for CI. Read
`docs/grounding-and-verification.md` for what a finding does and does not mean —
it is a starting point for inspection, not a defect count.

**Verify coverage before merging** when several agents ran: every digest id in
every batch must appear in exactly one output file. Agents writing to the wrong
path is the failure mode to check for.

### When clustering changes

```bash
knowledgestore summaries snapshot   # BEFORE re-clustering
# ... add repositories, merge, re-cluster ...
knowledgestore summaries remap      # AFTER: carries summaries onto the new ids
```

`remap` carries a summary only where the new cluster holding most of its old
members holds at least 60% of them (`--bar` to change), drops it otherwise
rather than risk prose on the wrong cluster, and prints retention so the cost of
the re-cluster is a measured number. It refuses to run on a wrong snapshot (no
shared node ids) or an implausibly small summary set (`--floor`), because both
failures silently produce an empty file over a good one.

Whatever `remap` drops is then a backfill: extract digests again and author the
uncovered clusters.


Community ids are not stable across re-clustering, and summaries are keyed by
id. After a re-cluster, do **not** assume the old file still applies. Either
regenerate, or remap by membership overlap: for each old cluster, find the new
cluster holding most of its members and carry the summary across only if the
overlap is convincing (60% is a reasonable bar). Drop the rest rather than risk
prose attached to the wrong cluster.

## Writing topic briefs

A topic brief is a pre-written, analyst-grade answer to a question people
actually ask, served whole when the question hits the topic's keywords.

Declare topics in `config/topics.txt` — `slug | title | comma-separated
keywords`. Keep this **demand-driven**: add a topic when someone asked a
question the evidence cluster answered badly, not by guessing upfront.

```bash
knowledgestore topics extract   # -> knowledge/topics/topics-input.json
```

Write `docs/topics/<slug>.md` from each dossier, then:

```bash
knowledgestore topics merge     # validates and renders to briefs.json
knowledgestore explorer         # embed into the page
```

Brief structure that works:

1. `# Title`, then a **headline verdict** paragraph — the direct answer.
2. `## How it works` with numbered `###` mechanisms, each citing repositories
   and source files in inline code.
3. `## Where it lives` — a table of repositories and their roles.
4. `## Change history` — a table of the most informative tickets with dates and
   what each tells us, plus a sentence on the overall pattern.
5. `## What this is NOT` — corrections of likely misconceptions, each grounded
   in evidence. Absence of evidence goes here as "no evidence of X", not
   "not X".
6. `**Sources:**` — the evidence basis and the graph-build-time caveat.

Only a constrained markdown subset renders: headings, paragraphs, bold, inline
code, flat bullet lists, pipe tables. No links, blockquotes, code fences or
raw HTML.

**Audit what you wrote.** Every ticket id, repository name and file path in a
brief must appear in that topic's dossier. Check it mechanically — extract the
citations and diff them against the dossier — before merging. State inferences
as inferences: if the dossier shows a workflow but not who performs it, do not
say "translated by humans".

## Writing a deep dive

A deep dive is a dossier on one repository — usually the one everybody already
suspects is the problem. The bundle gives you the evidence to confirm or
refute that suspicion; your job is the narrative.

```bash
knowledgestore deepdive extract <repo>    # loads the full graph; be patient
```

Write `docs/deep-dives/<repo>.md` from the bundle. Structure that works:

1. `# Deep dive: <repo>`, then a **headline verdict** paragraph, then a line
   stating what was measured: "Evidence measured at build `<short-sha>`,
   `<n>` tickets, sources synced `<date>`." The merge step **rejects a
   dossier that omits the short SHA.**
2. `## Scale and shape` — nodes, share of the estate, community spread.
3. `## What changes, and why` — churn leaders and the instability numbers
   (revert share, fix share) with sample tickets quoted.
4. `## Hidden coupling` — the co-change pairs, especially cross-concern ones
   (domain files coupled to build files); hotspots (high churn AND high
   degree) are the refactoring targets worth naming.
5. `## Coupling surface` — schema/event names other repositories also carry.
6. `## What this is NOT` — claims the evidence cannot support. Note that the
   graph holds no cross-repository call edges, so blast radius must come from
   the coupling surface, never asserted from graph edges.
7. `**Sources:**` — the bundle path and the graph-build caveat.

Only the constrained markdown subset renders (headings, bold, inline code,
flat bullets, pipe tables). Base every number on the bundle — never re-derive
figures by hand, and never soften them either.

Then:

```bash
knowledgestore deepdive merge
knowledgestore explorer
```

## Checking a store's health

`knowledgestore status` reports provenance, summary/brief coverage, dangling
corpus citations and whether the page is older than a layer it embeds. Add
`--drift` to ask GitHub how far each repository has moved since the build
(one API call per repository). It never fails the build: drift is normal,
and the response to it is a refresh, not a red cross.

## The semantic index

```bash
pip install 'hmcts-knowledge-store-builder[semantic]'
knowledgestore semantic     # -> knowledge/semantic/ (committed)
```

A model embeds the graph's own vocabulary once, locally; only the
nearest-neighbour map ships. Regenerate after the vocabulary changes
materially — new repositories, or many new summaries.

## Finishing a refresh

```bash
knowledgestore explorer
node tests/explorer/estate-regression.mjs   # if the estate has one
```

Commit `knowledge/`, `knowledge_context.md`, `docs/topics/` and
`graphify-out/`. Never commit `repositories/`, `knowledge/git-history/` or the
uncompressed `graph.json` — all are regenerable and large.

Report honestly what was and was not regenerated: which stages ran, how many
summaries or briefs are still missing, and whether the estate regression
passed.
