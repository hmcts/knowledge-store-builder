# Creating and refreshing a knowledge store

You own or maintain an estate — a fleet of git repositories — and want a
knowledge store for it, or want to keep an existing one fresh. This page gets
you from nothing to a first store, and names where the deeper material lives.

## What you need, and why it is on your machine

The plugin's build skill is instructions for Claude — it does not contain the
pipeline. The pipeline is this repository's Python library, and Claude drives
it by running `knowledgestore` commands locally. That is why a builder needs
tools installed where an asker needs none:

| Tool | Why |
|---|---|
| the library (`knowledgestore`) | every pipeline stage |
| [graphify](https://github.com/safishamsi/graphify) (`pip install graphifyy`) | graph extraction itself — this library prepares its inputs and enriches its output, it does not re-implement it |
| the GitHub CLI (`gh`), authenticated | repository discovery |
| the Claude Code plugin | the build skill: stage order, the authoring loops, the traps |

Install the plugin as in [Asking questions](asking-questions.md); builders
use the same install.

## Install the library

Releases are published to the **`hmcts-lib`** Azure Artifacts feed, not to
PyPI, so the feed has to be named explicitly. It reads anonymously — no
credentials, no HMCTS account:

```bash
pip install --extra-index-url \
  https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/ \
  hmcts-knowledge-store-builder
```

Opening the feed's root URL in a browser prompts for a sign-in, which makes it
look private. It is not: `pip` requests the per-package path, which serves the
distribution list and the artefacts without authentication. Credentials are
only needed to publish, and that is the release workflow's job.

For a store you will rebuild repeatedly, pin the version in a requirements
file and lock it, so every rebuild resolves the same way:

```
--extra-index-url https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/
--only-binary :all:
hmcts-knowledge-store-builder==X.Y.Z   # the release you mean: see the Releases page
```

then `uv pip compile --generate-hashes` for the lock. Two escape hatches:

```bash
pip install git+https://github.com/hmcts/knowledge-store-builder.git@main  # unreleased changes
pip install 'hmcts-knowledge-store-builder[semantic]'                      # the one optional extra: the embedding model
```

Everything else is Python standard library only, by design: the pipeline runs
anywhere Python does, with no supply chain to review.

One versioning asymmetry worth knowing: the library is released and pinned;
the plugin's skills ship from `main` with no version at all. A store can pin
one library release while running newer skills — neither is wrong, but when a
skill's behaviour does not match its documentation, the release number is not
the thing to check.

## Start a store

A store is a **working directory**, not necessarily a git repository. The
pipeline reads and writes files under one root and never requires the root to
be under version control. Make it a git repository when you want to share the
result — committing the outputs is how consumers get them — but that is a
publishing decision, not a build requirement.

```bash
mkdir my-estate-knowledge && cd my-estate-knowledge
mkdir config
cat > config/repository-filters.txt <<'EOF'
# One rule per line. Archived repositories are always excluded.
# A rule's value is the whole rest of the line, so a comment can only go on
# a line of its own -- a trailing one becomes part of the value and matches
# nothing.
prefix myteam-service-
prefix myteam-ui-
repo   shared-component-library
# everything a GitHub team owns:
team   my-github-team
exclude myteam-service-deprecated
EOF
```

`discover` warns about any selecting rule that matched no repository — a typo,
a renamed repository, or a rule whose value swallowed a comment. `--strict`
turns those warnings into a non-zero exit for CI.

## Run the pipeline

Every stage is independent and idempotent; re-run one without repeating the
others. `knowledgestore` with no arguments lists them in run order.

```bash
export KSB_GITHUB_ORG=my-org

knowledgestore discover         # config/repositories.txt, from the GitHub API
knowledgestore sync             # clone/update each repository
knowledgestore export-history   # per-repository commit datasets
knowledgestore context          # knowledge_context.md + repository manifest
knowledgestore intent           # file -> ticket index, descriptions from commits
```

Graph extraction runs next, **per repository, from inside each one** — never
from the store root, where ignore rules hide the very repositories you are
extracting and a near-empty graph results:

```bash
while IFS='|' read -r repo _; do        # repositories.txt is pipe-delimited
  case "$repo" in ''|\#*) continue;; esac
  ( cd "repositories/$repo" && graphify update . --no-cluster )
done < config/repositories.txt

graphify merge-graphs repositories/*/graphify-out/graph.json \
  --out graphify-out/graph.json
```

Then enrich, cluster and publish:

```bash
knowledgestore gherkin          # features, scenarios and ticket links into the graph
# cluster AFTER gherkin -- the knowledge-store-build skill covers this step,
# including the trap where a clustering tool reports success without persisting
knowledgestore explorer         # the searchable HTML page
```

If the store is shared through git — the normal way consumers get it — commit
`knowledge/`, `knowledge_context.md` and `graphify-out/`'s compressed forms.
Never commit `repositories/`, `knowledge/git-history/` or the uncompressed
`graph.json`: all are regenerable, and all are large.

## What each stage produces

| Stage | Output | What it gives you |
|---|---|---|
| `discover` | `config/repositories.txt` | The estate, resolved from your filters |
| `sync` | `repositories/` | Full clones (history export needs the blobs) |
| `export-history` | `knowledge/git-history/` | Per-commit NDJSON, one file per repository |
| `context` | `knowledge_context.md`, manifest | How to interpret the graph; what was read |
| `intent` | `knowledge/intent/*.json.gz` | File → tickets, and ticket descriptions mined from commit subjects |
| `ticket-titles` | `ticket-titles.json.gz` | Real issue titles, merged from a tracker CSV export |
| `gherkin` | (updates the graph) | Features, scenarios and ticket nodes in business language, linked to the step definitions implementing them (Java, Python and TypeScript) |
| `summaries` | `knowledge/summaries/` | Plain-English descriptions of each cluster |
| `semantic` | `knowledge/semantic/` | Token-neighbour map, so "outcomes" finds "results" |
| `topics` | `docs/topics/`, `briefs.json` | Pre-written answers to anticipated questions |
| `deepdive` | `knowledge/deep-dives/`, `docs/deep-dives/` | Evidence-grounded dossier on one repository: churn, instability, co-change coupling, hotspots |
| `status` | (report only) | Provenance, layer coverage, dangling citations, page freshness; `--drift` checks GitHub for commits since the build |
| `explorer` | `graphify-out/explorer.html` | One self-contained page: search and basic Q&A |

## The stages where you write

`summaries`, `topics` and `deepdive` need prose written from evidence, and
each is deliberately split so the LLM runs at **build** time, never at query
time — whoever builds the store may have a licence; the people querying it may
not. Everything the LLM produces is committed as reviewed static text.

```bash
knowledgestore summaries extract              # evidence digests, deterministic
# write the prose (in your coding agent, from the digests), then:
knowledgestore summaries merge written.json   # validated on the way in
```

`merge` rejects unknown cluster ids and out-of-range lengths — but shape is
all it can check. Three more sub-commands cover what shape cannot:

```bash
knowledgestore summaries verify --sample 200   # is the prose grounded in its digest?
knowledgestore summaries snapshot              # before re-clustering
knowledgestore summaries remap                 # after: carry summaries onto new ids
```

`verify` compares the identifiers each summary cites against those its digest
contains, because a confidently fabricated batch passes every length and id
check. `snapshot` and `remap` survive a re-cluster: community ids move, and
`remap` carries prose across by membership overlap, drops what it cannot
place, refuses an unclustered graph outright, and reports retention as a
number. See `grounding-and-verification.md`.

`topics` works the same way: `extract` gathers a per-topic evidence dossier,
you write `docs/topics/<slug>.md` from it, `merge` validates and renders it.

`deepdive` is scoped to one repository, and its `merge` rejects a dossier that
does not state which build it measured — churn figures go stale with every
commit, and a dossier that does not say when it was true misleads.

## Configuration

Every setting has a working default and an environment variable. The ones
that matter when adopting the library:

| Variable | Default | Purpose |
|---|---|---|
| `KSB_ROOT` | current directory | Where the store lives |
| `KSB_GITHUB_ORG` | **required** | Organisation to discover repositories from |
| `KSB_TICKET_PATTERN` | `\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b` | How ticket ids look in commit subjects |
| `KSB_TICKET_BROWSE_URL` | (unset) | Prefix for ticket links; unset renders ticket ids as plain text |
| `KSB_EXPLORER_TITLE` | `Estate Explorer` | Page heading |
| `KSB_BRIEF_REQUEST_URL` | (unset) | Where "request a topic brief" links point |
| `KSB_MIN_ENTRY_DEGREE` | `3` | Minimum connections for a code entry to be indexed |
| `KSB_E2E_REPOS` | (none) | Repositories whose tests *are* the business documentation |
| `KSB_MIN_COMMUNITY_SIZE` | `25` | Smallest cluster worth summarising |
| `KSB_FEATURES_DIR` | `features/` | Directory whose next path segment names a feature's area |
| `KSB_EMBEDDING_MODEL` | MiniLM-L6-v2 | Model for the semantic stage |

From Python, `config.configure()` does the same and is what the tests use:

```python
from knowledgestore import config, build_explorer

config.configure(root="/path/to/store", EXPLORER_TITLE="Payments Estate")
build_explorer.main()
```

## BDD specifications

The `gherkin` stage reads `.feature` files — the Cucumber format, nothing
estate-specific — and links each feature to the code implementing its steps,
across languages, so a mixed estate works:

| Language | Where it looks | What it recognises |
|---|---|---|
| Java | `src/test/java/**/*.java` | `@Given("...")`, named by the enclosing class |
| Python | `**/*.py` | `@given("...")` (behave, pytest-bdd), named by module |
| TypeScript | `**/*.ts` | `Given("...")` (cucumber-js), named by module |

Steps are normalised before matching — Cucumber expressions (`{int}`),
behave's typed parameters (`{amount:d}`), regex groups and quoted values all
collapse to a placeholder — so the same business step matches whichever
language declared it. Add a language, or narrow a glob, via
`config.STEP_DEFINITION_LANGUAGES`.

## Where to go deeper

- **The `knowledge-store-build` skill** (from the plugin) — the mechanics and
  the traps: parallel authoring with subagents, verifying grounding not just
  coverage, the clustering step, and the failures that only surface at estate
  scale. This is what Claude follows when it builds alongside you.
- **[`building-a-knowledge-store.md`](building-a-knowledge-store.md)** — the
  judgement: defining an estate, what extraction yields per content type,
  what to gate before building, and what a refresh costs. Read it before
  standing up a new store.
- **[`grounding-and-verification.md`](grounding-and-verification.md)** — the
  contract that every claim in a store traces to evidence, and how to check
  it.
