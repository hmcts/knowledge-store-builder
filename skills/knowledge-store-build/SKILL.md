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

**This skill assumes knowledge-store-builder 0.11.6 or newer.** Check before
following it. The skills and the library install separately - these instructions
come from the plugin cache, the library from pip - so the two drift, and the
symptom is a stage documented here reported as `unknown stage`:

```bash
knowledgestore --version        # older than 0.11.6? upgrade before following this
```

A store deliberately pinned to an older library is a legitimate position; then
follow the documentation for the version it pins rather than this text.

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

Then build the graph. **Do not run graphify at the store root.** `repositories/`
is gitignored in every store, and graphify's scan honours ignore rules, so a root
run sees only the store's own config and docs and produces a near-empty graph.
On one estate that produced a graph three orders of magnitude too small; only
graphify's overwrite guard stopped it replacing the store. Extract per
repository, from inside each one, then merge:

```bash
while IFS='|' read -r repo _; do      # repositories.txt is pipe-delimited
  case "$repo" in ''|\#*) continue;; esac
  ( cd "repositories/$repo" && graphify update . )
done < config/repositories.txt

graphify merge-graphs repositories/*/graphify-out/graph.json \
  --out graphify-out/graph.json
```

Extracting from inside the repository is what keeps `source_file` repo-relative,
which is what the file-to-ticket join is keyed on; `merge-graphs` adds the `repo`
attribute.

### Before dispatching semantic extraction

If the estate carries documents, papers or images, write the chunk plan first:

```bash
knowledgestore chunk-plan          # -> graphify-out/.graphify_chunk_plan.json
```

The dispatching agent reads it with `store_paths.load_plan()`, which resolves the
paths to absolute - the extraction spec requires agents to receive and echo paths
verbatim and absolute, while the committed file stores them relative to the store
root so nothing machine-specific is committed.

Write it even when dispatching from context anyway: **it is the only map from chunk
number to file list**, so without it a committed chunk archive cannot be read back.
The stage warns, with a count, if any path could not be made relative - which means
the corpus sits outside the store and the plan will not survive a clone.

**Choose `--kinds` before your first run.** The default plans `document,paper,image`,
because code is the AST layer's job and semantically re-extracting it pays twice for
the same nodes. But graphify classifies **YAML and Terraform as `code`**, so on an
infrastructure estate the default covers a quarter of the corpus - measured at 4,651
of 17,539 paths on one, where the interesting content is Flux Kustomizations, Helm
values and `variables.tf`. Such a store should pass `--kinds code,document`
deliberately.

**Expect many chunks smaller than the 20-25 graphify's skill mentions.** One
directory per chunk, never mixed, and `--chunk-size` is a maximum rather than a
target - so a three-file directory becomes a three-file chunk. The skill asks for
both "20-25 files" and "group files from the same directory together", which cannot
both hold; grouping wins here, because cross-file relationships are the reason the
semantic layer exists and padding a chunk with unrelated files asks an agent to
relate things that have no relation.

**Passing `code` is also a decision about fan-out cost.** Directory purity on an
estate whose code is spread thinly across deep trees produces many small chunks:
measured at 6,704 chunks averaging 3.3 files, against 762 averaging 22 for the same
corpus partitioned ad-hoc. That is roughly nine times the agent dispatches, which
interacts with the fan-out's own limits. Decide both together.

**Adopting this planner is a full re-archive.** On the one estate with an existing
plan, matching its path set exactly, only 799 chunks have identical membership - and
794 of those are single-image chunks, which agree by construction. Of its 762 text
chunks, **5 agree**. All the extraction value is in the text chunks.

Note what that estate's plan is, because it is the reason this stage exists: nothing
generates it. Eight scripts read it and none writes it - it was partitioned in an
agent's context during a build and never captured, so it cannot be regenerated,
audited, or even described reliably. Its own operator described it to me as
directory-grouped; it is not.

**Chunk numbering is the archive's only index.** If you change `--chunk-size`
between refreshes the numbering moves, and an archive of previous extractions is no
longer addressable by it. Decide the maximum once per estate.

### Merging the chunk extractions

Replace the skill's concatenation of `.graphify_chunk_*.json` with:

```bash
knowledgestore merge-chunks        # -> graphify-out/.graphify_semantic_new.json
```

Concatenation is wrong in two opposite directions at once, and both are silent.
Independent agents produce ids unique only *within* a chunk, so the same slug in two
chunks may be one entity or two - and **the label decides which of two opposite
actions is correct**. Concatenation merges both cases, re-pointing every edge at
whichever node won, which asserts relationships that were never in the corpus.
Conversely a genuinely shared entity that agents named by path gets one id per chunk
that saw it, so the cross-file linking the layer exists for is lost.

Read all eight counters, not the node total. `merged` and `consolidated` are the
linking working; `namespaced` is fabrication prevented; `fragmented` is the residue
of deliberately under-merging; `recovered` are cross-chunk edges a concatenation
would have thrown away; `ambiguous` and `dangling` are edges dropped rather than
guessed at.

Every renamed node carries `original_id`, and the stage refuses to write an id
carrying a chunk-derived suffix - the extraction spec forbids those outright, and
such an id changes on any re-plan.

### Merging the two layers

Replace the skill's concatenation of the AST and semantic layers with:

```bash
knowledgestore merge-layers        # -> graphify-out/.graphify_extract.json
```

Concatenation keeps the AST node when the two layers share an id and **keeps the
semantic layer's edges anyway**. Those edges still name the discarded id, which
now resolves to the AST node, so every relationship the semantic layer asserted
about one entity becomes an assertion about another. Nothing dangles and nothing
errors, because the id exists.

The extraction spec drops the file extension from the id stem, so a component and
its template are assigned one id by design - which is why this scales with how
much of a corpus is authored in paired files rather than occurring at random. One
estate measured 98 such collisions carrying 311 edges, every one between files
with different labels.

Read both collision counts, not the node total. `same label` is one entity both
layers found and is benign; `DIFFERENT labels` is the dangerous case, and the
stage keeps that semantic node under a new id rather than discarding it - so no
evidence is lost and no edge resolves against an entity it was never about. Every
renamed node carries `original_id`.

**Do not add `--no-cluster` here, however wasteful per-repository clustering
looks.** The merge does discard per-repository communities, so the reasoning is
sound and the conclusion is wrong: the clustering path also runs **symbol
resolution**, and skipping it leaves dangling edges that `merge-graphs` then
materialises as contentless nodes — `id`, `local_id`, `repo`, and nothing else.
Measured on one repository, same version, only the flag differing:

| | nodes | edges | dangling endpoints |
|---|---|---|---|
| `--no-cluster` | 1,288 | 2,711 | 166 |
| without | 1,288 | 2,236 | **0** |

Across that estate it produced **+6% nodes and +22% edges of material carrying no
content** — 49,506 contentless nodes with 339,159 edges pointing at them, almost
all JDK and test-library symbols (`assertthat`, `ioexception`, `mock`,
`loggerfactory`). Clustering would then have placed contentless nodes into
communities and billed an authoring pass to summarise them.

**What removing the flag costs, which is not nothing.** The clustered path drops
edges whose endpoints it cannot resolve, on the grounds that they are external or
standard-library symbols. That is right most of the time and **language-dependent**
— measured on two estates by matching dropped endpoint names against node labels:

| | estate A | estate B |
|---|---|---|
| Java | 35.0% | 24-26% |
| Groovy | 63.9% | - |
| TypeScript | 0.2% | 0 of 265 repos |
| Terraform | 0.1% | 0 of 38 repos |

So on an infrastructure or TypeScript estate the dropped edges really are external
and this costs nothing. On a Java-heavy estate roughly a quarter of them connect
two entities the store *does* hold, and dropping them removes a real relationship
between two displayed nodes.

Extract without the flag anyway: contentless nodes are the larger harm at +6%
nodes and +22% edges, and they cannot be cited or explained at all. But know that
on a Java estate this is a trade rather than a clean win, and that the underlying
defect is an id-matching failure rather than the flag.

**A post-merge prune is not an equivalent fix**, which is worth knowing before
reaching for the cheap one: clustering *resolves* some of those references into
real nodes rather than discarding them, so pruning drops edges the correct path
keeps (2,213 against 2,236 on that repository, and two fewer nodes). Re-extract
rather than clean up afterwards.

Nothing fails when the flag is used. Every stage reports success and the graph
is simply wrong, which is why this is documented here rather than left to be
noticed.

**Pin the hash seed before clustering.** Without it the same graph file can yield
a different community membership in each process — measured on two estates, and
on one the committed graph matched none of its own rebuilds. It is input-dependent
(11 of 12 communities over 100 nodes on one graph, 1 of 16 under 20) and the
rate varies between graphs — two other estates saw no instability at 899 and
2,832 nodes — so a store that tests clean has learned about its own graph today
and nothing that transfers. Summaries are keyed by
community id, so the loss is authored prose:

```bash
export PYTHONHASHSEED=0
```

A large graph also needs the size cap raised, or every graph operation refuses:

```bash
export GRAPHIFY_MAX_GRAPH_BYTES=4GB   # default is 512 MB
```

```bash
knowledgestore gherkin         # features, scenarios, ticket links into the graph
knowledgestore packages        # cross-repository package nodes and import edges
```

**Cluster after `gherkin`, not before**, so the Gherkin layer is clustered with
everything else. Then record which partitioner did it, in the same environment
and immediately afterwards, and build the page:

```bash
knowledgestore record-clustering   # -> graphify-out/clustering-inputs.json
knowledgestore explorer            # the self-contained search page
```

Stages are independent and idempotent — re-run one without repeating the rest.

### Ticket detail from the issue tracker: `fetch-tickets`

Optional, and off unless the store has tracker credentials
(`KSB_TRACKER_BASE_URL`, `KSB_TRACKER_TOKEN`). Run it after `intent`, which is
what discovers the tickets:

```bash
knowledgestore fetch-tickets     # -> knowledge/intent/ticket-tracker.json.gz
```

With no credentials it names the missing settings, writes nothing and exits 0 —
that is a correct outcome, not a failure to work around. **Do not obtain or
invent credentials to make it run**, and never put a token on a command line or
into any file in the store; it comes from the environment the operator set up.
Fetched tickets are never re-fetched, so a build without credentials reads the
committed cache. Commit `knowledge/intent/ticket-tracker.json.gz` with the rest.

Four numbers from its report belong in yours, and three of them are findings
rather than statistics:

- **denied** — tickets the run's token could not read. Say the number, and say
  that a run with broader access would close it. Never describe a denial as a
  ticket that does not exist: a permission gap recorded as absence becomes
  permanent and invisible.
- **undecided prefixes** — ticket prefixes in neither `KSB_TRACKER_PROJECTS` nor
  `KSB_TRACKER_DENY`, written to `knowledge/intent/tracker-undecided.json` with
  their ticket counts. Nothing was requested for them. **Report the list and stop
  there** — whether this store may read a project is not your decision, and
  neither quietly adding prefixes to the allowlist nor reporting them as skipped
  is yours to make.
- **redacted** — identifiers withheld from fetched text, under the same rules as
  mined commit text. Carry the count; never the value.
- **failed** — nothing was cached for those tickets and the next run retries
  them. A run with failures is not a broken build.

Fetched summaries and descriptions are **what the tracker says a ticket was**,
not evidence you derived. Attribute them that way, exactly as commit-mined text
is attributed to commits, and never merge the two into one claim.

### Clustering: `cluster-only` does not persist its result

`graphify cluster-only` re-extracts from the store root before clustering. For
the reason above its node count disagrees with the graph on disk, its overwrite
guard refuses the write, and **it still reports success** —
`Done - N communities. graph.json updated`. The graph is left with almost no
nodes carrying a `community`, and `GRAPHIFY_FORCE=1` does not help: `--force` is
documented for `update`, not `cluster-only`.

Left unnoticed this is destructive, because `summaries remap` then finds nothing
to map onto: it retains a handful of summaries out of thousands, reports the
rest as `whose members are gone`, and overwrites `communities.json` with what
survived. That reads as catastrophic churn when the node ids are essentially
unchanged. It is recoverable only because `communities.json` is committed
(`git checkout HEAD -- knowledge/summaries/communities.json`).

**Check coverage after clustering, before remapping:**

```bash
python3 -c "
import json; g=json.load(open('graphify-out/graph.json')); n=g['nodes']
h=[x for x in n if x.get('community') is not None]
print(f'{len(h):,}/{len(n):,} nodes have a community, {len({x[\"community\"] for x in h}):,} communities')"
```

Anything short of every node is a failed clustering, not a small gap.

Drive clustering through graphify's Python API instead, which persists because
you write the file yourself. `graphify.cluster` exposes `cluster()`,
`remap_communities_to_previous()` and `label_communities_by_hub()`. Write
`community` and `community_name` onto every node and a fresh
`.graphify_labels.json` (the digest label source; the library reads
`node.get("community", -1)`).

Two things decide whether the prose survives:

- **`remap_communities_to_previous` is not optional.** It renumbers new
  communities onto previous ids where membership overlaps, which is what lets
  summaries keyed to old ids stay attached. With it, a re-cluster keeps most of
  its prose; without it, effectively none survives.
- **Use graphify's `cluster()`, not plain Louvain.** On the same graph, Louvain
  at every resolution tried produced roughly a third as many communities, with a
  largest cluster an order of magnitude bigger than graphify's. Clusters that
  coarse are too big to summarise, and they collapse many old clusters into one,
  so remap discards them as collisions.

### `sync` on a case-insensitive filesystem

Where a remote has branches differing only in casing (`team/DEVOPS` and
`team/devops`), git cannot store both with the `files` ref backend on macOS or
Windows, and the fetch exits non-zero. On one estate roughly a fifth of the
repositories were affected.

`sync` isolates that: it names the failure, carries on with the rest, records
provenance for everything that succeeded, and **exits non-zero** with a list of
what failed. Read that list — a repository that did not sync keeps whatever
graph and history it had, so the estate is incomplete rather than merely warned
about. (Before this was fixed, one failure aborted the run and discarded the
provenance of the repositories that had already succeeded.)

Migrate the clones to the `reftable` backend (git 2.45+), which stores refs in a
table so casing stops mattering and both variants are kept:

```bash
git -C repositories/<repo> refs migrate --ref-format=reftable
```

Deleting the colliding local refs does **not** work — the remote carries both,
so the next fetch recreates the collision.

### Trust counts, not exit codes

Long rebuilds fail quietly, and every failure in a real estate refresh was
caught by reconciling a number against an expectation rather than by a tool
reporting failure:

- A shell wrapper reports its **last** command's status. `stage > log 2>&1; tail
  log` exits 0 however the stage ended. Capture the stage's own `rc` and read it.
- A loop that skips every item also exits 0. One extraction pass processed
  nothing because `repositories.txt` is pipe-delimited and the directory test
  silently failed for every repository; only counting successes revealed it.
- After any stage that writes per-item output, reconcile the count it reports
  against the count you expected.

### Adding repositories to an estate

Edit `config/repository-filters.txt` (include prefixes, explicit repositories,
exclusions; archived are always excluded), then re-run `discover` and `sync`.
Adding repositories changes clustering, so community ids move: see "when
clustering changes" below before regenerating summaries.

**A repository left out is a decision, so record it.** `exclude` removes a
repository from the estate and says nothing about why. `config/estate-boundary.txt`
is where the estate rules one `active`, `not-used` or `decommissioned`, names an
off-host alias of a repository it does hold, and dates a copy taken by hand.
`knowledgestore status` reconciles those rulings against provenance and names a
repository ruled active that the store does not hold - the shape of a
"no evidence of X" answer that is wrong. `knowledgestore context` renders the
declaration into `knowledge/repository-manifest.md`, and says plainly when there
is none. See the "Declare the boundary" section of `docs/creating-a-store.md` for
the rule set.

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
- **Anything else you write must carry your batch in its name.** A helper script,
  a scratch file, any intermediate output: `scratchpad/gen-<batch>.py`, never a
  bare `scratchpad/gen.py`. You are one of several agents writing to one
  filesystem at the same time, and only the output path you were given is unique
  to you. Two agents that pick the same helper path overwrite each other, and the
  one that loses then runs the other's script — producing another batch's work
  under its own name. Both writes succeed, so nothing reports an error.

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

**The bar measures recall, not fit**: it asks how much of the old cluster
landed together, never how much of the new cluster those members make up. A
summary can clear it and still describe a corner of where it lands. Retention
is a coverage number, not a correctness one — carried prose is owed a re-read,
and `verify` can split its flag rate by carried-versus-authored.

`remap` carries a summary only where the new cluster holding most of its old
members holds at least 60% of them (`--bar` to change), drops it otherwise
rather than risk prose on the wrong cluster, and prints retention so the cost of
the re-cluster is a measured number. It refuses to run on a wrong snapshot (no
shared node ids) or an implausibly small summary set (`--floor`), because both
failures silently produce an empty file over a good one.

Whatever `remap` drops is then a backfill — but not from scratch. `remap`
writes `knowledge/summaries/remap-report.json`: every displaced summary with
its prose, the reason (`below-bar`, `collision`, `members-gone`) and its best
new target. When a backfilled cluster's id appears as a `best_target` there,
give the author the displaced paragraph alongside the new digest with the
instruction: **revise to match this digest exactly; drop anything it no longer
shows**. Revised prose gets no trust discount — it goes through `merge` and
grounding verification like anything authored fresh. Measured on this
pipeline's own calibration, prose carried across clusters unrevised flags at
roughly four times the rate of prose written against its own digest, which is
why the spool feeds revision and never direct reinstatement.

`verify` uses the report's carried map to print grounding split by provenance
(carried vs authored). Read the two numbers together: retention improving
while carried grounding degrades means the remap is preserving coverage at
the cost of truth, and the bar or the prose needs attention.

Snapshot immediately **before each** re-cluster, not once per session. A
snapshot of a clustering the summaries are no longer keyed to is not refused; it
just retains less, and says nothing about why.

**Verify the clustered graph, never the clustering command's exit code.**
`graphify cluster-only` can compute a clustering, decline to write it, and still
print that it updated the graph — its writer refuses a net reduction in node
count, and that refusal does not fail the run. Check the artefact:

```bash
python3 -c "
import json
n = json.load(open('graphify-out/graph.json'))['nodes']
have = sum(1 for x in n if x.get('community') is not None)
print(f'{len(n)} nodes, {have} clustered ({have/len(n)*100:.1f}%)')"
```

Below 100% means it did not land, and remapping against an unclustered graph
produces a retention number that means nothing. A discarded write also leaves
labels, signatures and `GRAPH_REPORT.md` rewritten for the clustering it threw
away, so restore those from version control before retrying.

**Carry the previous membership into the re-cluster.** Clustering from scratch
renames most communities, and every summary keyed to a renamed id is dropped for
no reason connected to your change. `cluster-only` does not do this; driving the
API does:

```python
from graphify.cluster import cluster, label_communities_by_hub, remap_communities_to_previous
from graphify.export import to_json
from graphify.paths import load_node_link_graph

G = load_node_link_graph("graphify-out/graph.json")
communities = remap_communities_to_previous(cluster(G), previous_node_community)
labels = label_communities_by_hub(G, communities)
assert to_json(G, communities, "graphify-out/graph.json", community_labels=labels)
```

`previous_node_community` is `{node_id: community_id}` — invert
`knowledge/summaries/membership-snapshot.json.gz`, which stores it the other way
round. Loading the graph yourself also means the node count reaching the writer
matches the file on disk, so the guard above has nothing to fire on; do not reach
for `force=True` to get past it unless you can account for the difference.

**Record the partitioner every time you cluster**, before anything reads the new
ids:

```bash
knowledgestore record-clustering    # -> graphify-out/clustering-inputs.json
```

graphify partitions with graspologic's Leiden where that library imports and with
networkx's Louvain where it does not, so the algorithm behind every community id
is a property of the machine that clustered. Two operators on one corpus then get
two partitions, and `remap` reports a retention collapse with no cause in the
corpus. The record must be written by the environment that clustered — it cannot
know what someone else's run used — and committed beside the graph, which is what
lets a later `status` say *"records Leiden; this environment has only Louvain, so
a re-cluster here will not reproduce these communities"*. A store with no record
is reported as unknown, so do not read a silent `status` as agreement.

Community ids are not stable across re-clustering, and summaries are keyed by
id. After a re-cluster, do **not** assume the old file still applies. Either
regenerate, or remap by membership overlap: for each old cluster, find the new
cluster holding most of its members and carry the summary across only if the
overlap is convincing (60% is a reasonable bar). Drop the rest rather than risk
prose attached to the wrong cluster.

### Removing repositories from an estate

Nothing in the pipeline prunes what you remove. After editing the filters and
re-running `discover`, delete `repositories/<repo>` and
`knowledge/git-history/<repo>` by hand: `merge-graphs` takes a shell glob of
per-repository graphs and `intent` globs `*/commits.ndjson`, so both stages read
the filesystem rather than the configuration and a leftover directory silently
keeps a removed repository in every answer. Check for orphans that match no
configured repository at all while you are there. Then treat it as a normal
re-cluster, and expect `remap` to report the summaries whose members are gone —
that is a correct loss. Full procedure in
[docs/refreshing-a-store.md](../../docs/refreshing-a-store.md#remove-repositories).

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
4. `## Hidden coupling` — the co-ticket pairs (files recurring under the same
   tickets — ticket-level coupling, not commit-level co-change), especially cross-concern ones
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
corpus citations, the partitioner recorded in
`graphify-out/clustering-inputs.json` against the one this environment offers,
and whether the page is older than a layer it embeds. Add
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

## Asserting the store still answers

A refresh can leave every count healthy and every artefact well-formed while the
store has quietly stopped answering what it was built for. Declare the estate's
questions once, then gate every refresh on them:

```bash
cp examples/questions.txt config/questions.txt   # once, then make them your own
knowledgestore check-answers --candidate graphify-out/explorer.html   # before publishing
knowledgestore check-answers                                          # after
```

Check the **candidate** before committing it. Reading only the published page can
diagnose a bad publish and never prevent one - one estate's suite reported 12/12
while a rebuild sat unexamined, because every check read the published artefact.

Questions declare an answer *shape*, not text: `brief`, `dive`, `tickets`,
`graph`, `ticket`, `abstain`. Declare at least one `abstain` - a store that
answers everything is failing to say when it has nothing. `ticket` is the
strongest of them, because it asserts the file-to-ticket join, whose canonical
failure was 0 of 70,655 joined with the build green and both layers present.

Read the per-mode line, not only the total: `brief 4/4, graph 0/6` and
`10 of 10` cannot both be reported, but a total alone hides a dead layer behind a
healthy one. See `docs/building-a-knowledge-store.md` §9.

## Finishing a refresh

```bash
knowledgestore explorer
knowledgestore check-evidence               # must pass before you commit
node tests/explorer/estate-regression.mjs   # if the estate has one
```

`intent` redacts anything in mined commit text that identifies a specific case or
person — an email address by default, plus whatever formats the estate declares — replacing the matched span and keeping the account around it, and
reports how many went under each rule. **Carry that count into your report.** It is a finding about the
estate rather than a build statistic: the commit messages still hold the text,
whatever the store now publishes.

`check-evidence` exits non-zero when such text is still in the committed
artefact, including anything mined before the rule existed. It names the ticket,
the field and the rule and never the value — hold to that yourself: no matched
value in your report, in a ticket, or in this conversation. A clean result means
nothing matched the rules, never that the file holds no personal data.

Commit `knowledge/`, `knowledge_context.md`, `docs/topics/` and
`graphify-out/`. Never commit `repositories/`, `knowledge/git-history/` or the
uncompressed `graph.json` — all are regenerable and large.

Report honestly what was and was not regenerated: which stages ran, how many
summaries or briefs are still missing, and whether the estate regression
passed.
