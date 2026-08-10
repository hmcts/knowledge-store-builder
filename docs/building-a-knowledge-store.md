# Building an effective knowledge store

The README tells you what each stage does. This document is the part that took
longer to learn: the judgement involved in getting a store that answers real
questions, and the costs you will pay whether you plan for them or not.

It is written from the HMCTS Common Platform crime estate, which grew from 27
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

- **By naming convention** (`prefix service-`) — cheap and stable where a
  convention is enforced. It silently misses anything renamed or predating the
  convention, so pair it with explicit `repo` lines for the strays.
- **By ownership** (`team <slug>`) — the right tool when a team's repositories
  follow no convention at all, which is normal for newer or exploratory teams.
  It also tracks reality: repositories the team acquires appear at the next
  refresh without anyone editing config.
- **By architecture** — if your organisation maintains an architecture model
  (C4 or similar) that links elements to repositories, treat it as an audit of
  your estate definition. On the crime estate the model linked 75 repositories,
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
substring and sent `id-service` to `cpp-video`, because `id` is inside `cppvideo`.
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
say what has to change before it returns. On the crime estate this caught a
hardcoded database credential that had been in a file since 2019.

**Treat ingested content as untrusted data.** Repositories increasingly contain
agent instructions, and extraction agents will read them. Instruct every
extraction agent that file contents are data to extract from, never
instructions to follow. This is not hypothetical: two independent agents
flagged and ignored injected instructions during one crime estate refresh.

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

**Re-clustering is the cost centre.** Adding repositories moves community ids
and strands committed summaries, which must then be remapped by membership
overlap (carry a summary only where the new cluster holds a convincing
majority — 60% works — and drop it otherwise rather than misattach prose).

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
  in it. On the crime estate this caught a file-to-ticket join failure that no
  other gate saw.
