# Creating and refreshing a knowledge store

Build a committed snapshot of a repository estate that people can query with
Claude Code, `graphify query` or a self-contained browser page. The pipeline
extracts code structure, specifications and commit history; the build skill
adds evidence-grounded prose where deterministic extraction is not enough.

## Choose what to do

| You want to | Start with |
|---|---|
| Build a store for the first time | [Install the prerequisites](#install-the-prerequisites), then [Create and define the store](#create-and-define-the-store) |
| Bring an existing store up to date | [Refresh a store](#refresh-a-store) |
| Change the library release used by a store | [Update the library version](#update-the-library-version) |
| Ask questions of a store someone else maintains | [Asking questions of a knowledge store](asking-questions.md) |

For a command-only reference, see [CHEATSHEET.md](../CHEATSHEET.md). Read
[Building an effective knowledge store](building-a-knowledge-store.md) before
deciding what belongs in a new estate; this guide covers the mechanics.

## Install the prerequisites

Install the Knowledge Store plugin by following
[Install the plugin](asking-questions.md#install-the-plugin). The
`/knowledge-store:knowledge-store-build` carries the stage order, clustering procedure and
authoring checks. The plugin is instructions; the library and command-line tools
run the pipeline on your machine.

You need Python 3.10 or later, Git and the GitHub CLI. On macOS, install them
with [Homebrew](https://brew.sh/):

```bash
brew install python git gh
```

The tools have separate roles:

| Tool | Role |
|---|---|
| `knowledgestore` | Pipeline stages and generated retrieval layers |
| `graphify` | Code extraction, graph merging and traversal |
| `gh` | Authenticated GitHub repository discovery |
| Knowledge Store plugin | Build procedure and evidence-authoring workflow |

## Create and define the store

A store is a working directory. It does not need to be a git repository until
you want to share its generated artefacts.

### Create the working directory

```bash
mkdir my-estate-knowledge
cd my-estate-knowledge
python3 -m venv .venv
source .venv/bin/activate
```

### Install the pipeline

```bash
pip install --extra-index-url \
  https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/ \
  hmcts-knowledge-store-builder
pip install graphifyy
knowledgestore --help
```

The `hmcts-lib` Azure Artifacts feed serves this package anonymously even though
opening the feed root in a browser prompts for sign-in. Credentials are needed
to publish packages, not to install them.

### Select the repositories

Create `config/repository-filters.txt`:

```bash
mkdir -p config
cat > config/repository-filters.txt <<'EOF'
# Comments must be on their own line.
prefix myteam-service-
prefix myteam-ui-
repo shared-component-library
team my-github-team
exclude myteam-service-deprecated
EOF
```

Each rule has one value:

| Rule | Selects |
|---|---|
| `prefix <value>` | Every repository whose name begins with the value |
| `repo <name>` | One repository by exact name |
| `team <slug>` | Every repository owned by the GitHub team |
| `exclude <name>` | One repository to remove from the selection; exclusion wins |

Archived repositories are always excluded. Do not put a comment after a rule:
the parser treats the whole remainder of the line as its value, so an inline
comment prevents a match.

Discovery reads the GitHub API, so authenticate, name the organisation, then
resolve the filters:

```bash
gh auth status          # gh auth login first, if this fails
export KSB_GITHUB_ORG=my-org
knowledgestore discover
```

Review `config/repositories.txt` before continuing. Discovery warns when a
selecting rule matched nothing; `knowledgestore discover --strict` makes those
warnings fail CI.

## Build the store

Open Claude Code in the store directory and run `/knowledge-store:knowledge-store-build`. The
commands below show the stages it should run and the checkpoints you should
expect.

### Build the source layers

```bash
knowledgestore sync
knowledgestore export-history
knowledgestore context
knowledgestore intent
```

These stages clone or update the repositories, export commit datasets, describe
the estate and build the file-to-ticket index. Stages are independent and
idempotent, so a failed stage can be rerun. If `sync` reports any failed
repositories, fix them before continuing: it records successful repositories
and exits non-zero, leaving the estate incomplete.

Before graph extraction, complete the secret-scanning and untrusted-input gates
in [Building an effective knowledge store](building-a-knowledge-store.md#5-gate-before-you-build-not-after).

If you have a Jira CSV export containing `Issue key` and `Summary` columns, add
real ticket titles after `intent`:

```bash
knowledgestore ticket-titles path/to/export.csv
```

Repeat runs merge new or changed titles into
`knowledge/intent/ticket-titles.json.gz`.

### Build the graph

Raise graphify's size limit before working with a large estate:

```bash
export GRAPHIFY_MAX_GRAPH_BYTES=4GB
```

Extract each repository from inside its clone, without clustering, then merge
all per-repository graphs in one operation:

```bash
while IFS='|' read -r repo _; do
  case "$repo" in ''|\#*) continue;; esac
  ( cd "repositories/$repo" && graphify update . --no-cluster )
done < config/repositories.txt

graphify merge-graphs repositories/*/graphify-out/graph.json \
  --out graphify-out/graph.json
```

Reconcile the number of graphs produced with `config/repositories.txt` before
merging. A shell loop that skipped repositories can still exit successfully.

Do not run graphify at the store root or pass `repositories/<name>` from the
store root. The first route sees a near-empty ignored tree; the second writes
store-relative source paths that break the file-to-ticket join. Do not include
an earlier merged graph in `merge-graphs`, because node identifiers would be
namespaced twice.

Add business specifications after the merge:

```bash
knowledgestore gherkin
```

Cluster after `gherkin`, so features and scenarios participate in the same
communities as the code they describe. Follow the clustering procedure in
`/knowledge-store:knowledge-store-build` and verify that every node received a community
before continuing. Do not substitute `graphify cluster-only`: it can report
success without persisting the clustered graph when run at the store root.

### Add the retrieval layers

Community summaries give the explorer plain-English descriptions of graph
clusters. Extract their evidence, have Claude Code author the missing prose by
following the build skill, then merge and verify it:

```bash
knowledgestore summaries extract
knowledgestore summaries merge <written-01.json> [more.json ...]
knowledgestore summaries verify --sample 200
```

The [grounding and verification contract](grounding-and-verification.md)
governs authored content. A successful merge proves shape and coverage, not
that every statement follows from its evidence; the agent coordinating the
build verifies that before content enters the store.

Add the semantic token index when users need vocabulary bridging such as
“outcomes” finding “results”:

```bash
pip install --extra-index-url \
  https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/ \
  'hmcts-knowledge-store-builder[semantic]'
knowledgestore semantic
```

This downloads an embedding model at build time. Only the generated
`knowledge/semantic/token-neighbours.json.gz` ships; queries need no model or
network access.

Topic briefs and deep dives are demand-driven authored layers:

```bash
knowledgestore topics extract
# Write docs/topics/<slug>.md from its evidence dossier, then:
knowledgestore topics merge

knowledgestore deepdive extract <repo>
# Write docs/deep-dives/<repo>.md from its evidence bundle, then:
knowledgestore deepdive merge
```

Declare topics in `config/topics.txt` as
`slug | title | comma-separated keywords`. Add a topic or deep dive when a real
question needs it, rather than predicting demand. The build skill defines the
accepted Markdown and evidence checks.

### Build and check the page

Recompress the final graph after clustering or any later graph mutation, then
build the page:

```bash
gzip -n -c graphify-out/graph.json > graphify-out/graph.json.gz
knowledgestore explorer
knowledgestore status
```

`graphify-out/explorer.html` is the self-contained query page. `status` reports
provenance, layer coverage, dangling citations and whether the page predates a
layer it embeds. It always exits zero because drift and missing optional layers
are conditions to assess, not build failures; read the report.

## Refresh a store

Snapshot existing community membership before any re-clustering, then refresh
the deterministic layers:

```bash
source .venv/bin/activate
knowledgestore summaries snapshot
knowledgestore discover
knowledgestore sync
knowledgestore export-history
knowledgestore context
knowledgestore intent
```

Skip `summaries snapshot` only when the store has no summaries to preserve.
Review discovery and sync counts, then repeat [Build the graph](#build-the-graph)
against every configured repository. Re-clustering can change community IDs
even when the estate contains the same repositories.

After clustering, carry summaries onto new IDs by membership overlap:

```bash
knowledgestore summaries remap
knowledgestore summaries extract
```

Read the retention reported by `remap`. Author and merge the uncovered summary
digests, then run `summaries verify`. Regenerate the semantic index when the
graph vocabulary or summaries changed materially, refresh any affected topic
briefs and deep dives, recompress the graph, and rebuild `explorer.html`.

Finish by checking both store health and source drift:

```bash
knowledgestore status
knowledgestore status --drift
```

## Update the library version

Do this only when deliberately moving a store to another library release. Find
the version on the
[knowledge-store-builder releases page](https://github.com/hmcts/knowledge-store-builder/releases),
then change `X.Y.Z` in `requirements.in`. Keep the exact `==` pin: Sonar
requires dependencies to use an exact version.

```text
--extra-index-url https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/
--only-binary :all:
hmcts-knowledge-store-builder==X.Y.Z
```

Install `uv` if it is not already available. On macOS:

```bash
brew install uv
```

Recompile and install the lock file:

```bash
uv pip compile requirements.in --generate-hashes \
  --emit-index-url --emit-build-options \
  --output-file requirements.lock
pip install -r requirements.lock
```

The emit flags retain the package feed and binary-only setting in the generated
lock.

**The library and the plugin are versioned differently.** The library is released
and pinned; the plugin's skills install from this repository's `main` branch with
no version at all, so a store can pin one library release while running newer
skills. Neither is wrong — they are separate channels — but when a skill's
behaviour does not match its documentation, the library's release number is not
what to check.

To run an unreleased library change rather than a release, install from the
branch instead of the feed:

```bash
pip install git+https://github.com/hmcts/knowledge-store-builder.git@main
```

## Configure the store

Most settings have defaults. `KSB_GITHUB_ORG` is required for discovery.

| Variable | Default | Purpose |
|---|---|---|
| `KSB_ROOT` | current directory | Store root; `knowledgestore --root <path>` sets the same value for one command |
| `KSB_GITHUB_ORG` | none | GitHub organisation used by discovery |
| `KSB_TICKET_PATTERN` | uppercase project key and number | Ticket IDs recognised in commit subjects |
| `KSB_TICKET_BROWSE_URL` | none | URL prefix used to turn ticket IDs into links |
| `KSB_EXPLORER_TITLE` | `Estate Explorer` | Browser-page title |
| `KSB_BRIEF_REQUEST_URL` | none | Destination for “request a topic brief”; unset hides the link |
| `KSB_E2E_REPOS` | none | Repositories whose test code should be indexed as business documentation |
| `KSB_FEATURES_DIR` | `features/` | Feature-directory segment used to group Gherkin features |

The full set, including tuning thresholds, is defined in
[`src/knowledgestore/config.py`](../src/knowledgestore/config.py). A store can
keep its values in a file such as `config/pipeline.sh`; source it before each
build or refresh.

## Use BDD specifications

The `gherkin` stage reads `.feature` files wherever they occur and links their
features and scenarios to matching step definitions:

| Language | Default search | Recognised declaration |
|---|---|---|
| Java | `src/test/java/**/*.java` | `@Given("...")` and the other Cucumber annotations |
| Python | `**/*.py` | `@given("...")` from behave or pytest-bdd |
| TypeScript | `**/*.ts` | `Given("...", ...)` from cucumber-js |

Cucumber expressions, typed behave parameters, regular-expression groups and
quoted values are normalised before matching. Override
`config.STEP_DEFINITION_LANGUAGES` through the Python configuration API when an
estate uses another language or layout.

## Publish the store

A store becomes shareable when its generated static artefacts are committed.
At minimum, ignore these working files:

```gitignore
.venv/
repositories/
knowledge/git-history/
graphify-out/graph.json
```

Commit `config/`, `knowledge_context.md`, generated content under `knowledge/`
apart from `knowledge/git-history/`, authored material under `docs/`, and the
committed artefacts under `graphify-out/`, including `graph.json.gz` and
`explorer.html`. Do not commit repository clones or the uncompressed graph.

The browser page can be large, but committing it is what lets people without a
Claude licence or network access use the store. Report which stages ran, what
authored coverage remains, whether grounding checks passed and whether the
source-drift check is clean.

## Stage reference

| Stage | Main output | Purpose |
|---|---|---|
| `discover` | `config/repositories.txt` | Resolve the estate from reviewed filters |
| `sync` | `repositories/`, `knowledge/provenance.json` | Clone or update sources and record their commits |
| `export-history` | `knowledge/git-history/` | Create per-commit NDJSON and Markdown datasets |
| `context` | `knowledge_context.md`, `knowledge/repository-manifest.md` | Record how to interpret the estate and what was read |
| `intent` | `knowledge/intent/*.json.gz` | Link files to tickets and mine ticket descriptions |
| `ticket-titles` | `knowledge/intent/ticket-titles.json.gz` | Import real issue titles from CSV |
| `gherkin` | updated graph and labels | Add features, scenarios, ticket nodes and step-definition links |
| `summaries` | `knowledge/summaries/` | Extract, merge, verify and remap community prose |
| `semantic` | `knowledge/semantic/token-neighbours.json.gz` | Bridge vocabulary gaps at query time |
| `topics` | `docs/topics/`, `knowledge/topics/briefs.json` | Add pre-written answers to recurring questions |
| `deepdive` | `docs/deep-dives/`, `knowledge/deep-dives/` | Add a provenance-stamped repository dossier |
| `explorer` | `graphify-out/explorer.html` | Build the self-contained search and Q&A page |
| `status` | report only | Report provenance, coverage, citations, freshness and optional drift |

## Troubleshooting

| Symptom | Action |
|---|---|
| `No matching distribution found` | Install with the `hmcts-lib` feed. For a lock file, compile with `--emit-index-url`. |
| Discovery says a rule matched nothing | Check spelling and move any inline comment onto its own line. Use `--strict` in CI. |
| `sync` fails on branches that differ only by case | With Git 2.45 or later, run `git -C repositories/<repo> refs migrate --ref-format=reftable`, then retry. |
| The merged graph is unexpectedly small | Re-extract from inside each repository; do not run graphify at the store root. |
| A graph operation refuses a large file | Raise `GRAPHIFY_MAX_GRAPH_BYTES` before the operation. |
| `summaries remap` would discard most prose | Stop. Verify clustering coverage before remapping; a clustering command can report success without saving its result. |
| `status` says the page is older than an embedded layer | Run `knowledgestore explorer` again and commit the rebuilt page. |

## Where to go deeper

- `/knowledge-store:knowledge-store-build` is the operative build and refresh
  workflow, including clustering, parallel authoring and verification.
- [Building an effective knowledge store](building-a-knowledge-store.md) covers
  estate selection, extraction limits, security gates and refresh economics.
- [Grounding and verification](grounding-and-verification.md) defines what
  evidence-backed content means and how to check authored layers.
