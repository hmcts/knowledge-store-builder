# Retrieval design: how the store answers questions, and what it cannot do

This is the reasoning behind the pipeline. It explains how the store relates to
RAG and GraphRAG, why the LLM runs at build time only, and where each layer of
an answer comes from — including an honest account of the limits.

## Three phases, and which ones this replaces

Any question-answering system has three phases. This design keeps the first
two and deliberately replaces the third:

| Phase | Classic RAG | This store |
|---|---|---|
| **Ingest** | Chunk documents, embed to vectors, load a vector store | Extract code, specifications and commits into **typed nodes and edges** |
| **Retrieve** | Nearest neighbours in embedding space — what *sounds* similar | Term weighting plus **traversal of explicit edges** — what *is connected* |
| **Generate** | An LLM writes prose from retrieved chunks | **Deterministic composition**: templated answers and pre-written prose |

The representational difference matters more than the retrieval mechanics. RAG
ingestion flattens everything into opaque vectors: similarity survives,
structure is lost. Graph ingestion keeps the structure explicit — this
component calls that one, this feature exercises those steps, each edge marked
extracted or inferred with a source location.

That is why the explorer can state *"no edges connect these seventeen
same-named components; they are independent implementations"*. A vector store
cannot make that claim, because "relationship" is not something it stores.

This is GraphRAG's retrieval tier without its generation tier. Nothing is
wasted if a generation tier is added later — the graph is the retrieval backend
either way.

## The build-time / query-time split

The governing constraint: **consumers may have no LLM licence** — no API key,
no server, sometimes nothing but a browser. The governing insight: **whoever
builds the store does have one**. So intelligence moves into the build, and
everything it produces ships as committed static data.

```
                    BUILD TIME (maintainers)          QUERY TIME (anyone)
                    ────────────────────────          ───────────────────
repositories    ──► graph (AST parsers,               traversal, ranking and
+ commit history    no LLM)                           composed answers
+ specifications
                ──► community summaries               "what these areas do"
                    (LLM writes, reviewed,            prose, selected
                    committed)                        deterministically

                ──► semantic token index              query-term expansion
                    (embedding model over the         ("outcomes" finds
                    graph's own vocabulary)           "results") — pure lookup

                ──► ticket descriptions               business detail on every
                    (mined from commit messages)      ticket, with provenance

                ──► topic briefs                      served whole when a
                    (LLM writes analyst-grade         question hits a topic's
                    narrative from evidence)          keywords
```

Every LLM-authored artefact is validated on the way in: summaries must map to
real clusters and fall within length bounds; briefs must correspond to a
configured topic. A mismatched or invented batch is rejected, not merged.

### From graph to embeddings, specifically

1. The semantic stage collects the graph's own vocabulary — distinctive tokens
   from node labels, community summaries and feature names.
2. An embedding model runs **once, locally, at build time** and produces a
   vector per token.
3. Only the distillation is kept: each token's nearest neighbours above a
   cosine threshold, as a small committed JSON map.
4. At query time, expansion is a dictionary lookup. No model, no network, no
   licence.

**Why not embeddings in the browser?** A page opened from the filesystem
cannot fetch model assets, and shipping a model would dwarf the data. Token
expansion is the pragmatic trade: it bridges vocabulary gaps without
sentence-level semantics.

## What the graph is good for

- **Structural questions.** What connects to this, which repositories
  duplicate it, what would a change reach. Every answer cites file paths;
  fabrication is structurally impossible.
- **Duplication analysis.** Same-named nodes with no connecting edge are
  *proven* independent implementations.
- **Business behaviour**, through the Gherkin layer: features and scenarios are
  nodes in business language, wired to the code that implements them.
- **Why-questions**, through the intent layer: file → tickets → commit-mined
  descriptions with dates.
- **Impact and journey shapes**: two-hop reach, feature → scenario → step
  chains.

## What it is not good for

- **Free-form prose for unanticipated questions.** Outside the recognised
  question shapes, answers are evidence clusters. Topic briefs cover the
  questions you anticipate; genuinely novel ones need an agent.
- **Semantic search over intent.** Retrieval keys on vocabulary, not meaning.
  "How do we stop the wrong person being penalised?" will not find
  identity-matching code unless the words connect.
- **Per-commit history.** Deliberately outside the graph, kept as regenerable
  NDJSON alongside it.
- **Recency.** The store is a snapshot at its build commit.
- **Runtime truth.** Edges are static relationships from source. The graph
  knows nothing about deployment, configuration or production behaviour.

## Where each answer layer lives

| Layer | Artefact | Produced by |
|---|---|---|
| Graph | `graphify-out/graph.json` | graphify, plus the `gherkin` stage |
| Community summaries | `knowledge/summaries/communities.json` | `summaries` + an LLM at build time |
| Semantic token index | `knowledge/semantic/token-neighbours.json.gz` | `semantic` (local model) |
| Intent index and ticket descriptions | `knowledge/intent/*.json.gz` | `intent` |
| Ticket titles | `knowledge/intent/ticket-titles.json.gz` | `ticket-titles` (tracker CSV) |
| Topic briefs | `docs/topics/*.md`, `briefs.json` | `topics` + an LLM at build time |
| The consumer surface | `graphify-out/explorer.html` | `explorer` |
