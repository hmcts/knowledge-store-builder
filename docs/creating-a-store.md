# Creating a knowledge store

Build a committed snapshot of a repository estate that people can query with
Claude Code, `graphify query` or a self-contained browser page. The pipeline
extracts code structure, specifications and commit history; the build skill
adds evidence-grounded prose where deterministic extraction is not enough.

## Choose what to do

| You want to | Start with |
|---|---|
| Build a store for the first time | [Install the prerequisites](#install-the-prerequisites), then [Create and define the store](#create-and-define-the-store) |
| Bring an existing store up to date | [Refreshing a knowledge store](refreshing-a-store.md) |
| Change the library release used by a store | [Update the library version](refreshing-a-store.md#update-the-library-version) |
| Change pipeline settings or BDD support | [Configuring a knowledge store](configuring-a-store.md) |
| Ask questions of a store someone else maintains | [Asking questions of a knowledge store](asking-questions.md) |

For a command-only reference, see [CHEATSHEET.md](../CHEATSHEET.md). Read
[Building an effective knowledge store](building-a-knowledge-store.md) before
deciding what belongs in a new estate; this guide covers the mechanics.

## Install the prerequisites

Install the Knowledge Store plugin by following
[Install the plugin](asking-questions.md#install-the-plugin). The
`/knowledge-store:knowledge-store-build` skill carries the stage order,
clustering procedure and authoring checks. The plugin is instructions; the
library and command-line tools run the pipeline on your machine.

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

### Declare the boundary

Every "no evidence of X" a store reports means "no evidence of X in the
repositories this store holds". Declaring the boundary is how a reader can tell
those apart, and how a repository left out on purpose reads as a decision rather
than as a gap:

```bash
cat > config/estate-boundary.txt <<'EOF'
# Comments must be on their own line.
searched an internal forge, by hand - see the snapshot date below
unsearched a second internal forge no build machine can reach

active payments-api
not-used legacy-reporting
decommissioned old-batch-runner

alias payments.api payments-api
snapshot payments-api 2026-01-15
EOF

knowledgestore status
```

| Rule | Records |
|---|---|
| `searched <where>` | A source that contributed to this store beyond `KSB_GITHUB_ORG`, which the manifest already names |
| `unsearched <where>` | One known to hold estate code and not read |
| `active <name>` | A repository the estate rules live |
| `not-used <name>` | One that exists and is not used |
| `decommissioned <name>` | One that has been retired |
| `alias <other> <name>` | `<other>` is the same repository as `<name>` |
| `snapshot <name> <YYYY-MM-DD>` | When a hand-taken copy of an off-host repository was taken |

The file and every rule in it are optional. What the declaration never does is
claim completeness: a store may have enumerated every host and still be missing a
deployed service whose repository nobody has located, so
`knowledge/repository-manifest.md` states that the declaration says what the
estate knows about rather than what exists.

Three things to know before writing one:

- **A ruling is a decision, so nothing derives it.** `active` on a repository the
  store does not hold is the case worth writing down — `knowledgestore status`
  names it, because a question about that repository is currently answered as
  though it did not exist.
- **Put comments on their own line.** Unlike `config/repository-filters.txt`, a
  trailing comment here fails the build instead of quietly becoming part of a
  repository name.
- **An unreadable declaration stops `knowledgestore context`.** A file that
  silently rendered as "no boundary declared" would let an estate believe it had
  said something its own store denies.

Skip the file and the manifest says so, under **What this manifest does not
cover**. Nothing else in the pipeline changes.

Once the estate is synced, the same question runs the other way:
`knowledgestore gaps` ranks what the estate already depends on and does not
hold, and reads this declaration so a repository you ruled out reads as a
decision rather than as a candidate. See
[Decide what to ingest next](refreshing-a-store.md#decide-what-to-ingest-next).

## Build the store

Open Claude Code in the store directory and run
`/knowledge-store:knowledge-store-build`. The commands below show the stages it
should run and the checkpoints you should expect.

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

**Extract from inside each clone, never from the store root.** graphify's own skill
will tell you to run `graphify .` over the whole tree — follow the sequence here
instead. Two instructions describe this step and the one shipped with the tool is
the one that prints at you, so it tends to win by default; on a tree of
repositories it is the wrong one.

Running `graphify .` at the top of the store looks equivalent and is not, for
three reasons that only show up together:

- `repositories/` is in `.gitignore` (see *Publish the store* below), and graphify's
  detection honours `.gitignore`. From the store root it therefore finds the store's
  own handful of files and **builds a graph from almost nothing — successfully**.
  There is no error, and the finished store looks like a thin estate rather than a
  failed build.
- A single pass over a large corpus gives no per-repository progress, so a slow
  repository and a hung one are indistinguishable. One operator saw eight minutes
  at full CPU with no output and no way to tell which repository to blame.
- **It skips `merge-graphs`, which is what keeps node ids distinct between
  repositories.** `merge-graphs` prefixes each input graph with a unique repository
  tag; a single root-level pass has nothing to prefix, so declarations that share a
  path convention — `infrastructure/variables.tf` in every repository, or an import
  common to every service — collapse into one node and acquire edges belonging to
  all of them. Nothing errors, and the fused graph is confidently wrong.

Extract each repository from inside its clone, without clustering, then merge
all per-repository graphs in one operation:

```bash
while IFS='|' read -r repo _; do
  case "$repo" in ''|\#*) continue;; esac
  ( cd "repositories/$repo" && graphify update . --no-cluster )
done < config/repositories.txt

knowledgestore merge-inputs        # what the merge will read, and what it cannot account for

graphify merge-graphs repositories/*/graphify-out/graph.json \
  --out graphify-out/graph.json
```

Reconcile the number of graphs produced with `config/repositories.txt` before
merging. A shell loop that skipped repositories can still exit successfully.

`knowledgestore size-cuts` names every graph the merge glob finds, with its own
node and edge counts, and sizes the layer while it is still per-repository. See
[sizing the AST layer](building-a-knowledge-store.md#sizing-the-ast-layer-before-you-commit-to-it),
which matters most on an estate mixing symbol-level languages with
infrastructure formats. It does not compare against `config/repositories.txt`.

`knowledgestore merge-inputs` is what does that comparison. It names every graph
on disk that `config/repositories.txt` does not declare and
`knowledge/provenance.json` cannot date, and every declared repository with no
graph. Extraction is manifest-driven while the merge is directory-driven, so a
graph left behind by an abandoned refresh is still an input and merges without
anything saying so. Add `--strict` to fail a build on the first two.

Do not run graphify at the store root or pass `repositories/<name>` from the
store root. The first route sees a near-empty ignored tree; the second writes
store-relative source paths that break the file-to-ticket join. Do not include
an earlier merged graph in `merge-graphs`, because node identifiers would be
namespaced twice.

Expose the content set once graphify has scanned the corpus:

```bash
knowledgestore content-set
```

That commits `knowledge/corpus/content-files.txt`, the set of files the pipeline
itself classified as content, and the list a corpus search must read — the tree
also holds each clone's extraction cache and graph, its VCS pack files and any
vendored bundles, which on two measured estates outnumbered the corpus several
times over. The report names the directories holding no content, aggregated across
every repository holding one; excluding those with a `.graphifyignore` before
extracting is much cheaper than filtering after, because extraction persists in the
cache and in each clone's own graph where no later filter reaches it. Run it before
extraction for that reason, and again afterwards so the committed list matches the
final scan.

Do not hand-maintain an exclusion list instead. A list is a second model of what
the tool produces: correct the day it is written, and wrong by omission the next
time the pipeline emits something new.

The stage also names every cloned repository that contributed no content file, and
a non-zero count there is expected on a healthy estate. It refuses — non-zero exit,
neither artefact written — only when no clone contributed anything while clones are
present, which is what a scan that never saw the corpus produces.

Add business specifications after the merge:

```bash
knowledgestore gherkin
```

Cluster after `gherkin`, so features and scenarios participate in the same
communities as the code they describe. Follow the clustering procedure in
`/knowledge-store:knowledge-store-build` and verify that every node received a
community before continuing. Do not substitute `graphify cluster-only`: it can
report success without persisting the clustered graph when run at the store
root.

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

The remaining retrieval layers are optional. Add the semantic token index when
users need vocabulary bridging such as “outcomes” finding “results”:

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

Record which partitioner clustered the graph, recompress it after clustering or
any later graph mutation, then build the page:

```bash
knowledgestore record-clustering
gzip -9 -n -c graphify-out/graph.json > graphify-out/graph.json.gz
knowledgestore explorer
knowledgestore status
```

`record-clustering` writes `graphify-out/clustering-inputs.json`, naming the
partitioner the clustering environment offered: graphify uses graspologic's Leiden
where that library imports and networkx's Louvain where it does not, and that
choice decides every community id. Run it in the environment that clustered, and
commit it — `status` reads it to report when an environment cannot reproduce the
committed clustering, and says **unknown** rather than agreement where no build
recorded one.

`graphify-out/explorer.html` is the self-contained query page. `status` reports
provenance, layer coverage, dangling citations, the recorded partitioner and
whether the page predates a layer it embeds. It always exits zero because drift and missing optional layers
are conditions to assess, not build failures; read the report.

## Publish the store

A store becomes shareable when its generated static artefacts are committed.
At minimum, ignore these working files:

```gitignore
.venv/
repositories/
knowledge/git-history/
graphify-out/graph.json
```

Ignoring `repositories/` is why the graph must be built from inside each clone
rather than from the store root: extraction honours `.gitignore`, so a root-level
build sees an ignored corpus as an absent one and succeeds against what is left.
See *Build the graph* above.

Commit `config/`, `knowledge_context.md`, generated content under `knowledge/`
apart from `knowledge/git-history/`, authored material under `docs/`, and the
committed artefacts under `graphify-out/`, including `graph.json.gz` and
`explorer.html`. Do not commit repository clones or the uncompressed graph.

The browser page can be large, but committing it is what lets people without a
Claude licence or network access use the store. Report which stages ran, what
authored coverage remains and whether grounding checks passed.

## Troubleshooting

| Symptom | Action |
|---|---|
| `No matching distribution found` | Install with the `hmcts-lib` feed. Installing from a lock compiled without `--emit-index-url` needs `--extra-index-url` passed as well. |
| Discovery says a rule matched nothing | Check spelling and move any inline comment onto its own line. Use `--strict` in CI. |
| `sync` fails on branches that differ only by case | With Git 2.45 or later, run `git -C repositories/<repo> refs migrate --ref-format=reftable`, then retry. |
| The merged graph is unexpectedly small | Re-extract from inside each repository; do not run graphify at the store root. |
| A graph operation refuses a large file | Raise `GRAPHIFY_MAX_GRAPH_BYTES` before the operation. |
| `status` says the page is older than an embedded layer | Run `knowledgestore explorer` again and commit the rebuilt page. |

## Where to go deeper

- [Refreshing a knowledge store](refreshing-a-store.md) covers source updates,
  summary remapping, drift checks and library upgrades.
- [Configuring a knowledge store](configuring-a-store.md) lists pipeline
  settings, BDD support and stage outputs.
- `/knowledge-store:knowledge-store-build` is the operative build workflow,
  including clustering, parallel authoring and verification.
- [Building an effective knowledge store](building-a-knowledge-store.md) covers
  estate selection, extraction limits, security gates and refresh economics.
- [Grounding and verification](grounding-and-verification.md) defines what
  evidence-backed content means and how to check authored layers.
