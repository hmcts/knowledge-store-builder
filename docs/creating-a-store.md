# Creating a knowledge store

Build a committed snapshot of a repository estate that people can query through
the knowledge-store MCP server (which assistants use), `graphify query` or a
self-contained browser page. The pipeline
extracts code structure, specifications and commit history; the build skill
adds evidence-grounded prose where deterministic extraction is not enough.

## Choose what to do

| You want to | Start with |
|---|---|
| Build a store for the first time | [Install the prerequisites](#install-the-prerequisites), then [Create and define the store](#create-and-define-the-store) |
| Bring an existing store up to date | [Refreshing a knowledge store](refreshing-a-store.md) |
| Change the library release used by a store | [Update the library version](refreshing-a-store.md#update-the-library-version) |
| Keep a lock that is written by hand rather than compiled | [A lock you maintain by hand](refreshing-a-store.md#a-lock-you-maintain-by-hand) |
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

You need Python 3.10 or later, Git 2.48 or later and the GitHub CLI. On macOS,
install them with [Homebrew](https://brew.sh/):

```bash
brew install python git gh
git --version                 # 2.48 or later
```

**Git 2.48 is a floor, not a recommendation.** On a case-insensitive
filesystem, `sync` converts a clone to the reftable ref format when its remote has
branches that differ only in case, and an older Git cannot read a converted
clone. Every machine and CI runner that reads the store's clones needs Git 2.48
or later, not only the one that ran `sync`. `sync`, `drift`, `export-history` and
`check-citations` refuse to start on an older Git. `knowledgestore --version` and
`knowledgestore status` report the Git they found.

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
  'hmcts-knowledge-store-builder[ast]'
knowledgestore --help
```

The `[ast]` extra installs graphify together with its `terraform` and `sql`
grammars, so a store takes graphify from this library and does not pin or install
it separately. Without those grammars graphify reads `.tf`, `.tfvars`, `.hcl` and
`.sql` files as nothing, with no error.

The `hmcts-lib` Azure Artifacts feed serves this package anonymously even though
opening the feed root in a browser prompts for sign-in. Credentials are needed
to publish packages, not to install them.

Confirm which install you got:

```bash
knowledgestore --version
```

That prints the version, the interpreter and the package directory. Read all
three whenever a run behaves as though it holds a version you did not install,
and capture all three in any check that asserts what is installed.

**Ask through the console script, never a bare `python3`.** A check reading
`python3 -c "import knowledgestore; ..."` answers for whichever `python3` sits on
`PATH`, which need not be the one that runs your pipeline. A
pre-commit hook can assert that the installed library matches its lock, from the
machine interpreter, while the virtualenv building the store holds a different
version — so it passes on an environment that never builds the store. No check
can close that gap, because a check is code some interpreter runs and inherits
whichever one ran it. `knowledgestore` is a console script inside the
virtualenv, so it can only answer for the environment that owns it.

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
knowledgestore apply-ignores
knowledgestore convert
knowledgestore check-corpus
knowledgestore export-history
knowledgestore context
knowledgestore intent
```

These stages clone or update the repositories, export commit datasets, describe
the estate and build the file-to-ticket index. Stages are independent and
idempotent, so a failed stage can be rerun. If `sync` reports any failed
repositories, fix them before continuing: it records successful repositories
and exits non-zero, leaving the estate incomplete.

**`apply-ignores` runs straight after `sync`.** `sync` ends in
`git clean -fd -e graphify-out`, which deletes every clone's `.graphifyignore`.
Declare the exclusions your store relies on in `config/graphify-ignore.txt`, one
`.graphifyignore` line each, and this stage writes them back into every clone:

```text
# every clone
node_modules/
# one repository: name | pattern
service-a | src/main/resources/static/vendor/
```

It appends only the lines a clone lacks and keeps the lines a repository commits
in its own ignore file, so running it twice changes nothing. `extract-ast` reads
the same file and refuses a clone that lacks a declared line. A store with no
`config/graphify-ignore.txt` gets no change from either.

**`convert` runs after `sync` and before any extraction.** Extraction reads PDF
and a wide spread of code and markup, and is blind to Office formats, so a
design document, a field mapping or an interface specification contributes its
filename and nothing else until this stage turns it into Markdown beside the
original. Those are the documents a store exists to answer questions from, so
skipping it does not fail the build — it produces a store that answers from
code alone and cannot say what it did not read.

**`check-corpus` reports harness configuration the corpus carries.** A cloned
repository may contain its own agent instructions — `CLAUDE.md`, `AGENTS.md`,
a `.claude/` directory — and those are estate content rather than instructions
to the agent building the store. The stage names what it found so you decide
before extraction reads it. It reports and exits zero; what to do about a
finding is yours.

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
  repository and a hung one are indistinguishable. A long run can sit at full CPU
  with no output and no way to tell which repository to blame.
- **It skips `merge-graphs`, which is what keeps node ids distinct between
  repositories.** `merge-graphs` prefixes each input graph with a unique repository
  tag; a single root-level pass has nothing to prefix, so declarations that share a
  path convention — `infrastructure/variables.tf` in every repository, or an import
  common to every service — collapse into one node and acquire edges belonging to
  all of them. Nothing errors, and the fused graph is confidently wrong.

Extract each repository from inside its clone, then merge all per-repository
graphs in one operation:

```bash
while IFS='|' read -r repo _; do
  case "$repo" in ''|\#*) continue;; esac
  ( cd "repositories/$repo" && graphify update . )
done < config/repositories.txt

knowledgestore merge-inputs        # what the merge will read, and what it cannot account for

graphify merge-graphs repositories/*/graphify-out/graph.json \
  --out graphify-out/graph.json
```

The loop produces each clone's `graph.json`, which is what `merge-graphs` reads.
It does not produce the AST layer `merge-layers` reads. `knowledgestore extract-ast`
writes that (`graphify-out/.graphify_ast.json`) and is the supported route to it;
see [Extract the AST layer](#extract-the-ast-layer).

**Do not add `--no-cluster` to that extraction**, however wasteful clustering a
repository looks when the merge is about to discard its communities. That much is
true, and the conclusion drawn from it is still wrong: the clustering path is also
where graphify resolves an edge's endpoints against the nodes it holds, and drops
the edges it cannot resolve as external or standard-library symbols. Skip it and
those edges survive naming nodes that do not exist, which `merge-graphs` then
materialises as nodes carrying an id and no content at all — material an authoring
pass later pays an LLM to summarise. Nothing fails; every stage reports success and
the graph is wrong. What removing the flag costs is real but smaller, and it
is language-dependent: `/knowledge-store:knowledge-store-build` measures both
directions and gives `knowledgestore dangling-endpoints` for measuring the trade on
your own estate.

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

### Write the detect result, then expose the content set

`content-set`, `chunk-plan` and `extract-ast` all read one artefact:
`graphify-out/.graphify_detect.json` at the store root, graphify's own record of
every file it classified. Nothing in graphify's CLI writes it. Write it, then
expose the content set derived from it:

```bash
mkdir -p graphify-out
python3 -c "import json; from pathlib import Path; from graphify.detect import detect; print(json.dumps(detect(Path('.'), gitignore=False)))" \
  > graphify-out/.graphify_detect.json

knowledgestore content-set
```

Three things about that command, each with a failure behind it:

- **`graphify detect` does not exist, and `graphify update .` does not write the
  file.** At the store root `graphify update .` exits 0, reports the graph it
  updated, and leaves no detect result — so an operator following the CLI finds
  nothing and the next stage refuses.
- **`gitignore=False` is what makes the corpus visible.** `repositories/` is in
  the store's `.gitignore`, and the scan honours ignore rules, so the default
  classifies the store's own handful of files and nothing in any clone.
- **`mkdir -p graphify-out` first.** The shell opens the redirect target before
  python runs, so an absent directory fails the command rather than the scan.

This is a classification scan at the store root, not graph extraction: it reads
the tree and writes one JSON file. Graph extraction still runs from inside each
clone, as above.

**What `gitignore=False` costs.** With `gitignore=False`, anything a repository
excluded only through its own `.gitignore` becomes content unless the store's
`.graphifyignore` re-excludes it — and that file is one forgotten reinstall away
from absent, because `sync` ends in `git clean -fd -e graphify-out`. It trades an
exclusion list each repository maintains for its own reasons for one this
pipeline has to remember to reinstall — which `apply-ignores` does from
`config/graphify-ignore.txt`, so write the detect result after it. Two measured
facts go with it, and a reader tends to assume the opposite of both:

- **`gitignore=False` does not disable `.graphifyignore`.** Measured in two
  independent sessions: with the parameter set, a `.graphifyignore` still
  excluded a bundled directory inside a clone while the rest of that clone was
  classified. The exclusion mechanism a store relies on survives the parameter.
- **For content a repository tracks, the parameter changes nothing.** A tracked
  file was never excluded by that repository's `.gitignore` and cannot be, so
  `gitignore=False` widens the scan only over what a repository generates and
  ignores. Anything committed that must not be extracted needs excluding
  explicitly either way.

**Where the scan does not complete, this route is unavailable.** Where the scan
does not finish at that scale, there is no substitute route to recommend: the
store has to produce `graphify-out/.graphify_detect.json` another way, and every
stage reading it takes it at face value, so nothing downstream can tell how it
was made. Say which route produced it.

`knowledgestore content-set` commits `knowledge/corpus/content-files.txt`, the
set of files the pipeline itself classified as content, and the list a corpus
search must read — the tree also holds each clone's extraction cache and graph,
its VCS pack files and any vendored bundles, which can
outnumber the corpus several times over. The report names the directories
holding no content, aggregated across every repository holding one; excluding
those with a `.graphifyignore` before extracting is much cheaper than filtering
after, because extraction persists in the cache and in each clone's own graph
where no later filter reaches it. Run it before extraction for that reason, and
again afterwards so the committed list matches the final scan.

Do not hand-maintain an exclusion list instead. A list is a second model of what
the tool produces: correct the day it is written, and wrong by omission the next
time the pipeline emits something new.

The stage also names every cloned repository that contributed no content file, and
a non-zero count there is expected on a healthy estate.

It refuses outright — non-zero exit, neither artefact written — in four cases:
`--top` below 1, which is a usage error; detect having classified no files at all;
clones being present while none of them contributed a content file, which is what
a scan that never saw the corpus produces; and a file in a format that holds
resolved secret values having reached the set.

That last one refuses rather than reports because a report arrives too late.
Extracted content persists in the extraction cache and in each clone's own graph,
so filtering the published graph afterwards reaches neither copy. Remove each named
file from the corpus and write the detect result again — or, where the estate has
ruled a named file safe, declare it in `config/content-set-allowed.txt` (one
store-relative path a line, `#` comments allowed) or pass `--allow <path>` for a
single run. Prefer the file: it keeps the record of every ruling the estate has
made, and a flag leaves none. With the graphify release this library is tested against, no file reaches that
refusal; it is a backstop, not evidence about your estate.

Add business specifications after the merge:

```bash
knowledgestore gherkin
```

Add JSON Schema documents, which graphify declines or keyword-walks:

```bash
knowledgestore json-schema --unresolved unresolved.json
```

It reads the content set, so [write the detect result](#write-the-detect-result-then-expose-the-content-set) first.
It adds a node per schema, definition and property with an edge for each `$ref`
it can resolve, and **removes graphify's keyword nodes for those files from the
merged graph**. Read the unresolved count in the report: `meta-schema` is
expected, while the others (`external`, `ambiguous`, `no-such-pointer`,
`unrecognised-target` and `malformed`) are references the store cannot follow, and `unresolved.json` lists each. Files that declare
the same schema id are `ambiguous` unless exactly one of them is in the referring
file's own repository, or exactly one is in a repository outside `KSB_FORK_REPOS`,
the setting `relationships` reads (below). The [JSON Schema layer](how-it-works.md#the-json-schema-layer) lists
what it recognises and the resolution order.

Write the relationships layer, which records which repositories depend on which:

```bash
export KSB_CHART_REPO_PATTERN='chart-{name}'   # optional: where a chart's repository is, by name
export KSB_UMBRELLA_REPOS=charts-bundle         # optional: repositories that bundle or wrap others' charts
export KSB_FORK_REPOS=lib-copy                  # optional: repositories that republish another's Maven or npm coordinates
knowledgestore relationships
```

It reads the clones and `knowledge/provenance.json`, never the graph, so its place
in the order is a convention. It prints one line per kind, giving the number of
edges, references, unresolved targets and skipped declarations. Read the
unresolved count: these are dependencies the store does not hold. Leave
`KSB_CHART_REPO_PATTERN` unset unless the estate names chart repositories by a
rule. A clone holding a Flux `HelmRelease` needs the `deploy` extra (PyYAML):
without it the stage exits 1 and names the file. The
[relationships layer](how-it-works.md#the-relationships-layer) lists the files it
writes and the resolution order.

To record the GitHub settings the build's token can read for the minimum-standard
layer, run the command below. It records archived, default branch, branch
protection, secret scanning, Dependabot alerts and organisation for each repository.
For each organisation it records whether its `.github` repository is public and,
when it is, the default `SECURITY.md` there and whether that file carries a contact.
GitHub applies the default only from a public `.github` repository.

```bash
knowledgestore minimum-standard-settings
```

It writes `knowledge/minimum-standard/github-settings.json` with the time it was
read. A setting the token cannot read is recorded as unreadable, never as off.

Then record the evidence for each repository against the standard's seven points.
Both stages run after `relationships` and before `index`:

```bash
knowledgestore minimum-standard
```

It reads the clones, the content-file list and that snapshot, and writes
`knowledge/minimum-standard/by-repo/<repo>.json` and `summary.json`. It prints the
repositories recorded and the count of points in each state. Run `content-set`
first: with no content-file list the stage exits 1 and writes nothing, because an
empty list would read every secret scan as clean. A missing snapshot is not a
refusal; the settings items read `not_determinable`, "no settings snapshot", and so
does `security_contact` for a repository with no SECURITY.md of its own, because its
organisation's default was not read.

To scope questions by product, declare them in `config/products.json`, a map from a
product name to repository names (`{"payments": ["repo-a", "repo-b"]}`). A malformed
file, or a name the store does not hold, stops the stage before it writes anything.

A repository's `absent` means the practice is not evidenced in it, not that the team
does not do it, and a clean secret scan reads `not_determinable`, never `present`.
Archived repositories are not in the store, so the layer says nothing about them.
[The minimum-standard layer](how-it-works.md#the-minimum-standard-layer) explains
what each point reads and why.

Then link every node to its file at the commit the store was built from:

```bash
knowledgestore source-urls
```

It writes a `url` onto each node whose repository is on github.com,
`https://github.com/<org>/<repo>/blob/<sha>/<path>#L<line>`, and reports the
nodes it could not link by reason. Answers cite that link. Without it they cite
`repositories/<repo>/...`, which is not committed, so no reader can open it.

Cluster after `gherkin`, so features and scenarios participate in the same
communities as the code they describe. Follow the clustering procedure in
`/knowledge-store:knowledge-store-build` and verify that every node received a
community before continuing. Do not substitute `graphify cluster-only`: it can
report success without persisting the clustered graph when run at the store
root.

### Extract the AST layer

```bash
pip install --extra-index-url \
  https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/ \
  'hmcts-knowledge-store-builder[ast]'
knowledgestore extract-ast
```

`extract-ast` writes `graphify-out/.graphify_ast.json`, the file `merge-layers`
reads. It extracts each repository from inside its clone and writes the shape the
per-repository `graphify update .` and `merge-graphs` route writes: node ids as
`<repo>::<id>`, a repository-relative `source_file`, and `repo` set, so the
file-to-ticket join matches and a store can switch routes without re-keying. A layer an
earlier release wrote carried store-relative paths and no `repo`; re-run `extract-ast`
to replace it. The per-file record `.graphify_ast_file_counts.json` keeps its
store-relative keys, so its baseline stays valid. The `[ast]` extra installs the third-party parser; the stage refuses to
run without it. It reads the content set from
`graphify-out/.graphify_detect.json`, so write that first, as above.

Run the AST route in this order, because each step reads what the one before it
left in the clones:

```bash
knowledgestore sync             # deletes every clone's .graphifyignore
knowledgestore apply-ignores    # writes config/graphify-ignore.txt back into each
# write the detect result and run content-set, as above
knowledgestore check-grammars
knowledgestore extract-ast
```

**It reconciles against `config/repositories.txt` before extracting**, on a first
run as on any other. A declared repository with no clone under `repositories/`
fails the run with exit 1, naming each one, and nothing is written. A declared
repository whose content set holds no parseable code is named on a warning line
and does not fail the run. A store with no `config/repositories.txt` is not
reconciled.

This is a behaviour change: a store that declares a repository it has not cloned
used to get a layer without it and exit 0, and now fails `extract-ast`. Run
`knowledgestore sync`, or remove the repository from the declaration.

That includes a repository whose first clone failed. `sync` reports the failure and
carries on, but the repository is still declared and still has no clone, so
`extract-ast` stays blocked until a later `sync` clones it or you remove it from
`config/repositories.txt`.

**It refuses a clone missing a declared exclusion.** When
`config/graphify-ignore.txt` exists, every clone's `.graphifyignore` must hold
each line declared for it; otherwise the run exits 1 naming the repository and
the line, and nothing is extracted. Run `knowledgestore apply-ignores`, then
write the detect result again.

It is the supported route to the AST layer. A hand-written per-repository loop
reaches the same extractor and lacks four properties this stage carries, each
from a defect that reached a real estate:

- **A time bound per repository.** `--timeout` (default 600 seconds, 0 disables)
  ends a repository that will not finish and names it. One whole-corpus call
  gives a hung run and a slow run the same silence.
- **One failure does not cost the others.** A repository that fails is named and
  counted, the rest still extract, the partial layer is written, and the exit
  code is non-zero so a hole is not committed as success.
- **It refuses the pipeline's own output.** Input from inside a `graphify-out`
  directory is refused, whichever route supplied the list. A hand-kept exclusion
  list missed that directory, and the graph then described itself.
- **It consumes the computed content set.** There is no exclusion pattern to
  maintain, so there is no second model to drift from the first. `--files`
  takes an explicit path list instead; `--detect` and `--out` move the input and
  the layer.

A run also compares each repository's node count with the last run and reports
decreases, absences and new repositories. It reports and does not refuse, because
deleted code is a legitimate reason for a smaller layer. The counts are written to
`graphify-out/.graphify_ast_counts.json` as each repository finishes, so an
interrupted run keeps every count it measured, and a repository that went
backwards says `down from N` on its own progress line.

**It fails on a file that went to zero.** Each content-set file's node count is
recorded in `graphify-out/.graphify_ast_file_counts.json`, by store-relative path.
A file that produced nodes last run and produces none now, while still in the
content set, is named with its previous count, and the run exits 1:

```text
  VANISHED  repositories/service-a/schema/order.json  (12 node(s) last run, 0 now; refused)
1 file(s) vanished, 12 node(s) lost.
```

The label says why when graphify did: `declined by graphify` for a file it lists in
`skipped_sources` (from 0.9.94), and `unread` for one in `failed_sources`. Both fail
the same way, because a lost file is lost whatever the reason.

When a loss is intended, such as a file whose content legitimately emptied, name it:

```bash
knowledgestore extract-ast --allow-vanished repositories/service-a/schema/order.json
```

Each named file is accepted and recorded as 0. Any other file that went to zero
still fails the run and keeps its previous count, so the next run fails again. A
file removed from the content set is not a vanished one, and a file that shrank
but kept nodes is left to the per-repository report above. The first run says it
has no record and writes one.

The record is only as good as the match between each node's `source_file` and a
content-set path. When fewer than half of the nodes that carry a `source_file`
match one, the stage refuses with `instrument mismatch` and writes no record. A
record of zeros would otherwise read as a clean baseline from which the guard could
never fire. A store root reached through a symlink is handled: graphify spells
those paths against the resolved root, and the stage rebuilds them.

**It refuses before extracting when graphify cannot parse a file type the content
set holds.** graphify reads `.tf`, `.tfvars`, `.hcl` and `.sql` files as nothing
when its grammar package is absent, with no error, so a green run proves nothing
about them. `extract-ast` probes the interpreter it imports graphify from and
exits non-zero, naming the missing grammar, how many files need it and the fix:
install the `[ast]` extra in that environment. Content sets with no such files
are never probed.

### Check the grammars before any store-owned AST route

```bash
knowledgestore check-grammars
```

A store that drives graphify itself, rather than through `extract-ast`, runs this
as the first step of its own driver. It reads the same content set as
`extract-ast` and applies the same refusal, with no extraction and nothing
written. It probes the interpreter that will run graphify, chosen most-specific
first, and prints which rule chose it and the path probed, whether it passes or
refuses:

1. `--interpreter PATH`, the python that will run graphify.
2. The repository's own `graphify-out/.graphify_python`, where graphify records
   its interpreter.
3. The store-level `graphify-out/.graphify_python`.
4. `sys.executable`, the interpreter running `knowledgestore`. It is never a
   `graphify` found on `PATH`, because a machine can hold several.

It exits 0 when no grammar is missing or none of those extensions occur, and
non-zero otherwise. `--detect` and `--files` move the input as they do for
`extract-ast`.

**A per-repository `graphify update` route runs `knowledgestore check-grammars
--clones` first.** The content set on disk comes from the previous extraction, so
after a resync that adds a repository or a file type it is stale, and the check
would miss exactly the new `.tf` and `.sql` files. `--clones` scans
`repositories/<name>` for every repository `config/repositories.txt` declares and
nothing else. The file list comes from graphify itself:
`graphify.extract.collect_files`, run inside the probed interpreter with the clone
as working directory, so each repository's `.graphifyignore`, vendored and
symlinked paths are treated as that graphify will treat them. The output names the
method beside the interpreter, either `graphify detection (exact)` or `library
exclusions + .graphifyignore parse (approximate)`. The second is the fallback for
an interpreter with no usable graphify: it prunes `.git` and `graphify-out` and
applies `.graphifyignore` with gitignore semantics, so its counts are
approximate - treat a refusal or a pass under it accordingly.

### Check each semantic chunk before merging

```bash
pip install --extra-index-url \
  https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/ \
  'hmcts-knowledge-store-builder[ast]'          # check-chunk needs graphify; it refuses without it
knowledgestore check-chunk --self-test          # once per install: proves every rule can fire
knowledgestore check-chunk --batch batch-07.json
```

`check-chunk` is the extraction spec's mechanical gate, shipped once. Give it to
every extraction agent instead of asking each to write its own: without it, an
agent spends most of its turns building and iterating that checker.
A batch file is `{"chunks": [{"n": 7, "out": "<chunk file>", "files": [...]}]}`,
the form a dispatcher hands an agent.

It checks shape, not truth, and holds each chunk to **its own** file list rather
than the batch's. The rules: the file parses; the top level is `nodes` and `edges`
lists; `file_type` and edge `relation` come from the spec's vocabularies, with
`contains` accepted; confidence bands and scores follow the rubric; every node and
hyperedge id equals its canonical form under graphify's `normalize_id`; no node id repeats
within one repository; no hyperedge id repeats anywhere in the run; no edge or
hyperedge names a node the chunk lacks; a hyperedge has at least three members and
a chunk at most three hyperedges; every `source_file` is one of the chunk's files;
no id ends in the chunk's own number (`_c0042` in chunk 42 does, the form code
`_c100` does not); and every file has a node.

One id in two repositories inside one chunk is not a duplicate: the merge keeps
both. Every rule sets the exit status, a truncated chunk is a `PARSE` violation
rather than a traceback, and violations print before the refusal of a batch naming
no chunks. `--batch` takes several files and compares hyperedge ids across all of
them.

`--self-test` breaks each rule on a synthetic batch and requires that rule, and no
other, to fire; seven negative controls must stay clean. Run it after installing,
so the gate an agent trusts is one you have seen fail.

### Keep your extraction brief's detail with headless workers

```bash
knowledgestore workers extract --brief extraction-brief.md --chunks 41,42,43
knowledgestore check-yield --chunks <directory of this wave's chunk files> \
  --baseline graphify-out/.graphify_semantic.json
```

`knowledgestore workers extract --brief` hands the store's extraction brief to every headless
worker, under the library's own rules, which it cannot override. `check-yield`
then compares nodes per file in the new chunks against the layer they would
replace, and exits 1 when the overall ratio is below `--floor` (default 0.5). A
chunk gate checks shape, not how much a file yielded, so this is the check that
catches a wave saying less than the layer it replaces. The build skill's
[headless route](../skills/knowledge-store-build/SKILL.md#the-headless-route-workers-extract)
covers both in full.

### Add the retrieval layers

Community summaries give the explorer plain-English descriptions of graph
clusters. Extract their evidence, have Claude Code author the missing prose by
following the build skill, then merge and verify it:

```bash
knowledgestore summaries extract
knowledgestore summaries batches --out-dir <empty directory>
knowledgestore summaries check-batch <batch_01.json> [more.json ...]
knowledgestore summaries merge <out-01.json> [more.json ...]
knowledgestore summaries verify --sample 200
```

`batches` writes one `{"batch", "out", "digests"}` file per `--size` (default 100)
significant communities that have no prose. Each authoring agent reads its batch,
writes `{"<id>": "<summary>"}` to the batch's `out` path, and runs `check-batch`
on it instead of writing a checker of its own. `check-batch` prints every
violation and exits 1 on any: an id missing, repeated or not in the batch, a
summary outside 60–700 characters or 2–4 sentences, or a cited identifier its
digest does not contain. The length bounds are `merge`'s and the grounding rule
is `verify`'s, so a batch that passes is one both accept. `merge` takes the `out`
files; handed a batch file it names the `out` file and refuses, and a run that
merges nothing writes nothing and exits 1.

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

### Serve the store's own written answers

A store's journeys, findings and design notes are usually a directory of
Markdown. The MCP server's `answers` tool reads them only as one JSON file of
entries, so write that file, then declare it as an extra layer:

```bash
knowledgestore answers-layer --markdown docs/answers --out knowledge/answers.json
```

```json
{
  "answers": {
    "path": "knowledge/answers.json",
    "description": "Written journeys and findings.",
    "kind": "answers"
  }
}
```

The declaration goes in `config/store-extra-layers.json`. Declare the JSON file,
never the Markdown directory: the server reports a directory as `not_read`.

Each `.md` file becomes one entry, keyed by its name without `.md`: its first
heading as `title` (else the file name), the body rendered as `html` with the
topic briefs' Markdown subset, and its store-relative path as `source`. A
fenced code block, such as a Mermaid diagram, is kept as written and escaped.
The stage names any file in the directory that is not `.md`, because it is not
served.

Written answers are point-in-time, so every entry carries `as_of` and `commit`.
`as_of` is the front matter's `date:` when the file has one, otherwise the date
of the last commit to the file, and `commit` is that commit's SHA, null for a
dated file not yet committed. Two runs at one commit write identical bytes.

The stage exits 1 and writes nothing when the directory holds no `.md` file,
when a file has neither a `date:` nor a commit, or when a file sits in a
subdirectory or has a space in its name — the server skips such an entry
without saying so. Run it before `manifest`.

### Build and check the page

Record which partitioner clustered the graph, recompress it after clustering or
any later graph mutation, then build the page:

```bash
knowledgestore record-clustering
gzip -9 -n -c graphify-out/graph.json > graphify-out/graph.json.gz
knowledgestore explorer
knowledgestore index
knowledgestore manifest
knowledgestore status
```

`manifest` runs last, after every stage that writes a layer, and writes
`knowledge/store-manifest.json`: which layers the store holds, where, and a
content digest for each. It refuses, writing nothing, when it cannot name the
store: set `KSB_STORE_ID`, or give the clone an `origin` remote. Commit the file
with the store. See [The store manifest](how-it-works.md#the-store-manifest).

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

### Build the query index

`knowledgestore index` writes `knowledge/index/store.sqlite`, a copy of the graph
and the ticket layer that a reader opens read-only and queries, instead of
parsing `graph.json.gz` into memory. It writes `knowledge/index/store.sqlite.gz`
beside it, and prints what it read against what it wrote, then both sizes. Run it
last, after the graph, `intent` and `source-urls` have settled, because it is a
copy of them. The schema and the queries it supports are in
[Query index](how-it-works.md#query-index).

Ignore the plain file and commit the archive, as the graph is handled:

```gitignore
knowledge/index/store.sqlite
```

The archive is what a store commits, because it is far smaller than the plain
file and GitHub refuses a file over 100 MB. The stage prints both sizes, so
measure your own. When the archive passes `KSB_INDEX_WARN_MB` (default 90), the
stage warns and still exits 0, because whether to commit it is the store's
decision. A store whose archive cannot stay under 100 MB should also ignore
`knowledge/index/store.sqlite.gz` and publish the index some other way.

A build needs temporary disk: staging tables live in a file under `TMPDIR`, not in
memory (`temp_store=FILE`), and the sort spills there too. Allow for that, plus
the finished `.building` file and its archive beside the output.

A missing graph, a truncated or corrupt graph or ticket layer, or a SQLite
without FTS5 exits 1 and writes nothing, leaving any existing index as it was. A
target that cannot be written exits 2.

### Check that answers cite real code

```bash
knowledgestore check-citations                     # docs/topics and docs/deep-dives
knowledgestore check-citations docs/answers/ a.md  # or name files and directories
knowledgestore check-citations --allow-none        # no answers yet is not a failure
```

Every backticked identifier in a published answer is looked up in the graph. Call
arguments are ignored, so `setValue(null)` resolves where the graph holds
`.setValue()`; a chain such as `ofNullable(...).ifPresent(...)` needs every member
to resolve. A path resolves when its directories are the trailing directories of a
real source file. An identifier that does not resolve is printed with the nearest
real one, and the stage exits 1. It exits 2, having asserted nothing, when there is
no graph or no answer to read, when `config/repositories.txt` cannot be parsed, or
when the answers cite a source link and there is no `config/repositories.txt` to
check it against.

A store whose gate chain runs `check-citations` before it has published any
answers passes `--allow-none`: with no answer to read, the stage prints the same
"nothing was asserted" message and exits 0 instead of 2. It excuses only that. An
answer that cites code the graph does not hold still exits 1, and every other
exit-2 case above still exits 2.

A source link, `https://github.com/<org>/<repo>/blob/<sha>/<path>#L<line>`, is
checked wherever the answer writes it: a markdown link, a bare link or a backticked
one. A bare link ends where GitHub's autolinker ends it, so `**<link>**` and
`<link>.` cite the link. Run the stage from the store root, or set `KSB_ROOT`,
because it reads the repository list and `knowledge/provenance.json` there. The
link fails, and the report says which claim is wrong, when:

- its path is not a `source_file` of that repository, nor a file the repository's
  clone under `repositories/<name>/` holds at the recorded commit: `unknown file`,
  with the nearest file the graph holds for that repository. Where no clone is
  present, or git cannot be asked, a path no node names cannot be decided and is
  listed as unchecked;
- its sha is not the commit provenance records for that repository, including a
  branch name or an abbreviated sha: `different sha`, naming the commit the store
  read. A link that is not pinned to a full sha reports this alone.

The line anchor is not checked. Any other URL is skipped.

A `table.column` citation is a third result. The graph holds code, so a database
column never resolves and never should. A source link to a repository the store
does not configure, such as a third-party library, is treated the same way. These
are counted and named, and do not change the exit code; read the list and decide.
Generated evidence is never read, and fenced code blocks are skipped. Only a fence
that opens at the start of a line and is closed counts: inside an indented fence,
or one never closed, links and backticked spans are checked like prose. A pass
shows the cited thing exists, not that the claim about it is true.

Stores that wrote their own gate should replace it with this stage rather than
importing `build_community_summaries._normalise`, which is private. The supported
name is `knowledgestore.check_citations.normalise`.

### Know what the command guards refuse

The plugin refuses five command shapes before Claude Code runs them, each a
mistake this library has documented as costly:

- `git clean -fd` inside a clone under `repositories/` without
  `-e graphify-out`, which deletes the untracked per-repo graphs.
- `git add -A`, `git add --all` or `git add .`, which stages generated output
  and other sessions' edits together.
- `graphify update` or `graphify extract` given a `repositories/<name>` path,
  which prefixes every `source_file` and silently breaks the file-to-ticket join.
- A checker piped into something and chained with `&&` to `git commit` or
  `git push`, where the pipeline's exit status hides the checker's.
- `graphify merge-graphs` before `knowledgestore merge-inputs` has run in the
  session.

These guards bind only a Claude Code session with the plugin installed. CI, a
person at a terminal, and any session without the plugin see nothing of them.
They are an early warning, not a gate: a fault in a guard lets the command
through, and they replace no check in CI or the library. They match command
text, so a command written another way is not caught.

## Publish the store

A store becomes shareable when its generated static artefacts are committed.
At minimum, ignore these working files:

```gitignore
.venv/
repositories/
knowledge/git-history/
graphify-out/graph.json
knowledge/index/store.sqlite
```

Ignoring `repositories/` is why the graph must be built from inside each clone
rather than from the store root: extraction honours `.gitignore`, so a root-level
build sees an ignored corpus as an absent one and succeeds against what is left.
See *Build the graph* above.

Commit `config/`, `knowledge_context.md`, generated content under `knowledge/`
apart from `knowledge/git-history/`, authored material under `docs/`, and the
committed artefacts under `graphify-out/`, including `graph.json.gz` and
`explorer.html`, and `knowledge/index/store.sqlite.gz`. Do not commit repository
clones, the uncompressed graph or the uncompressed index.

The browser page can be large, but committing it is what lets people without a
Claude licence or network access use the store. Report which stages ran, what
authored coverage remains and whether grounding checks passed.

### Add the server to the store plugin

Assistants answer a store's questions through the knowledge-store MCP server.
Declare it in the store plugin so an asker's assistant launches it with no setup
beyond the plugin. Where you declare it depends on where the plugin lives.

**The plugin root is the store repository's root (the usual case).** Declare the
server inline as `mcpServers` in `.claude-plugin/plugin.json`, beside the
plugin's own keys:

```json
{
  "name": "<store-name>",
  "description": "Questions about <the estate>, answered from the store.",
  "author": { "name": "<owner>" },
  "mcpServers": {
    "knowledge-store": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/hmcts/knowledge-store-mcp@v0.3.0",
        "knowledge-store-mcp", "--store-id", "<id>"
      ]
    }
  }
}
```

Do not create a `.mcp.json` at the repository root: it also loads as project
configuration for anyone working in the repository, and `check-plugin` refuses it.

**The plugin lives in its own subdirectory.** Create `<store-plugin>/.mcp.json`:

```json
{
  "mcpServers": {
    "knowledge-store": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/hmcts/knowledge-store-mcp@v0.3.0",
        "knowledge-store-mcp", "--store-id", "<id>"
      ]
    }
  }
}
```

`claude plugin validate` accepts either form, and `knowledgestore check-plugin`
reads either, so a store can verify the declaration before publishing.

Take `<id>` from the store's manifest, not from the plugin name:

```bash
jq -r .store.id knowledge/store-manifest.json
```

`knowledgestore manifest` writes that id, from `KSB_STORE_ID` or else the
repository name of the clone's `origin` remote.

Check the plugin before you publish it, and in CI beside `check-install-docs`:

```bash
knowledgestore check-plugin [--plugin <store-plugin>]
```

`--plugin` defaults to the store root. The stage reads `.mcp.json`, or
`mcpServers` in `.claude-plugin/plugin.json`, and exits 1 naming each reason when
the `knowledge-store` server is missing, the command is not `uvx`, `--from` is
not an exact `knowledge-store-mcp` tag, `--store-id` is absent or differs from
the manifest's `store.id`, or the plugin root is the repository root and a root
`.mcp.json` exists. A pinned tag older than the one this guide documents is a
warning. When everything agrees it prints the tag and the id and exits 0.

It also reads the committed manifest. It fails when the manifest does not
record the index and relationships layers as present, or the relationships layer
as recognised, because the server then answers from a fallback or not at all; a
layer recorded as `present: null` is a warning. When `KSB_STORE_ID` is set it
must equal the manifest's `store.id`, or the next manifest build writes an id the
plugin does not ask for. When it is unset the check
is skipped and says so on stderr (`build id not resolved (KSB_STORE_ID unset); its
check was skipped`), still exiting 0, so a CI step that loads no settings is not
mistaken for one that was checked.

- **Build and commit the manifest before you publish.** At v0.1.0 the server
  exits at start-up when it finds no registered clone or no
  `knowledge/store-manifest.json`, so an asker's clone needs the committed
  manifest. Run `knowledgestore manifest` as part of the build.
- **Run `knowledgestore source-urls` before `index`.** The server returns each
  node's `url` as the graph holds it and builds none, so a graph without them
  gives answers that cite store-relative paths instead of pinned links.
- **Askers need `uv`**, which provides `uvx`. The `knowledge-store` skill names
  it when it is missing.
- **The server finds the clone** through `~/.config/knowledge-store/locations`,
  which the skill writes, or through the `KNOWLEDGE_STORE_PATH` environment
  variable.
- **To upgrade the server**, change the tag in `.mcp.json` and publish the
  plugin. The tag is the only version a store pins.

Setup for other clients, such as Claude Desktop and GitHub Copilot, is in the
server's [front doors](https://github.com/hmcts/knowledge-store-mcp#front-doors)
section.

## Troubleshooting

| Symptom | Action |
|---|---|
| `No matching distribution found` | Install with the `hmcts-lib` feed. Installing from a lock compiled without `--emit-index-url` needs `--extra-index-url` passed as well. |
| Discovery says a rule matched nothing | Check spelling and move any inline comment onto its own line. Use `--strict` in CI. |
| `sync` fails on branches that differ only by case | `sync` converts the clone to reftable refs itself. If the failure says the clone is already reftable, the collision is not the cause; read Git's output above it. |
| A stage refuses because Git is older than 2.48 | Upgrade Git on that machine or runner. See [Install the prerequisites](#install-the-prerequisites). |
| The merged graph is unexpectedly small | Re-extract from inside each repository; do not run graphify at the store root. |
| A graph operation refuses a large file | Raise `GRAPHIFY_MAX_GRAPH_BYTES` before the operation. |
| `status` says the page is older than an embedded layer | Run `knowledgestore explorer` again and commit the rebuilt page. |

## Where to go deeper

- [Refreshing a knowledge store](refreshing-a-store.md) covers source updates,
  summary remapping, drift checks and library upgrades.
- [Moving to v0.18](moving-to-v0-18.md) takes an existing store, and its
  askers, onto answering through the knowledge-store MCP server.
- [Configuring a knowledge store](configuring-a-store.md) lists pipeline
  settings, BDD support and stage outputs.
- `/knowledge-store:knowledge-store-build` is the operative build workflow,
  including clustering, parallel authoring and verification.
- [Building an effective knowledge store](building-a-knowledge-store.md) covers
  estate selection, extraction limits, security gates and refresh economics.
- [Grounding and verification](grounding-and-verification.md) defines what
  evidence-backed content means and how to check authored layers.
