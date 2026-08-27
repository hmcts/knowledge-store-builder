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

**This skill assumes knowledge-store-builder 0.15.2 or newer.** Check before
following it. The skills and the library install separately - these instructions
come from the plugin cache, the library from pip - so the two drift, and the
symptom is a stage documented here reported as `unknown stage`:

```bash
knowledgestore --version
```

**Stop if it reports below 0.15.2. Do not continue to any step below.** Say which
version is installed, that this skill needs 0.15.2 or newer, and that the fix is
`pip install --upgrade hmcts-knowledge-store-builder`. Continuing produces
`unknown stage` partway through a build, after earlier stages have already written
committed artefacts - a worse place to stop than here.

Two things this check cannot tell you, so ask rather than assume:

- **A store deliberately pinned to an older library is a legitimate position.** If
  that is the case, follow the documentation for the version it pins rather than
  this text - do not upgrade a pinned store to satisfy this skill.
- **The plugin does not upgrade the library and never will**; they install
  separately, and the plugin is not refreshed automatically either. If the version
  looks older than expected, the plugin may also be stale - `.claude-plugin/plugin.json`
  names the release these skills were shipped with, and `docs/asking-questions.md`
  has the three commands that refresh it.

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

knowledgestore merge-inputs        # reconcile what the merge will read
graphify merge-graphs repositories/*/graphify-out/graph.json \
  --out graphify-out/graph.json
```

Extracting from inside the repository is what keeps `source_file` repo-relative,
which is what the file-to-ticket join is keyed on; `merge-graphs` adds the `repo`
attribute.

**Run `merge-inputs` before every merge, and read its output.** Extraction is
driven by `config/repositories.txt`; the merge is driven by a shell glob, and
nothing else reconciles them. A repository cloned and extracted during a refresh
that was later abandoned keeps its graph, so the merge reads an input the store
does not declare and `knowledge/provenance.json` cannot date - and an answer
would cite it. The stage names four divergences rather than counting them:
undeclared, no provenance entry, declared but not extracted, and extracted only
as `graph.json.gz` (which the glob above will not read).

It reports and exits 0, because a tree caught mid-refresh is normal. Two states
exit 1 whatever you pass, because the check could not run at all: no graphs
found, and an unreadable `config/repositories.txt`. `--strict` also fails on an
undeclared or undated input; `--paths` writes one input path per line on stdout
and the report on stderr, so the merge can be handed names instead of a glob.

**Do not replace it with a check that walks `config/repositories.txt`.** The
input that fails is the one the declaration omits, so such a check skips it and
reports clean - which is worse than no check, because a clean report is read as
an answer.

### Write the detect result, then expose the content set

`content-set`, `chunk-plan` and `extract-ast` all read
`graphify-out/.graphify_detect.json` at the store root, and **nothing in
graphify's CLI writes it**: there is no `graphify detect`, and `graphify update .`
at the store root exits 0 leaving no detect result. Write it first, from the store
root, before extraction:

```bash
mkdir -p graphify-out
python3 -c "import json; from pathlib import Path; from graphify.detect import detect; print(json.dumps(detect(Path('.'), gitignore=False)))" \
  > graphify-out/.graphify_detect.json

knowledgestore content-set     # -> knowledge/corpus/content-files.txt
                               #    knowledge/corpus/content-set.json
```

`gitignore=False` is what makes the corpus visible: `repositories/` is in the
store's `.gitignore` and the scan honours ignore rules, so the default classifies
the store's own handful of files and nothing in any clone. `mkdir -p graphify-out`
first — the shell opens the redirect target before python runs. This is a
classification scan, not extraction; extraction still runs inside each clone.

**Say what the parameter costs when you report the build.** With
`gitignore=False`, anything a repository excluded only through its own
`.gitignore` becomes content unless the store's `.graphifyignore` re-excludes it —
and that file is one forgotten reinstall away from absent, because `sync` ends in
`git clean -fd -e graphify-out`. Two measured facts, both the opposite of what a
reader assumes:

- **`gitignore=False` does not disable `.graphifyignore`.** Measured in two
  independent sessions: a `.graphifyignore` still excluded a bundled directory
  inside a clone with the parameter set.
- **For content a repository tracks, the parameter changes nothing.** A tracked
  file was never excluded by that repository's `.gitignore` and cannot be, so it
  widens the scan only over what a repository generates and ignores.

**Where the scan does not complete, this route is unavailable.** On at least one
real estate the scan does not finish at that scale, and that store builds its
content set from extraction output instead. There is no substitute to recommend:
such a store has to produce `graphify-out/.graphify_detect.json` another way, and
every stage reading it takes it at face value. Say which route produced it.

Two things come out of `content-set`, and both are committed.

**The path list is what a corpus search must read.** Anyone who falls back from the
graph to `grep` otherwise searches the raw tree, and the tree holds each clone's
extraction cache and graph, its VCS pack files and any vendored bundles alongside
the corpus. Measured on two estates, a naive search sees several times as many
files as the store considers content. Do not hand-maintain an exclusion list to
work around it: a list is a second model of what the tool produces, correct the day
it is written and silently wrong the next time the pipeline emits something new.
One store's list covered dependency bundles, build output and state files and not
the pipeline's own directory, so hundreds of its own artefacts were being fed back
to the extractor.

**The report names where the noise is, and now is when that is cheap to act on.**
Each row is the shallowest directory in a repository under which the store found no
content at all, aggregated across every repository holding one — so the estate-wide
magnitude of a single cause reads as one line. Excluding those with a
`.graphifyignore` **before** extracting is far cheaper than filtering afterwards:
extract a file once and its derived content persists in the extraction cache and in
that clone's own graph, neither of which a later filter touches.

Nothing here is classified against a list of directory names. The rows are derived
from the content set, so an estate whose dominant cause is something nobody has
seen before still gets it named.

**It also reconciles the set against the clones on disk, and this one can stop the
build.** Cloned repositories that contributed no content file are named, and a
non-zero count there is expected rather than a defect — a repository created and
never populated contributes nothing. The stage exits non-zero, writing neither
artefact, only when *no* clone contributed anything while clones are present:
that is what a scan which never saw the corpus produces, and the set it reports is
small rather than empty, so nothing else in the pipeline can tell. Read the
refusal before acting on it — it says which single case it cannot distinguish.

### Size the AST layer before merging

Run this between the extraction loop and `merge-graphs` above, while the layer
can still be measured on its own terms:

```bash
knowledgestore size-cuts       # every repositories/*/graphify-out/graph.json
```

It names each per-repository graph with its own counts, sizes any candidate cut
declared in `config/content-cuts.txt`, and records the layer's totals so the next
refresh can say what moved. After the merge you can only measure the layer you
published, which is a different quantity: a cut removes content for reasons
unrelated to it being noise, so what survives says nothing about what the raw
layer held.

Three rules when an estate asks for a content cut, and none of them is a number:

- **Size a candidate by its surviving edges, not its node count.** An edge
  survives only when both endpoints do. The most attractive candidate by nodes on
  one estate kept only file-level nodes — tens of thousands of them, joined by low
  hundreds of edges, because AST edges connect symbols rather than files.
- **Never propose a threshold, a cap or a per-language ban.** Two estates measured
  AST-to-semantic node ratios roughly a factor of a hundred apart, so a constant
  is wrong on one of them by two orders of magnitude. Compare against that store's
  own previous refresh in `knowledge/telemetry.json`, which is what recording it
  is for.
- **Do not prune by cross-file connectivity.** It was measured and abandoned:
  roughly three quarters of Java nodes had a cross-file edge and rather fewer
  Terraform nodes did, so the axis marks ordinary imports rather than relevance. A
  cut is a statement of what the store is for — say it in the store's own
  documentation, so a reader knows the shape of what is missing.

Applying a cut is a change to the graph the store commits, so get
`knowledgestore check-answers` passing on the uncut store first: the question is
not how much smaller the graph got, it is whether anything the estate needs
answered depended on what left.

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
interacts with the fan-out's own limits. Decide both together - the limits are in
"Dispatching the semantic fan-out" below, and nine times the dispatches is nine
times the exposure to every one of them.

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

### Dispatching the semantic fan-out

**A code-only corpus never reaches this section.** graphify's semantic pass writes
an empty layer for a corpus holding no documents, papers or images, so an estate of
nothing but application code produces zero chunks and skips the fan-out entirely.
Everything below is conditional on corpus composition rather than universal - and a
doc-bearing estate spends a long build inside it, so the absence of complaints
about the fan-out is evidence of who has run it, not that it is well-supported.

Give **every** extraction agent this instruction, verbatim:

> Write each chunk to disk IMMEDIATELY after producing it. Do NOT accumulate
> results in your context and write at the end.

An agent handed twenty-odd chunks will otherwise accumulate and write at the end,
and past roughly **64k output tokens it dies with everything it produced lost** -
it wrote nothing. The evidence for the instruction is stronger than the evidence of
the failure that prompted it: later in the same build a session limit killed ten
agents mid-batch simultaneously, and because every one was writing per chunk the
loss was the single chunk each had in flight rather than the twenty-odd each had
completed. On a layer that costs tens of millions of tokens to produce, this line
is the difference between an interruption costing a handful of chunks and it
costing hundreds.

**The concurrency ceiling rejects rather than queues.** Dispatch past the
concurrent-agent limit and the excess is refused, not held - so a chunk can be
recorded as dispatched and never launched, and it then waits forever because
nothing will ever produce it. Two things follow:

- **"No output on disk" has two causes** - an agent still working, and an agent
  that was never launched - and only the second needs a human. Plan-ordered
  dispatch hides it: a low-numbered chunk rejected in an early round sits behind
  every higher-numbered id that followed, unnoticed for as long as the numbers
  above it keep arriving.
- **Capacity arithmetic computed from a completed count is systematically
  optimistic**, because agents finish between the check and the dispatch. Dispatch
  smaller batches and reconcile after each round rather than computing headroom
  once and spending it.

**Read progress off disk, never off your dispatch log.**

```bash
knowledgestore chunk-status --dispatched round-1.txt round-2.txt
```

`done` is the plan intersected with the extractions on disk and consults no log.
The log only ever *splits* the outstanding set, into `NEVER SENT` and `in flight`,
and never-sent is printed first. Chunk files that are present but unreadable are
reported separately and never counted as done - that is what an agent killed
mid-write leaves behind. Log tokens matching no chunk in the plan are named rather
than counted, and a token that looks like several ids run together is diagnosed as
such. The stage names the logs it read and how many tokens it took from them, so
you can reconcile against what you dispatched; it exits non-zero only when there
is no plan to measure against.

**A dispatch log is a cache of intent, not a record of fact.** Both halves of that
have cost real work. A coverage gap of ninety-odd chunks was announced by diffing
the plan against a log without intersecting disk, and a redundant round of a dozen
agents was launched for a gap that did not exist - the log simply did not cover the
early rounds. Separately, a log assembled by appending batch files that carried no
trailing newline fused the last id of one file onto the first of the next; those
tokens matched no chunk, counted as dispatched-but-absent, and for several rounds
inflated `in flight` and deflated `NEVER SENT` while every total stayed plausible.
A status tool that launders a corrupt log into a confident number is worse than no
tool, because it is trusted.

**Reconcile before merging.** `merge-chunks` reports the chunk files it could not
read, but it cannot report a chunk that produced no file at all - it never sees
one. Run `chunk-status` until `NEVER SENT` is none and `done` equals the plan.

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

**Measure that trade on this estate before acting on it.** The share above
varies by three orders of magnitude between estates measured with the same
predicate, and by different mechanisms, so no figure from another estate
predicts yours:

```bash
knowledgestore dangling-endpoints          # after extraction, before merge-graphs
knowledgestore dangling-endpoints --json graphify-out/dangling-endpoints.json
```

It walks `repositories/*/graphify-out/graph.json`, names every file it read, and
splits the dangling endpoints three ways: **recoverable** (the id names exactly
one node the graph already holds), **ambiguous** (it names more than one — never
guessed, never counted as recovered), and **absent** (no node of that name, which
is what external and standard-library symbols look like). It writes nothing to
the graph.

Run it **before** `merge-graphs`. Measured on the merged graph the rate is zero
by construction: `merge-graphs` has already turned every dangling endpoint into a
node, and the layer merge has already dropped the rest. The stage refuses that
file by name rather than reporting a clean zero, and it exits non-zero when the
walk finds nothing at all.

Read the count beside the rate. A high rate over a few dozen endpoints sizes
nothing; a low rate over tens of thousands may still be worth acting on. The
recoverable count is what a repair could win, and the absent count is what
materialising from `local_id` would turn into labelled nodes nobody asked for.

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

### Run local extractors before the stages that rewrite the archive

Three stages rewrite the committed archive `graphify-out/graph.json.gz` from the
mid-pipeline `graphify-out/graph.json`:

```bash
# Stages that rewrite graphify-out/graph.json.gz
knowledgestore gherkin
knowledgestore packages
knowledgestore deployments     # opt-in: does nothing unless KSB_DEPLOY_REPOS is set
```

A store with extractors of its own has one ordering to keep: **run them before
those three, or remove the archive between them.** Nodes written into
`graph.json` after the archive was last built leave the two files describing
different graphs, and neither outcome from there is the one you want: either the
stage refuses, in a message about communities and clustered nodes, or it
rewrites the archive from `graph.json` and whatever the archive held that
`graph.json` does not is gone.

**The refusal names clustering; the cause is the ordering.** It counts
communities and clustered nodes in both files, prints both totals and says one
is stale — because losing a clustering is the worst thing the overwrite can do,
not because clustering is what went wrong. Read it as "these two files hold
different graphs", and ask what wrote `graph.json` last.

**Do not follow the remedy it prints before answering that.** It offers
`gunzip -kf graphify-out/graph.json.gz`, which decompresses the archive over
`graph.json`: right when `graph.json` is a leftover from an abandoned run, and
the loss of a whole layer when it is the file a local extractor has just
written. Removing `graph.json`, the other half of the same message, discards it
too.

Removing the archive instead leaves the stage nothing to overwrite, so it writes
both files from `graph.json` and the local nodes reach the archive. The archive
is committed, so version control has it back if the run then fails. Say which
of the two orders you used in your report: it is what explains the graph the
store now ships.

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

### Compare the count with the last build's, not with an expectation

A count you reconcile against your own expectation only catches what you thought
to expect. `intent`, `merge-layers` and `explorer` each record what they measured
in `knowledge/telemetry.json` and print the movement since the last recorded
build:

```
Telemetry, against the last record in knowledge/telemetry.json:
  explorer.rows_with_tickets: 5,568 -> 1,204 (-78.4%)
```

**Read the movements and carry them into your report.** Every number in that
report was plausible on its own and implausible beside its predecessor - a
file-to-ticket join that lost most of its matches still reports a healthy-looking
fraction of the graph. `git diff knowledge/telemetry.json` is the reviewable
record; commit it with the rest of the store.

Nothing fails on a movement, because an estate change moves all of these
legitimately. The single exception is a measurement that was non-zero and is now
zero, which goes to stderr as a warning. Do not add a threshold of your own: two
estates measured the AST-to-semantic node ratio a hundredfold apart, so any
constant is wrong on one of them, and the comparison that works is against this
store's own history.

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

**Which repository to add is a measurement, not a guess.** Once the estate is
synced:

```bash
knowledgestore gaps          # add --limit 0 for every namespace
```

It reads the estate's own build files for the artefact coordinates it consumes,
subtracts the ones it builds, and ranks what is left. Three rules when reading
it, each of which has already cost someone an afternoon:

- **Read the class column before the weight.** Domain namespaces are listed
  first whatever their weight, because most reference weight is framework
  plumbing and a reference to a test utility says the estate writes tests, not
  how its business works.
- **Never resolve a coordinate to a repository by name.** An internal artefact
  is published to a binary repository, so its `artifactId` may appear in no
  source file on the forge and code search returns nothing while rate-limiting.
  Name matching against a large organisation returns confident nonsense from
  unrelated programmes. Report the coordinate as written and let a human resolve
  it from the published POM's `<scm>` URL.
- **Unbuilt does not mean addable, and this stage never proposes an addition.**
  On the estate the method was measured against, roughly a hundred coordinates
  were unbuilt and one was worth adding. Rank, explain, and hand the decision to
  the operator - including the decision to record a rejected candidate in
  `config/estate-boundary.txt`.

Widening the name prefixes instead is the intuitive move and the measured
answer was no: on that estate it would have added mostly reusable
infrastructure wrappers and empty repositories, and contradicted an exclusion
the estate had already recorded deliberately.

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
- **A hyphenated term is checked as an identifier only if it has three or more
  segments and a lowercase initial**, so `same-named` and `JDBC-backed` are never
  flagged, and a compound joined by a preposition or conjunction (`end-to-end`,
  `point-in-time`) is exempt as well. A three-segment lowercase compound is
  flagged whether it is an identifier or ordinary English, because an estate's
  identifiers are built from ordinary English words — `widget-record-created` and
  `no-reason-supplied` are one shape to any check. If a flagged term is English,
  rephrase it; do not assume the check is wrong.
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
knowledgestore summaries adrift     # FIRST: is the committed snapshot still the graph's?
knowledgestore summaries snapshot   # BEFORE re-clustering
# ... add repositories, merge, re-cluster ...
knowledgestore summaries remap      # AFTER: carries summaries onto the new ids
knowledgestore summaries snapshot   # re-key the baseline to the new clustering
```

**`adrift` first, and never straight after a snapshot.** Community ids are
positional, so only the snapshot binds a summary to a member set; re-cluster or
rebuild without refreshing it and every summary stays attached to a community it
no longer describes, while every community still has a summary and `status`
reports the same coverage either way. `remap` cannot see it — it refuses when the
snapshot and the graph share *no* node ids, and a snapshot taken from a stale
graph shares *every* id with that same stale file. Run `adrift` on the store as
committed; run it immediately after `summaries snapshot` and it compares the
snapshot against the graph it was just taken from, which passes by construction.

Exit 1 is drift. **Exit 2 means the check could not run**, and it names why: no
membership read, or the wrong snapshot. Both make every summary compare as adrift,
and the response is to fix the graph or re-take the snapshot — never to re-author
prose. An id-space mismatch (`<repo>::<id>` on one side, bare ids on the other) is
reported as a note for the same reason.

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
whether the page is older than a layer it embeds, and what the last build
recorded in `knowledge/telemetry.json`. Add
`--drift` to ask GitHub how far each repository has moved since the build
(one API call per repository). It never fails the build: drift is normal,
and the response to it is a refresh, not a red cross.

Add `--paths` to report absolute paths in the store's own tracked files. Paths a
store persists about itself are **relative at rest, absolute in flight**: an
absolute one records the build machine's directory layout in a committed artefact
and stops naming a real file as soon as the store moves, while every count still
reconciles and the JSON stays well-formed. The check names the files and counts
them, and separates the artefacts that hold absolute paths by contract — graphify's
`FILE_LIST` must be absolute verbatim — from the ones that should not. Convert with
`knowledgestore.store_paths`, which still hands readers absolute paths.

Add `--duplicates` to rank repositories by how much of their (label, path) pairs
they share:

```bash
knowledgestore status --duplicates
```

It reports and ranks; **you** decide whether a copy is deliberate. A vendored
fork, a template instantiated twice and a migration part-way through all look
identical to this measurement, and there is no percentage above which it declares
anything. Nothing else in the pipeline can see a near-copy: `merge-graphs`
namespaces node ids per repository, so two copies of one file produce ids that
cannot collide, and every count downstream reads as two ordinary repositories.
What a copy costs is a second set of communities, a second set of LLM-authored
summaries for the same code, and two equally good citations for one question.

Each line gives the shared count and the percentage of *each* side, because the
reading differs by direction: a small repository wholly inside a large one is 100%
of itself and a few percent of the other. The ranking uses the larger repository
as the denominator. The header states how many pairs were bounded and how many
were actually intersected - the report streams the whole graph, so it is opt-in
like `--central`.

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
summaries or briefs are still missing, what `knowledge/telemetry.json` moved by,
and whether the estate regression passed.
