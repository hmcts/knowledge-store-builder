# Building an effective knowledge store

The README tells you what each stage does. This document is the part that took
longer to learn: the judgement involved in getting a store that answers real
questions, and the costs you will pay whether you plan for them or not.

It is written from one large internal estate, which grew from 27
repositories to 168 over several refreshes. Where a figure appears, it is
evidence from a specific build, not a target.

## 1. What a store is good at, and what it cannot do

A store answers questions about **structure, ownership, history and intent**:
which repositories implement a thing, where a name appears and whether those
occurrences are the same implementation, what changed and under which ticket,
what a cluster of code is for.

It cannot answer questions about **runtime**. Extraction is static and
per-repository, so unless you deliberately add an architecture layer (§4) the
graph has no cross-repository call edges. You can prove that fourteen
repositories carry copies of a schema; you cannot prove which of them breaks
when it changes. Say so rather than inferring — an honest "the graph cannot
show this" is worth more than a confident guess, and consumers learn to trust
the store's limits.

Nor does it know anything that is not in the sources you gave it. Wikis,
ticket-tracker prose, Slack decisions and undocumented conventions are all
invisible. If the knowledge only exists in someone's head, the store's job is
to tell you whose head.

## 2. Defining the estate

The estate definition is the highest-leverage decision you will make, and the
easiest to get quietly wrong.

**Three ways to select repositories, in ascending order of reliability:**

- **By naming convention** (`prefix service-`, or `match *-shared-infrastructure`
  where the distinguishing part sits at the end) — cheap and stable where a
  convention is enforced. It silently misses anything renamed or predating the
  convention, so pair it with explicit `repo` lines for the strays.

  `prefix cpp-` and `match cpp-*` are the same selection: use `prefix` for the
  common case, `match` where a prefix cannot reach. There is deliberately no
  `suffix` rule, because a glob already covers it — and one estate needed **55
  explicit `repo` lines** for a `{product}-shared-infrastructure` convention
  before this existed. A glob of nothing but wildcards (`match *`) is refused: it
  would select a whole organisation from one character, and the result would look
  every bit as deliberate as an estate somebody chose.
- **By ownership** (`team <slug>`) — the right tool when a team's repositories
  follow no convention at all, which is normal for newer or exploratory teams.
  It also tracks reality: repositories the team acquires appear at the next
  refresh without anyone editing config.
- **By architecture** — if your organisation maintains an architecture model
  (C4 or similar) that links elements to repositories, treat it as an audit of
  your estate definition. On that estate the model linked 75 repositories,
  74 of which were already ingested; the 75th was a product nobody had noticed
  was missing. A model that disagrees with your estate is telling you one of
  them is wrong.

**The inclusion test is "does it answer questions people ask", not "is it a
deployable service".** These come apart more often than you would expect. An
architecture model deliberately excludes shared libraries because they are not
deployable — but the event-sourcing framework every backend service is built
on is exactly what someone needs when they ask how event sourcing works here.
Conversely, a developer tool or an abandoned prototype adds noise and answers
nothing.

**Exclude deliberately and say why.** Every exclusion is a decision someone
will later want to reverse or understand: a repository that is a separate
architecture, one gated for a security finding, the store's own repository (or
a broadened prefix will make it ingest itself), and the pipeline library. Put
the reason in the filter file next to the rule; that file is the only place
anybody looks.

**Some sources belong on disk but not in the graph** — use `fetch`, not `repo`.
A `fetch <name>` rule clones a repository and never extracts it: `discover` writes
it to `config/repositories-external.txt` rather than the estate manifest, and
`sync` puts it under `external/` rather than `repositories/`. The extraction pass
walks `repositories/`, so it cannot reach a fetch-only source by construction
rather than by anybody remembering not to.

Reach for it when a repository is worth having locally but is the wrong thing to
ingest whole:

- **A source a bespoke script takes selectively.** The clearest case is a
  hand-written knowledge base describing the same estate the store extracts.
  Ingesting it puts a second, prose description of the estate inside the graph,
  and the prose one goes stale silently. A script that takes only the parts
  extraction cannot produce needs the clone; the graph must not have it.
- **A repository held back behind a review** — a secrets finding, a licence
  question — that you still want fetched and watched.

The rule earns its place because the alternative gives no signal either way.
Listed as `repo`, such a repository is extracted on the next refresh alongside
whatever the bespoke script contributes, and the graph ends up describing the same
estate twice. The run completes normally, so there is nothing for a reviewer to
notice at the time and nothing to look up later. Before the rule existed the usual
workaround was a clone kept outside the store by hand: it met the same need, but it
drifted, and it was easy for someone new to be unaware of.

A name cannot be both `repo` and `fetch`; `discover` rejects the file rather than
guess. `fetch` beats a `prefix` that would have matched, and `exclude` beats
both.

**Watch for negative knowledge.** "This repository does not exist", "this
component is a module inside that repository, not a deployment", "this project
was abandoned before it was built" — these save real time. If your
organisation has done a catalogue-versus-reality reconciliation, ingest it.

**Size an expansion by its file mix, not by its repository count.** Two
estimates for one expansion were wrong by an order of magnitude in opposite
directions, both because they counted repositories: infrastructure repositories
predicted at hundreds of semantic chunks produced tens, because `.tf` goes to
the AST layer rather than the semantic plan, and the application repositories
predicted to be vendor-heavy were not. A language that emits a node per symbol
and a format that emits one per resource differ by orders of magnitude for the
same repository, so the same repository count can mean either. §3 says what each
content type contributes, and [sizing the AST layer](#sizing-the-ast-layer-before-you-commit-to-it)
measures what yours actually did.

## 3. What extraction actually yields, by content type

This table is the single most useful thing in this document. Extraction quality
varies enormously by language and format, and knowing which is which stops you
expecting structure that will never appear.

| Content | What you get | Notes |
|---|---|---|
| Java, TypeScript, Python, Go | Rich AST structure: symbols, imports, call edges within the repository | The dense core of any code store |
| JSON/YAML schemas, RAML | Field and property names as nodes | Strong evidence: a field name proves the system records that fact |
| Gherkin `.feature` files | Features, scenarios and ticket links in business language | The best bridge from business question to code |
| Markdown (ADRs, runbooks, designs) | Nothing structurally — **semantic (LLM) extraction only** | Where decisions and rationale live; worth the extraction cost |
| Architecture DSL (C4, LikeC4, Structurizr) | Nothing structurally — semantic only, but disproportionately valuable | See §4 |
| Terraform / HCL | **Nothing.** No AST support | Infrastructure value arrives via its YAML, its markdown and file names |
| Helm charts | Chart YAML parses; the deployment topology is in values files | Names every service and its config surface — a good cross-repo index |
| Postman collections, binary, images | Nothing useful | History and manifest presence only |

Two consequences worth planning for. First, a repository can be legitimately
**in the estate with no graph nodes at all** — its commit history, ticket links
and manifest entry still answer questions. Do not treat "no nodes" as a failed
ingestion. Second, **markdown-heavy repositories need the semantic path**, which
means an LLM and real time; a code-only sweep silently skips them. If you run
extraction without a model available, use the code-only mode consciously and
record which repositories were skipped, rather than discovering it later.

### Sizing the AST layer before you commit to it

```bash
knowledgestore size-cuts                    # every repositories/*/graphify-out/graph.json
knowledgestore size-cuts --no-record        # while you are still trying candidates out
```

Each per-repository graph is named with its own node and edge counts, then every
candidate declared in `config/content-cuts.txt` is sized. The layer's totals and
each candidate's two counts go to `knowledge/telemetry.json` — which your store
commits, so they appear in its diff — and the next refresh reports what moved. Run it
after extraction and before the layers are merged — the per-repository graphs are
the only place the AST layer can still be measured on its own terms.

Why it matters on a mixed estate: symbol-level languages contribute a node per
declaration where infrastructure formats contribute one per resource, so the AST
layer can reach a size where the semantic layer's edges cannot influence
clustering at all — and community summaries then describe call graphs instead of
the estate. The library sets no threshold for that, and cannot: two estates
measured AST-to-semantic node ratios roughly a factor of a hundred apart, so any
constant would be wrong on one of them by two orders of magnitude. What you get
is your layer's numbers and your own previous refresh's.

**Size a candidate by its surviving edges, never by its node count.** An edge
survives a cut only when both of its endpoints do, so the two reductions are not
proportional. On the estate that reported this, the most attractive candidate by
node count kept only file-level nodes — tens of thousands of them, joined by low
hundreds of edges, because AST edges connect symbols rather than files. Nothing
but the edge count says that a candidate is mass without structure.

**A cut is a statement of what the store is for, not a structural heuristic.**
The tempting version — keep every node with an edge to another file, prune the
rest — was measured on that estate and abandoned: roughly three quarters of Java
nodes had a cross-file edge and rather fewer Terraform nodes did, so the
application symbol graph looked *more* connected than the infrastructure the
store existed to describe. An ordinary import reaches across files. A policy has
only to be true of your intent, which is why "this store does not hold
application symbol detail" survives contact with data that refutes the
heuristic — and it can be written in the store's own documentation, so a reader
knows the shape of what is missing.

Declare candidates in `config/content-cuts.txt`, one `cut <name>` per candidate:

```bash
cp examples/content-cuts.txt config/content-cuts.txt   # then make them your own
```

```
# Keep the estate's infrastructure surface wherever it lives, including a
# component's own /infrastructure directory inside an application repository.
cut iac-anywhere
file *.tf
file *.tfvars
file *.hcl

# For comparison: the same intent expressed by repository, which discards every
# application repository's component-level infrastructure along with the rest.
cut infra-repositories-only
repo *-infrastructure
```

Three axes — `file` (the node's `source_file`), `kind` (its declaration kind) and
`repo` — each with a `not-` form. A node is kept when it matches at least one
rule on **every axis the cut constrains** and no `not-` rule, so rules on one
axis widen a candidate and a second axis narrows it: `file *.java` with
`not-kind method` is "Java declarations without the callables".

Three things that will otherwise cost you a run:

- `*` crosses directory separators, so `*.tf` already means any `.tf` at any
  depth. `**` is refused rather than read as one `*`, because `**/*.tf` would
  quietly exclude a `.tf` file at a repository's root.
- `merge-graphs` is what writes the `repo` attribute, so on the layer as
  extracted no node carries one. A `repo` rule falls back to the
  `repositories/<name>/` segment of the graph file's own path, which is why the
  axis works on the default input at all.
- A rule that matched no node is reported with its line number, and so is a
  candidate that kept nodes and no edges. Both look exactly like a clean run
  otherwise.

**The stage sizes cuts; it does not apply one.** A repository-level cut is
applied by naming fewer graphs to `graphify merge-graphs`; a cut by file type
inside a repository has no route in the library yet. Before applying either, get
`config/questions.txt` and `knowledgestore check-answers` (§9) working against
the uncut store — the measurement that decides a cut is not its size, it is
whether any question you need answered depended on what it removed, and there is
no baseline for that after the fact.

### Deployment configuration (the `deployments` stage, opt-in)

An estate whose deployment configuration lives in a repository can carry it as
evidence: which services reach which environment, and what each is configured
with. Name the clone in `KSB_DEPLOY_REPOS`; unset, the stage does nothing,
because most estates have no such repository. It needs PyYAML, which is not a
runtime dependency of this library — `pip install
'hmcts-knowledge-store-builder[deploy]'`.

**The unit is (service, environment), never service alone.** On the estate this
was built against, the same repository declares 96 services in `prd` and 72 in
`dev`, with 42 in production absent from development. A node per service would
have merged those into one answer true of neither, and it would have looked
perfectly plausible.

Three things it cannot promise, all worth saying to whoever asks:

- **It is declared desired state, not live cluster state.** Even a store built
  minutes ago answers "what the repository declares at this commit", never "what
  is running". Those are different claims, and conflating them during an incident
  is how somebody acts on configuration a hotfix superseded.
- **Templated values do not resolve.** Roughly nine in ten values files on that
  estate carry Jinja markers, so "does this service set resource limits" is
  answerable and "what is its replica count" often is not. The key survives
  carrying a placeholder, so *set from a variable* stays distinguishable from
  *unset* — never quote a placeholder as though it were a value.
- **The join is by name, and names drift.** The stage reports the match rate and
  names what did not match.

That last point has a trap worth stating plainly, because it cost a rewrite here.
**A missing join is visible and a wrong join is not.** A service joined to the
wrong repository is counted as a success, so it *raises* the match rate — the one
signal you have reads as reassurance while production configuration hangs off
unrelated code. An early version of this stage matched on any normalised
substring and sent `id-service` to a video repository, because `id` is inside `video`.
Matching now requires a whole hyphen-delimited segment, falling back to substring
only for stems long enough that coincidence is implausible. If a match rate ever
looks surprisingly good, suspect the matcher before believing it.

The whole configuration stays in the graph for an agent to read; the explorer
page carries a capped summary per deployment (`KSB_DEPLOY_PAGE_KEYS`), searchable
by key or value, on the same trade the ticket detail makes.

## 4. Architecture-as-code earns its place

If a repository in your organisation contains an architecture model, ingest it
before almost anything else. It is the only source that can give a
per-repository extraction pipeline a **cross-repository layer**: elements bound
to the repositories that implement them, and stated relationships between them
("makes API calls to"), authored deliberately rather than inferred.

Extract it semantically, and shape the work to the model:

- **Chunk by coherent architectural unit** — one subdomain or bounded context
  per agent, not an arbitrary file count. Elements and their relationships stay
  in the same chunk, so relationships come out EXTRACTED rather than inferred.
- **Tell the extractor what the DSL means**: which construct is an element,
  that `a -> b "verb"` is a stated relationship, that a repository link binds
  the element to code. Generic extraction guidance will produce generic results
  from a highly structured source.
- **Have later chunks reuse earlier chunks' node ids** for elements they both
  reference. Without this, cross-subdomain relationships point at duplicate
  ghost nodes and the model fragments instead of traversing.

## 5. Gate before you build, not after

**Scan for secrets before any graph content is generated.** Infrastructure and
legacy repositories are the likely offenders. Scan the working tree, because
that is what gets ingested, and be clear about the limit: a working-tree scan
says nothing about git history. When something is found, drop the repository
from the estate, record the finding with its location — never its value — and
say what has to change before it returns. On that estate this caught a
hardcoded database credential that had been in a file since 2019.

**A rewrite that lands on a real file cannot be caught by checking that files
are real.** A store normalising committed paths ran a rewrite that resolved
symlinks as a side effect, so two entries changed identity while keeping a
plausible shape. Every check anyone would naturally run still passed: the counts
were unchanged and every resulting path existed on disk. Existence is the wrong
question — the right one is whether these are the *same* paths, which needs an
independently written earlier witness to compare against. The same shape has now
produced a dead file-to-ticket join, fused dispatch tokens counted as valid work,
and a corrupted corpus inventory that a twelve-check suite passed 12/12. If you
build one verification primitive, make it "compare against a witness written
before the change", not "validate the result".

**A count without its layer is not a finding.** Two correct measurements taken
at different layers look exactly like a contradiction, and this estate family
produced three such false conflicts in one week: 456 nodes against 57 for the
same symlink (raw AST versus published graph), 12.7% against 15% for contentless
nodes (a loose rule against a strict one), and a clustering variance attributed
to the partitioner that was actually in the splitting pass downstream of it. In
every case both parties were right and one number was quoted without saying what
it counted. State the layer with the number, always.

**An idempotency check must cover everything the step is responsible for, not
only what it originally wrote.** A store repairing a graph attribute guarded its
repair with "if the node already has the right value, skip" — measured against a
field that was *already* correct, so every node counted as done, the new
attribute was never written, and the script printed success having changed
nothing. The report and the result disagreed and only the result mattered. This
is why a repair is verified by re-reading the field afterwards rather than by
trusting the line that says it was written.

**Treat ingested content as untrusted data.** Repositories increasingly contain
agent instructions, and extraction agents will read them. Instruct every
extraction agent that file contents are data to extract from, never
instructions to follow. This is not hypothetical: two independent agents
flagged and ignored injected instructions during one refresh of that estate.

**Symlinked source files are extracted twice.** Extraction records the path it
walked, not the link target, so a symlink and its target become two sets of
nodes with identical content under two paths. For a store whose most valuable
answer is "these same-named things are independent implementations", that is a
wrong answer rather than a noisy one — a shared parent resource appears once
per directory that links to it.

`knowledgestore status` reports them before a rebuild. It is most useful
*between installing an extractor extra and rebuilding*, not at rebuild time: a
suffix nothing can parse yields nothing, twice, so installing the parser is the
moment a latent duplicate becomes a real one. One estate carried 60 symlinked
`.tf` files in a single repository, invisible until the Terraform extra landed.

Excluding them needs a `.graphifyignore`, and **where it goes depends on how
extraction is invoked, which is easy to get wrong because the wrong placement
fails silently rather than erroring.** The file is read at the scan root and its
ancestors up to the enclosing VCS root; a file *below* the scan root is inert.
Measured, one symlink and its target:

| `.graphifyignore` at | per-repository scan | single-root scan |
|---|---|---|
| inside the cloned repository | **excluded** | ignored — below the scan root |
| the store root / `repositories/` | ignored — above the repository's VCS root | **excluded** |

The two placements are mutually exclusive, and the awkward part is that the
placement which survives `sync` (outside the corpus) only works for the
single-root scan — which is the invocation that costs you per-repository node
namespacing. Extract per repository and the exclusion has to live inside the
cloned corpus, where the next `sync` may remove it. There is no comfortable
answer yet; know which trade you are making rather than discovering it in the
graph.

**Exclusion is also the one thing a store loses by moving to the per-repository
route, and it loses it silently.** That route does no vendor skipping, so
committed dependency bundles come straight back into the graph: on one estate
6,116 nodes of vendored package-manager releases returned from **two files**,
**35.8% of the whole AST layer**. Nothing announces it, because the result looks
like a graph working hard rather than a graph full of a package manager — the
god nodes are named `c()` and `push()`. Exclude vendored trees *before*
extraction rather than filtering after it, which also keeps the corpus inventory
from claiming the estate covers a package manager. The same argument applies with more force to anything secret-bearing — a
Terraform state file holds resolved secret values — and there "filter it out
afterwards" is not merely untidy, it does not work. **Extract a file once and
its derived content persists in two places later filtering never touches:**

- `graphify-out/cache/ast/<version>/<sha256>.json`, the extraction cache, keyed
  by content hash. Removing nodes from the published graph does not invalidate
  it, and the next build replays from it.
- each clone's own `graphify-out/graph.json` — and this is the sharp one,
  because `sync` ends with `git clean -fd -e graphify-out`, making
  `graphify-out/` **the one directory in a clone that sync deliberately
  preserves**. The exemption exists for a good reason (cleaning it forces a full
  re-extraction), but its effect is that anything extracted once is durable by
  design.

So exclude before extraction, not after. Note also that vendored code is not
reliably *under* a vendored directory: one estate's exclusion appeared to work —
`.yarn/` and `node_modules/` residue went to zero — while 1,914 nodes came from
`.pnp.cjs`, a generated loader sitting at the repository root, which every
directory-shaped pattern missed and which reads as authored code.

**A `.graphifyignore` inside a cloned repository does not survive `sync`.** The
sync stage ends with `git clean -fd -e graphify-out`, which deletes untracked
files in every clone and exempts only `graphify-out/`. So the per-repository
placement above is a build step to re-apply after every sync, never the source
of truth for what an estate excludes. Nothing announces its disappearance; the
symptom is `status` reverting to reporting symlinks it had previously called
excluded.

One further trap, and it is narrower than it first looked: `collect_files()`
called directly with a **relative** path silently does not apply
`.graphifyignore` — it returns everything, with no error. The **CLI resolves the
path first**, so `graphify update .` from inside a repository, which is the form
the build skill documents and the pipeline uses, honours the ignore file
normally. So this bites the Python API, not the documented route. Measured both
ways; an earlier revision of this guide stated it as a general rule about
extraction, which would have had operators adding an absolute-path requirement
they do not need.

## 6. The prose layers, and what they cost

Three layers are LLM-authored rather than derived, and each has a two-part
shape: a deterministic extract, then written prose merged back with validation.

- **Community summaries** are the retrieval surface — they become "what these
  areas do" in composed answers, so their vocabulary matters as much as their
  accuracy. This is the expensive layer: one paragraph per significant
  community, thousands of them on a large estate.
- **Topic briefs** should be demand-driven. Write one when a real question was
  answered badly, never by guessing what people might ask. A brief nobody
  needed is maintenance you have chosen for no reason.
- **Deep dives** suit a repository people keep asking about. They are dated
  evidence: stamp them with the build and the source commits, and regenerate
  rather than editing figures.

**Parallel authoring, and the trap in it.** Batch the work and dispatch one
agent per batch. Concurrency helps here far more than in extraction, where each
agent's own reading time dominates — a several-thousand-summary backfill is
bounded by how many agents you can run, an extraction sweep is not.

Then the trap, which cost 141 summaries before it was noticed: **wait for every
agent to report, not for its output file to appear.** An agent writes, then
validates, then may rewrite — so the file exists before the agent has finished
with it. And when you merge, **reconcile the count accepted against the count
written**. A merge reporting "361 merged" reads like success; it was a 28%
silent shortfall, and the discrepancy was the only signal.

## 7. Refresh economics

**Run each store from its own virtual environment.** A store that pins this
library in a lock file and then runs it from a shared interpreter has not pinned
anything: whichever session installed last decides what the build used, and the
lock file records an intention rather than a fact.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
.venv/bin/knowledgestore status          # not the bare `knowledgestore`
```

Measured while two stores shared one machine: a store's lock pinned `0.11.3`
while the interpreter on the path held `0.11.5`, and nobody could say who had
upgraded it. The cost is not theoretical — it invalidated a control run, because
output labelled as one version had been produced by another, and the only reason
it was caught was that the newer version printed a line the older one did not.

The same reasoning as pinning the hash seed below, one level up: a build is
reproducible when everything that decides its output is recorded, and the
interpreter decides more than the lock file does.

**Pin the environment before you cluster, or the graph cannot reproduce itself.**

```bash
export PYTHONHASHSEED=0     # before any clustering run
```

Without it, clustering the *same graph file* in three separate processes yields
three different community memberships. Measured independently on two estates —
7,789 / 7,794 / 7,795 communities on one, 42,656 / 42,629 / 42,646 on another,
and byte-identical once pinned. On the second, the *committed* graph turned out
to be a fourth value, so that store could not have reproduced its own published
clustering and nothing said so.

The cause is not graphify's, and knowing which layer it sits in matters because
two other explanations look identical. graphify's partitioner is sorted and
seeded and **is** deterministic — measured, 3/3 identical. The variance is
downstream, in the pass that splits oversized communities with networkx's
Louvain, where `seed=` fixes that algorithm's node shuffle but not the iteration
order of the node-id **sets** it aggregates between levels.

**It is input-dependent, and its rate is not portable between estates.** Swept
across 28 real communities of one graph, three processes each:

| community size | unstable |
|---|---|
| 100+ | 11 of 12 |
| under 20 | 1 of 16 |
| any size, `PYTHONHASHSEED=0` | **0 of 28** |

Two other estates then tried to reproduce it on single large communities and both
came back stable — 899 nodes on one, 2,832 on another, the latter *larger than
anything in the sweep above*. If 11-of-12 were a per-community probability, two
independent stable draws would be a 0.7% event; it is far likelier that the rate
differs between graphs. Those twelve also come from one graph and share its
structure, so 11/12 is that estate's rate rather than a general one.

So: instability is size-correlated **within** a graph, and its overall rate
varies **between** graphs.

That is the argument for a blanket pin, and it is stronger than "one clean run
misleads you". **The rate does not transfer.** A store that tests carefully and
concludes it is unaffected has learned something about its graph today and
nothing about the next estate, the next refresh, or a community that grows past
whatever it happened to sample. Pinning removes a question that cannot be
answered cheaply and whose answer would not travel if it could.

**If you ever do check it, hash the membership — do not compare counts.** Of the
12 unstable communities, **4 returned an identical community count with different
membership**. A count-based check would have passed on a third of the real
failures, which is the same count-versus-content trap as a ticket join that
matches nothing while every total looks healthy.

The near-tie explanation predicts the size gradient and has not been measured
directly; treat it as the working mechanism rather than an established one.

The cost lands where it hurts most. Loss rate rises with community size — 0.86%
of communities with 5+ members, 1.13% at 10+, **2.12% at 20+** — and large
communities are exactly the ones carrying authored prose. A flat "1.5% of
communities move" understates it against any summary-weighted measure.

**Record which partitioner ran, too.** graphify uses Leiden when `graspologic`
is installed and falls back to Louvain when it is not, so two operators on one
corpus produce different partitions and read it as the corpus having changed.
Pinning the hash seed does not help if the partitioner differs.

```bash
knowledgestore record-clustering    # -> graphify-out/clustering-inputs.json
```

Run it in the environment that clustered, immediately after clustering, and
commit the record beside the graph. It records what *that* environment offered
and cannot know what a clustering it did not run was built with — and it refuses
on an unclustered graph rather than naming a partitioner for communities that do
not exist.

`status` then reads that file — never the graph — and compares it against the
environment it is run in, so a mismatch arrives as one line instead of an
unexplainable retention collapse:

```
Clustering partitioner: graphify-out/clustering-inputs.json records Leiden
(graspologic); this environment has Louvain (networkx), so a re-cluster here will
NOT reproduce those communities - the ids move, and `summaries remap` reports
retention loss with no cause in the corpus.
```

A store with no record is reported as **unknown**, never as agreement. Whether
this environment has graspologic is decided by attempting graphify's own import,
because anything inferred instead — a pinned extra, a lock file, an installed
version — can disagree with what actually ran.

**Re-clustering is the cost centre.** Adding repositories moves community ids
and strands committed summaries, which must then be remapped by membership
overlap (carry a summary only where the new cluster holds a convincing
majority — 60% works — and drop it otherwise rather than misattach prose).

**That bar measures recall, and a precision floor now measures fit.** It asks how much of the *old* cluster
landed together, and nothing about how much of the *new* cluster those members
constitute. A summary can therefore clear the bar and still describe a small
corner of the cluster it lands on — most of that cluster being newly arrived
members the prose has never seen. Retention is a coverage number, not a
correctness one: treat carried prose as owed a re-read, and split `verify`'s
flag rate by carried-versus-authored rather than reading the headline.

`remap` now also drops a summary whose target cluster grew so much that the
prose describes a corner of it (`--precision`, default 20%). Measured on one
refresh: a community of 37 members grew to 458 with *every* old member
retained — recall 1.00, clearing a 60% bar comfortably, precision 0.08. The
default is deliberately low, because on that estate 93.5% of carried summaries
already described 80% or more of their cluster and re-authoring costs real
money; the run prints the whole distribution so an operator can tighten it
against their own numbers rather than a guess.

**The damage scales with what you add, not with the act of re-clustering:**

| Change | Summary retention |
|---|---|
| +70 repositories | 54% |
| +6 repositories | 93% |
| +0 repositories, sources refreshed only | 71% |
| −12 repositories, 1.8% of nodes | 88% |

This matters because the intuitive response — batch every addition to avoid
re-clustering — is wrong. A small, well-motivated addition is cheap. Measure
retention on each refresh instead of assuming, and treat the backfill as a
known, bounded cost rather than a disaster.

**Removal costs roughly what an addition of the same size costs**, and the split
`remap` reports separates the unavoidable part from the incidental. Of 635
summaries dropped when 12 repositories left a 156-repository estate, 161
described the departed repositories — a correct loss, and one you can predict
before starting by grepping the summaries for their names — while 291 fell below
the overlap bar and 183 were merged-cluster collisions. Only the first group is
caused by the removal; the rest is the price of re-clustering, and would have
been paid by any refresh.

**Most of a re-cluster's cost is avoidable and often self-inflicted.** Clustering
from scratch renames most communities, and every summary keyed to a renamed id is
dropped for no reason connected to the change. Passing the previous membership
through `remap_communities_to_previous` is what turns that into the 88% above; a
run that skipped it renamed 22,507 of 28,004 communities on the same graph, which
would have stranded almost every summary in the store. Retention is therefore a
measure of your procedure at least as much as of the estate change.

**Re-extraction is usually a fraction of the estate.** A repository's own graph
does not change because another one left, and over five days only 14 of 156
repositories had moved at all. Recording each clone's `HEAD` before `sync` and
re-extracting only what differs turns hours into minutes; the merge, not the
extraction, is then the floor.

**A refresh that adds nothing is not free.** The last row is the surprise: the
estate was unchanged and only the sources moved on, yet retention was worse than
a six-repository addition. Enough source churn re-shapes the graph on its own —
that refresh consolidated roughly 39,000 communities into 28,000, and most of
the loss was merged-cluster collisions rather than summaries falling below the
overlap bar. Two clusters that merge can keep only one summary between them.

So budget backfill for any refresh, not only for additions, and read the split
that `remap` reports: collisions mean consolidation, whereas drops below the bar
mean genuine drift. It also pays to know how long the authoring costs — roughly
1,500 summaries took about thirty parallel subagents and a few minutes of
wall-clock, which is small against the rebuild itself.

**Order constraints bite.** Some stages rewrite artefacts others read: build
the page after every layer it embeds, and re-compress the graph after any stage
that mutates it. When a store's health check tells you the page is older than a
layer, believe it.

**Path format is a real trap in a multi-repository merge.** Per-repository
extraction and incremental updates can disagree about whether a source path is
repository-relative or prefixed with the sync directory. Normalise before
clustering, gate the normalisation on a plausible node count, and verify that a
downstream join still works — a silent format drift produced zero
step-definition edges once, and nothing else noticed.

## 8. Keeping a store honest

- **Never state a count in prose.** Repository, node and coverage figures are
  build outputs; every one written into a README or a skill has gone stale
  within a refresh or two and then misled somebody. Point at the generated
  report, the manifest and the health check. Dated evidence in a dossier is the
  exception, because the stamp is the point.
- **Record provenance.** Per-repository commit SHAs turn "the store is
  probably current" into a checkable claim, and let a dossier say exactly what
  it was measured against.
- **Regenerate prose, do not hand-edit it.** Editing an LLM-authored summary to
  match a rename is how a brief stops matching the evidence it cites. Either
  regenerate, or leave it and say it is stale.
- **Commit the browser artefact if unlicensed users need it, and know the
  price.** A self-contained page of tens of megabytes is one file that deltas
  badly, so history grows substantially per refresh. That can be the right
  trade — it is the only way someone without an LLM licence can use the store —
  but decide it consciously and do not casually add a second such artefact.
- **Verify what subagents produce; do not take their word for it.** Authoring and
  extraction fan out across agents, and every mechanical gate in the pipeline
  measures shape rather than truth — a batch of fabricated summaries passes
  coverage, length and merge validation. The dispatching agent has to check
  grounding, and it is not delegable to the author. `docs/grounding-and-verification.md`
  states the contract and the techniques, cheapest first.
- **Send findings out as exports, not as store content.** When a query turns up
  something a team must act on, run `/knowledge-store:knowledge-store-export` and attach the
  dated export to the ticket that will own it.
  Keep those out of the committed store: an export is a derivative, it is often
  sharper than the store it came from, and a ticket has the access control,
  owner and lifecycle that a file in a widely-cloned repository does not. For
  anything sensitive, the export carries locations, masked shapes and a
  regeneration command — never the values, because every copy of an export is a
  fresh disclosure.
- **Keep a regression suite of real questions.** Not unit tests of the scorer:
  actual questions from actual users, asserted against the built page. It is
  what catches a silent break in a join, and every new layer deserves a shape
  in it. On that estate this caught a file-to-ticket join failure that no
  other gate saw.

## 9. Asserting the store still answers

A refresh can leave every count healthy and every artefact well-formed while the
store has quietly stopped answering the questions it was built for. Two operators
of separate estates each discovered this and each built a regression suite for it
independently, neither knowing the other had (#134). The library now ships the
runner, so the third estate does not have to.

**The library owns the runner; the estate owns the questions.** A question like
"what is crime case readiness?" means nothing on another estate - generalise the
questions and every store fights the result, generalise the runner and every store
gets the gate for free.

```bash
cp examples/questions.txt config/questions.txt   # then make them your own
knowledgestore check-answers                     # did we publish something broken?
knowledgestore check-answers --candidate PATH     # are we about to?
```

Both positions matter. A gate that can only read the published page is a
post-mortem tool wearing a gate's clothes: one estate's suite reported 12/12 while
a rebuild sat unexamined, because every check read the *published* artefact and
would have gone on reporting 12/12 however bad the candidate was. Pointed at the
candidate, it failed immediately on the two things that were actually wrong.

### What it asserts

Answer **shapes**, not answer text - `brief`, `dive`, `tickets`, `graph`,
`ticket`, `abstain`. A harness pinning prose is red after every refresh that
legitimately reworded something, and a harness that is always red is one nobody
reads.

The assertions run in Node against the shipped page, because `assets/app.js` is
the ranker every consumer uses. An earlier attempt approximated it in Python with
keyword overlap and was measured and discarded: at one shared term "what is the
data retention policy?" routed to tickets on the word *data*; at two, a genuine
graph question collapsed to nothing. The failures were in opposite directions, so
it was not a threshold to tune - it was a second implementation of routing.

### Two things it does deliberately

**A pass rate is decomposed by mode, and each mode carries a zero floor.** A
composite is a weighted average of parts that fail independently, so a healthy
majority always masks a dead minority: 18 of 20 passing reads fine while every
`graph` question in the set abstains. The floor needs no estate-shaped threshold -
if a mode has questions declared and none pass, that is a finding whatever the
total says.

**Every finding names the artefact it read.** Each miss that motivated this was
false testimony rather than silence - something was counted, and the number meant
something other than it appeared to. None of them could have been written down
naming its source.

### Declare at least one `abstain`

A store that answers everything is not answering well - it is failing to say when
it has nothing, and that is the assertion nothing else makes.

**Every term in such a question must be absent from the estate's vocabulary.** The
engine abstains only when it has evidence for none of them, so a single ordinary
word produces an answer. Measured on real estates:

| question | why it answered |
|---|---|
| how is quantum chromodynamics **configured** here? | `configured` expands to settings, setup |
| what is the production **database** password? | `database` is everywhere |
| how is paediatric anaesthetic dosage **calculated**? | `calculated`, and nothing else |
| does the **orchestra** perform on Tuesdays? | `orchestra` matches `orchestrator` |

The last is the one to remember: the expansion is **morphological, not semantic**,
so "pick something obviously from another domain" is not a strategy. `orchestra` is
as far from a court estate as a word gets and it still matched. Two or three terms
from an unrelated technical field, with no everyday verb among them, is what works -
`gluon confinement lattice chromodynamics` abstains on both estates that tried it.

Three of those four were written by people who had already read this warning,
including its author.

When a declared `abstain` does answer, the gate names the terms the estate turned
out to have, so the fix is to reword the question rather than to go looking for a
defect.

**Why this section exists at all is worth stating**, because the mistake generalises
past `abstain`. The first version of `examples/questions.txt` shipped with two
`abstain` examples that would have failed on any real estate - they were written
against the synthetic fixture, where an estate's vocabulary is a dozen words, and
nothing tested them against a real one. Every mechanical check passed: the file
parsed, the modes were valid, the fixture went green.

That is the stale-fixture class - a claim about the shape of real input that was
only ever checked against invented input - and a fixture cannot catch it by
construction. One run against a real estate did, in seconds. When you write anything
that asserts what real data looks like, run it against real data before you ship it.

## 10. Reading a large graph from your own scripts

Most things that read a graph want a few fields per node, not the graph. A store's
own scripts are where this bites hardest, because they are written against a
subset and then meet the whole estate.

```python
from knowledgestore import graph_stream

for node in graph_stream.iter_array(path):                  # "nodes"
    ...
for edge in graph_stream.iter_array(path, key="links"):
    ...
```

Gzipped or not - the suffix decides, so scanning the committed `.gz` costs no more
than the uncompressed form. Measured on a 785,493-node estate:

| | wall | peak RSS |
|---|---|---|
| streamed | 2.1s | **0.032 GB** |
| loaded | 5.3s | 3.75 GB |

That difference is why this exists rather than being a preference. The case that
prompted it: one estate's `merge-graphs` output is 1.5 GB on disk across 627,737
nodes, and the script that reads it does a pure per-field scan over `id`, `label`,
`source_file` and `repo`. Loading it needs **4.1-4.3 GB**, so on a 16 GB machine a
store can complete its own build and only just read the result.

### What loading a graph actually costs

Three graphs, two estates, both CPython 3.13 on arm64:

| graph | on disk | nodes | edges | edges/node | peak RSS | x file |
|---|---|---|---|---|---|---|
| cut + semantic | 0.10 GB | 72,370 | 104,874 | 1.45 | 0.40 GB | 4.16x |
| committed `graph.json` | 1.36 GB | 785,493 | 1,495,218 | 1.90 | 3.75 GB | 2.76x |
| `merge-graphs` output | 1.51 GB | 627,737 | 2,318,935 | 3.69 | 4.10 GB | 2.72x |

**There is no portable multiplier, and the useful part is why not.** The factor
*falls* as the estate grows, which is the opposite of what everyone involved
predicted. Nothing about total size explains it; **composition does**, and
consistently computed the cost per object falls as edge density rises:

| graph | edges/node | RSS per node | RSS per object |
|---|---|---|---|
| cut + semantic | 1.45 | 5,935 | 2,423 |
| committed `graph.json` | 1.90 | 5,126 | 1,765 |
| `merge-graphs` output | 3.69 | 7,013 | 1,494 |

Per *node* the numbers do not order - 5,935, 5,126, 7,013. Per *object* they fall
monotonically, because **an edge costs about a third of a node**: a merged edge is
a thin two-key dict, while a cut-graph node carries `community`, `label`,
`norm_label`, `file_type`, `_origin` and both repository attributes. Fitting the
three measurements to two coefficients gives roughly `3,150 B/node + 1,045 B/edge`,
which reproduces the two larger graphs to within 0.3% and misses the smallest by
21%.

**Treat the coefficients as the reason, not as a formula.** Three points fit two
parameters, which is not a law, and the 21% miss is the honest warning: that graph's
nodes carry 11.1 fields against the merged file's 8.0, and the model assumes every
node costs the same. Shape is the variable, and attribute richness is part of shape.
So: **expect roughly 3x, up to 4x for a node-heavy graph, then measure your own** -
that range is the two coefficients, not a rule of thumb.

Two estimates were wrong before these were measured, in opposite directions. 6.6 GB
came from scaling the smallest graph's 4.16x and was 1.6x too high; the correction
offered for it predicted the factor would *rise*. And the first version of this
section reported per-object costs that did not order - because the middle row's
figure was bytes-per-object **on disk** while the outer two were per-object **in
memory**. Mixing two quantities in one sequence hid a monotonic relationship and
turned a real finding into an apparent absence, which is why this table names the
metric in every column heading.

**Quote max RSS and peak footprint together, or neither.** Their ordering inverts
between the small and large files above - 0.40 against 0.39 GB on one, 4.10 against
4.32 GB on the other - so either alone makes one file look better than the pair
does.

**A truncated file raises rather than returning what it read.** A caller counting
nodes would otherwise receive a smaller number indistinguishable from a real one,
which is the shape of every expensive mistake in this pipeline. An *absent* array
yields nothing and does not raise, because absent and empty are both legitimately
"no such content".

Two things to know before writing your own version instead, both of which were got
wrong here first:

- **Advance an index; never re-slice per object.** `buffer = buffer[end:]` copies
  the remainder once per object, and on that estate measured **13.1s against 5.3s
  for simply loading the file** - two and a half times slower than the thing it
  replaced, with an excellent memory graph. `raw_decode` takes a start index.
- **Read size sets peak memory; the compaction threshold barely matters.** 64 KiB
  gives 0.030 GB and 4 MiB gives 0.239 GB, with wall clock flat across the range.
  Two operators saw a 6x difference in peak between their implementations and put
  it down to one machine being under memory pressure. It was this constant. Peak
  allocation is a property of the code; pressure costs time, not peak.
