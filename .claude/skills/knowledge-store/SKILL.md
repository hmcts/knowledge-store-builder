---
name: knowledge-store
description: Query a committed knowledge store built by knowledge-store-builder — architecture, duplication, dependencies, user journeys, business intent, "which repo does X", "why does this exist". Use whenever the working directory contains graphify-out/graph.json or graph.json.gz, or when the user invokes /knowledge-store. Handles setup (decompressing the graph, installing the graphify CLI) automatically.
---

# Querying a knowledge store

A knowledge store is a committed graph of a repository estate plus retrieval
layers: business features from Gherkin, a file-to-ticket intent index,
community summaries, and pre-written topic briefs. Everything needed is in the
clone. **Never rebuild the store to answer a question** — the committed store
is the product.

## Step 1 — Locate the store

The store lives in the working directory, not in this skill. Confirm
`graphify-out/graph.json` or `graphify-out/graph.json.gz` exists. If not, ask
the user to `cd` into their store repository.

## Step 2 — Setup (run silently, only what is missing)

```bash
[ -f graphify-out/graph.json ] || gunzip -k graphify-out/graph.json.gz
command -v graphify >/dev/null 2>&1 \
  || uv tool install graphifyy -q 2>/dev/null \
  || pip install graphifyy -q
```

## Step 3 — Read the written answers first

Before traversing, check whether the question is already answered:

- `docs/topics/` — topic briefs: analyst-grade answers to estate-level
  questions, written from graph evidence and reviewed. If one covers the
  question, answer from it and cite it.
- `docs/journeys/`, `docs/findings/` — narrative write-ups, where the estate
  keeps them.
- `knowledge_context.md` — how this particular estate should be interpreted.

## Step 4 — Query the graph

```bash
graphify query "<the user's question>"     # breadth-first context
graphify query "<question>" --dfs          # trace one path instead
graphify path "ConceptA" "ConceptB"        # shortest path between two nodes
graphify explain "NodeName"                # a node and its connections
graphify affected "NodeName"               # reverse impact analysis
graphify god-nodes --top 20                # most connected abstractions
```

`graphify-out/graph.json` is NetworkX node-link JSON (`nodes` + `links`), so
Python one-liners are the right tool for aggregate questions — counts per
repository, degree ranking, community sizes.

### Interpretation rules

- Every node carries `repo`. **Always say which repository a finding is in.**
- Same-named nodes in different repositories are **independent
  implementations** unless an edge connects them. This is the store's most
  valuable finding and the easiest to get wrong.
- Cite `source_file` / `source_location` for specific claims.
- Answer only from what the graph returns. If the graph lacks it, say so.
- Absence of evidence is itself a finding — report it as one.

## Business intent: "why does this code exist?"

Two layers connect code to intent:

1. **Gherkin features as nodes** (`metadata.kind: gherkin_feature`), wired to
   their scenarios, ticket nodes and the step definitions they exercise. Walk
   from a component up to the behaviours it implements.
2. **The intent index** — which tickets' commits touched each file:

   ```python
   import gzip, json
   index = json.load(gzip.open('knowledge/intent/file-tickets.json.gz', 'rt'))
   index['<repo>']['<source file>']
   ```

   Descriptions mined from commit subjects are in
   `ticket-descriptions.json.gz`; real tracker titles, if imported, in
   `ticket-titles.json.gz`.

Recipe: graph neighbourhood of X → linked features (business language) →
intent-index tickets for X's source files → their descriptions. Quote commit
subjects and say so when no tracker title exists. **Never guess what a ticket
was about.**

## User journeys

If an estate has E2E suites, its page objects and step definitions *are*
scripted user journeys — their methods are the user's actions, in order.
Anchor journey questions there (`graphify explain "<PageObjectName>"`), then
map the owning feature module: routes and guards reveal preconditions,
per-outcome containers reveal decision branches.

Write it up as: screens in order → decision branches → side journeys → E2E
corroboration → what it connects onward to. State explicitly when screen order
is inferred from guards rather than read from routes. If the walkthrough is
worth keeping, offer to save it under `docs/journeys/`.

## Questions the graph cannot answer

- **Per-commit history** ("when did this change, who changed it") is
  deliberately outside the graph. Regenerate the datasets and query them:

  ```bash
  knowledgestore sync && knowledgestore export-history
  jq -r 'select(.subject|test("<term>";"i")) | [.repository,.author_date,.subject] | @tsv' \
    knowledge/git-history/*/commits.ndjson
  ```

- **Runtime behaviour.** Edges are static relationships from source. The graph
  knows nothing about deployment, configuration or production.
- **Recency.** The store is a snapshot at the commit recorded in
  `graphify-out/GRAPH_REPORT.md`. Flag staleness if the question is about very
  recent changes.

## Honesty rules

- Never invent nodes, edges or tickets.
- Distinguish what the graph shows from what you infer from it, in the answer.
- Say when evidence is thin rather than filling the gap from general knowledge
  of the technologies involved.
