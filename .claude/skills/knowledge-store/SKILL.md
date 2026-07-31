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

A store is a repository: the graph and its retrieval layers are committed
files, so they must be on disk. Work through this in order, and stay quiet
about steps that were already satisfied.

**1. Is it here?** If the working directory contains `graphify-out/`, use it.

**2. Has the user said before?** The environment variable wins; the file is
what this skill wrote the first time it asked.

```bash
[ -d "${KNOWLEDGE_STORE:-/nonexistent}/graphify-out" ] && echo "$KNOWLEDGE_STORE"
cat ~/.config/knowledge-store/locations 2>/dev/null
```

**3. Is it somewhere obvious?** One cheap look before troubling them:

```bash
find ~ -maxdepth 4 -type d -name graphify-out -not -path '*/node_modules/*' 2>/dev/null | head -5
```

**4. More than one candidate?** Someone may keep several estates. Identify each
before choosing — never guess which estate a question is about:

```bash
head -3 <candidate>/knowledge/repository-manifest.md 2>/dev/null
```

Ask which they mean, unless the question names an estate unambiguously.

**5. Otherwise ask.** Two questions together, then wait:

- do they already have a clone you have not found, and where?
- if not, which store should be cloned, and into which directory?

Do not clone into the current directory by default — people are usually sitting
in an unrelated project. An estate-specific skill (installed alongside this
one) normally supplies its own repository URL; without one, ask.

**6. Cloning cheaply.** A store's committed page and visualisation are large and
an agent never reads them, so take one commit and leave them behind:

```bash
git clone --depth 1 --filter=blob:none --sparse <store-repository> <directory>
cd <directory>
git sparse-checkout set graphify-out/graph.json.gz graphify-out/GRAPH_REPORT.md \
  graphify-out/.graphify_labels.json knowledge docs knowledge_context.md
```

On a large estate this is the difference between tens and hundreds of
megabytes. Someone who wants `explorer.html` in a browser needs a plain
`git clone` instead — offer that if they mention the browser page.

**7. Remember it, so this is asked once.** Append rather than overwrite: the
file is a list, because a user may query more than one estate.

```bash
mkdir -p ~/.config/knowledge-store
grep -qxF "$(pwd)" ~/.config/knowledge-store/locations 2>/dev/null \
  || printf '%s\n' "$(pwd)" >> ~/.config/knowledge-store/locations
```

Mention that `KNOWLEDGE_STORE` in their shell profile pins a default, which is
worth doing if they mostly query one estate.

## Step 2 — Prepare the store (run silently, only what is missing)

```bash
[ -f graphify-out/graph.json ] || gunzip -k graphify-out/graph.json.gz
command -v graphify >/dev/null 2>&1 \
  || uv tool install graphifyy -q 2>/dev/null \
  || pip install graphifyy -q
```

Offer `git pull` when the question concerns recent changes: a clone goes stale
silently, and `graphify-out/GRAPH_REPORT.md` records the commit the graph was
built from.

## Step 3 — Read the written answers first

Before traversing, check whether the question is already answered:

- `docs/topics/` — topic briefs: analyst-grade answers to estate-level
  questions, written from graph evidence and reviewed. If one covers the
  question, answer from it and cite it.
- `docs/deep-dives/` — evidence-grounded dossiers on individual repositories; if the question is about one repository's health, answer from its dive and cite it.
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
- **Recency.** When the question concerns recent changes, run `knowledgestore status --drift`
  (if the pipeline is installed) and report concretely — "the store predates
  9 commits to cpp-context-progression" — rather than a vague staleness caveat.

## Honesty rules

- **Every claim traces to evidence in the store** — node names, source paths,
  repository names, ticket ids, schema fields, commit subjects. Interpreting what
  a name implies is fine; asserting behaviour the evidence does not show is not.
  The test: could a reader check your claim against the same evidence and agree?
- **Say which layer answered**, because they differ in reliability: a committed
  brief or summary was LLM-authored against a specific build and goes stale
  silently; nodes and edges are mechanical. When prose and graph disagree, the
  graph wins and the prose is stale — say so rather than reconciling them.
- Never invent nodes, edges or tickets.
- Distinguish what the graph shows from what you infer from it, in the answer.
- Say when evidence is thin rather than filling the gap from general knowledge
  of the technologies involved.
