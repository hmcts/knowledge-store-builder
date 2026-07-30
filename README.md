# knowledge-store-builder

Build a committed, queryable knowledge store from a fleet of git repositories.

Point it at a GitHub organisation. It reads the source, the commit history and
the Gherkin specifications of the repositories you select, and produces a set
of static files: a graph of the estate, an index from every file to the tickets
that changed it, business features as first-class nodes, and a single HTML page
that searches and answers questions with no server and no LLM.

You commit the output. From then on, anyone who clones the repository can query
the estate — best through a coding agent, which can traverse the graph and
write a cited answer, and adequately through the HTML page if they have no
licence at all.

Built for the HMCTS Common Platform crime estate (93 repositories, 745k nodes),
where it answers questions like "which applications implement their own address
formatting, and which tickets changed them" without anyone reading the code.

## Install

```bash
pip install hmcts-knowledge-store-builder            # from the hmcts-lib feed
pip install 'hmcts-knowledge-store-builder[semantic]' # adds the embedding stage
```

The core has no dependencies beyond the standard library. Two external tools
are needed for the full pipeline:

- **[graphify](https://github.com/safishamsi/graphify)** builds the graph
  itself (`pip install graphifyy`). This library prepares its inputs and
  enriches its output; it does not re-implement extraction.
- **the GitHub CLI** (`gh`), authenticated, for repository discovery.

## Quickstart

Create a repository to hold the store, and tell it which repositories to read:

```bash
mkdir my-estate-knowledge && cd my-estate-knowledge && git init
mkdir config
cat > config/repository-filters.txt <<'EOF'
# One rule per line. Archived repositories are always excluded.
prefix myteam-service-
prefix myteam-ui-
repo   shared-component-library
exclude myteam-service-deprecated
EOF
```

Then run the pipeline. Every stage is independent and idempotent, so you can
re-run one without repeating the others:

```bash
export KSB_GITHUB_ORG=my-org

knowledgestore discover         # config/repositories.txt, from the GitHub API
knowledgestore sync             # clone/update each repository
knowledgestore export-history   # per-repository commit datasets
knowledgestore context          # knowledge_context.md + repository manifest
knowledgestore intent           # file -> ticket index, descriptions from commits

graphify .                      # build the graph (see graphify's docs)

knowledgestore gherkin          # add features, scenarios and ticket links
knowledgestore explorer         # build the searchable HTML page
```

Commit `knowledge/`, `knowledge_context.md` and `graphify-out/`. Do not commit
`repositories/` or `knowledge/git-history/` — both are regenerable, and both
are large.

`knowledgestore` with no arguments lists every stage in run order.

## What each stage produces

| Stage | Output | What it gives you |
|---|---|---|
| `discover` | `config/repositories.txt` | The estate, resolved from your filters |
| `sync` | `repositories/` | Full clones (history export needs the blobs) |
| `export-history` | `knowledge/git-history/` | Per-commit NDJSON, one file per repository |
| `context` | `knowledge_context.md`, manifest | How to interpret the graph; what was read |
| `intent` | `knowledge/intent/*.json.gz` | File → tickets, and ticket descriptions mined from commit subjects |
| `ticket-titles` | `ticket-titles.json.gz` | Real issue titles, merged from a tracker CSV export |
| `gherkin` | (updates the graph) | Features, scenarios and ticket nodes in business language |
| `summaries` | `knowledge/summaries/` | Plain-English descriptions of each cluster |
| `semantic` | `knowledge/semantic/` | Token-neighbour map, so "outcomes" finds "results" |
| `topics` | `docs/topics/`, `briefs.json` | Pre-written answers to anticipated questions |
| `explorer` | `graphify-out/explorer.html` | One self-contained page: search and basic Q&A |

## The two-part stages

`summaries` and `topics` are the only stages that need an LLM, and they are
deliberately split so the LLM runs at **build** time, never at query time:

```bash
knowledgestore summaries extract              # evidence digests, deterministic
# write the prose (in your coding agent, from the digests), then:
knowledgestore summaries merge written.json   # validated on the way in
```

`merge` rejects unknown cluster ids and out-of-range lengths, so a
hallucinated or mismatched batch cannot silently enter the store. `topics`
works the same way: `extract` gathers a per-topic evidence dossier, you write
`docs/topics/<slug>.md` from it, and `merge` validates and renders it.

This split is the point of the design. Whoever builds the store may have an
LLM; the people querying it may not. Everything the LLM produces is committed
as reviewed static text.

## Configuration

Every setting has a working default and an environment variable. The ones that
matter when adopting the library:

| Variable | Default | Purpose |
|---|---|---|
| `KSB_ROOT` | current directory | Where the store lives |
| `KSB_GITHUB_ORG` | `hmcts` | Organisation to discover repositories from |
| `KSB_TICKET_PATTERN` | `\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b` | How ticket ids look in commit subjects |
| `KSB_TICKET_BROWSE_URL` | HMCTS Jira | Prefix for ticket links in the page |
| `KSB_EXPLORER_TITLE` | `Estate Explorer` | Page heading |
| `KSB_BRIEF_REQUEST_URL` | (unset) | Where "request a topic brief" links point |
| `KSB_MIN_ENTRY_DEGREE` | `3` | Minimum connections for a code entry to be indexed |
| `KSB_E2E_REPOS` | two cpp-ui repos | Repositories whose tests *are* the business documentation |
| `KSB_MIN_COMMUNITY_SIZE` | `25` | Smallest cluster worth summarising |
| `KSB_EMBEDDING_MODEL` | MiniLM-L6-v2 | Model for the semantic stage |

From Python, `config.configure()` does the same thing and is what the tests
use:

```python
from knowledgestore import config, build_explorer

config.configure(root="/path/to/store", EXPLORER_TITLE="Payments Estate")
build_explorer.main()
```

## Claude Code plugin

The reusable skills ship here, so any store gets them:

```
/plugin marketplace add hmcts/knowledge-store-builder
/plugin install knowledge-store@knowledge-store-builder
```

- **`knowledge-store`** — query a store: setup, traversal recipes,
  interpretation rules (same-named nodes in different repositories are
  independent implementations unless an edge says otherwise), the business-intent
  and journey recipes, and what the graph cannot answer.
- **`knowledge-store-build`** — build or refresh one, including the two loops
  that need an LLM: writing community summaries from digests (chunked across
  parallel subagents, coverage-checked, validated on merge) and writing topic
  briefs from evidence dossiers.

Individual stores can add their own thin skill for estate specifics — which
repositories matter, where journeys are written up, what is known stale — and
leave the mechanics to these.

## Querying the result

The store is designed to be read two ways, and it is worth being explicit
about which is which:

**Through a coding agent — this is where the value is.** An agent can run
several traversals, cross-check what it finds against the commit history, and
write a cited narrative. Journeys, impact analysis, "why does this exist",
cross-repository comparisons: all of it belongs here. Point your agent at the
committed graph (`graphify-out/graph.json`, plain NetworkX node-link JSON) and
`knowledge_context.md`.

**Through `explorer.html` — for everyone else.** Open the file; there is
nothing to install and no network access. It searches the whole estate and
answers recognised question shapes (which repositories, where used, what is
impacted, why, journeys, ticket lookups) from pre-computed evidence, plus any
topic briefs you have written. It cannot compose prose for a question nobody
anticipated, and the page says so.

There is also `graphify query` from the terminal for deterministic traversal
without a licence.

## Design notes

- **The store is the product.** Outputs are committed static files. Consumers
  clone and read; nothing is built at query time.
- **Deterministic where it can be.** Extraction, indexing and page composition
  are pure functions of the sources; two runs of the same inputs produce
  byte-identical output.
- **Per-commit history stays out of the graph.** It is exported alongside as
  NDJSON, because "what changed last sprint" is a dataset query, not a graph
  traversal — and because it keeps the committed graph an order of magnitude
  smaller.
- **Absence of evidence is a finding.** Same-named components with no
  connecting edge are independent implementations, and the tooling says that
  rather than guessing.

`docs/retrieval-architecture.md` covers the retrieval design in full: how this
differs from vector RAG, what the graph is and is not good for, and where each
answer layer lives.

## Development

```bash
pip install -e .
python3 -m unittest discover -s tests -v     # 97 unit tests

node tests/explorer/engine-unit.mjs          # scorer maths
python3 tests/explorer/fixture.py            # build a page from a synthetic estate
node tests/explorer/page-regression.mjs      # drive every answer shape against it
```

The explorer page application is `src/knowledgestore/assets/app.js`, typed with
JSDoc and checked by `tsc --checkJs` in CI. It is inlined verbatim into the
built page, and the page regression asserts that byte-for-byte, so what the
tests exercise is what ships.

## Checks that run on every change

| Workflow | What it does |
|---|---|
| `tests` | ruff, unit tests, eslint, `tsc --checkJs`, the scorer tests and the page regression |
| `build` | builds the wheel and sdist, checks metadata, publishes to the `hmcts-lib` feed on main and on releases |
| `codeql` | security analysis of the Python pipeline and the JavaScript explorer application (auto-enables when the repository is public) |
| `secrets-scanner` | gitleaks over the full history, weekly and on every pull request |
| `dependabot-auto-merge` | merges grouped minor and patch bumps once checks are green; majors go to review |

Locally, `pre-commit install` gives the same lint and secret checks before a
commit is made (needs `gitleaks` on PATH).

## Licence

MIT. See [LICENSE](LICENSE).
