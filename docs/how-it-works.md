# How a knowledge store works

The mechanisms behind a store, for readers who want the science rather than
the operating instructions. Every claim here is checkable: each section names
the source file that implements it, and the constants quoted are the ones in
the code. Measured economics live in the
[operator guide](building-a-knowledge-store.md) — one home per fact.

## The constraints that shape everything

Four constraints, chosen up front, decide most of the design:

1. **The LLM runs at build time, never on the page.** Whoever builds a store
   may have a licence; the people querying it may not. Everything a model
   writes is validated and committed as static text.
2. **The store is committed files.** Consumers clone and read. There is no
   server, no database, no query-time network access.
3. **Deterministic where possible.** Extraction, indexing and page
   composition are pure functions of the sources: two runs on the same inputs
   produce byte-identical output. Compression included: every gzip writer uses
   level 9 with neither timestamp nor filename in the header — the behaviour
   of `gzip -9 -n` (`src/knowledgestore/io.py`, `gzip_text`). That *every*
   is held by `tests/test_gzip_writes_stay_deterministic.py`, which reads the
   source for a gzip write that bypasses the helper: one had, and the
   artefact it wrote churned on every run.
4. **Every claim traces to evidence.** The
   [grounding contract](grounding-and-verification.md) governs anything a
   model authored; absence of evidence is reported as a finding, not filled.

Everything below is a consequence of holding those four at once.

## The graph layer

Each repository is extracted **from inside its own clone**
([graphify](https://github.com/safishamsi/graphify), AST-based), then the
per-repository graphs are merged in one operation. The merge namespaces node
ids by repository and stamps a `repo` attribute; `source_file` stays
repository-relative, which matters because it is the join key the
file-to-ticket index matches against. Extracting from anywhere else writes
paths that silently break that join.

**BDD specifications become graph nodes**
(`src/knowledgestore/extract_gherkin.py`). Feature files contribute feature
and scenario nodes; step definitions in Java, Python and TypeScript are
linked to the scenarios that use them by normalising both sides to a common
form — Cucumber expressions (`{int}`, `{string}`), typed parameters
(`{amount:d}`), regular-expression groups, quoted values and outline
parameters all reduce to the same placeholder, so a business step matches
whichever language declared it. Data tables and doc strings are deliberately
not modelled — they are example data, not structure — and the parser is a
measured structural subset: `Rule:`, `Example:` and localised keywords appeared
zero times across the 1,266 feature files it was built against.

**Communities** are detected by modularity optimisation over the merged
graph, driven by graphify: graspologic's Leiden where that library is
installed, seeded networkx Louvain otherwise — the same seed either way, so
like-for-like reruns are stable and churn is graph-change-driven. Which of the
two ran is therefore an input to every community id, and machine-dependent:
`knowledgestore record-clustering` writes it to
`graphify-out/clustering-inputs.json`, and `status` reports when the environment
it runs in could not reproduce what that file records.
Community ids are not stable across runs — nothing in modularity optimisation
anchors them — so two mechanisms restore continuity:

- `remap_communities_to_previous` aligns a new clustering's ids with the
  previous one by membership overlap, so a cluster that survives keeps its
  identity;
- every saved community label carries a **membership signature** (a hash of
  its member set). A later run reuses a saved name only while the signature
  matches; a community whose membership changed gets renamed from its hub
  rather than keeping a name that now describes something else.

**Cross-repository package edges** are the one place the graph connects
repositories directly (`src/knowledgestore/build_package_edges.py`): a node
per shared npm package, citing the `package.json` that declares it, with
edges from the files that import it — each edge citing the importing file.
Deliberately declaration-level: measured on a real estate, symbol-name
collisions across repositories were template scaffolding and vendored
copies — independent implementations, which the store already answers
correctly — so symbol-level identity (SCIP) stays deferred until
package-level answers prove insufficient.

**Source links** make a citation followable (`src/knowledgestore/source_urls.py`).
Each node gets a `url` naming its file on github.com at the commit recorded in
`knowledge/provenance.json`, with the line from `source_location` where there
is one. A node gets no link rather than a wrong one: a repository on another
host, or one with no recorded commit, is reported and left unlinked. The sha is
the head `sync` recorded, not the commit each graph was extracted from, so a
refresh that syncs a repository without re-extracting it pins the old graph's
line numbers to the new commit.

What id continuity cannot fix is **splits**: re-clustering from scratch can
shatter a stable community into fragments, and prose attached to the whole
does not describe a part. The measured cost of that, and the economics of
refreshing generally, are in the operator guide §7.

## The relationships layer

Which repositories depend on which others is answered from their own
declarations, not from the graph (`src/knowledgestore/relationships.py`, written
by `knowledgestore relationships`). Four kinds are read:

| Kind | Declared in | Target |
|---|---|---|
| `terraform_module` | a `module` block's `source` | the repository a GitHub source names, or the address as written |
| `helm_chart` | `Chart.yaml` `dependencies`, and a Flux `HelmRelease`'s chart | the chart name |
| `maven_artifact` | `pom.xml` `<dependencies>`, including under `<profiles>` | `groupId:artifactId` |
| `npm_package` | `package.json` `dependencies`, `devDependencies` and `peerDependencies` | the package name |

Each reference resolves to a repository in the estate, to the repository that
declared it, or to nothing the store holds. A reference whose resolution lands on
its own repository (a sibling Maven module, a module source naming its own
repository) is counted under `skipped` as `self` and is not an edge. Publishing the
target is not enough: an umbrella that also defines a chart another clone owns
depends on that clone, and a coordinate two clones publish resolves by the conflict
rule below, whichever of them declared it. A local module path is counted under
`skipped` as `local`. A target the store does
not hold is never dropped: `unresolved_targets_outside_estate` lists it per kind,
with how often it is referenced and every repository that references it.

**Three outputs**, under `knowledge/relationships/`:

- `by-repo/<repo>.json`, one per clone: its `depends_on` and `depended_on_by`
  edges, the `commit` and `base_url` from `knowledge/provenance.json` (`null` when
  none is recorded), and `extra`, copied from `extra/<repo>.json` when the store
  writes one. The stage never interprets `extra`.
- `repository-dependencies.json`: every edge with both ends, the unresolved
  targets, and `conflicts`. Its top level opens with
  `"format": {"layer": "relationships", "major": 1, "minor": 0}`, the stamp that
  says whose layer this is: major changes for an incompatible change, minor for an
  addition. A reader refuses a file with no stamp or another major, so a store's
  own differently shaped `knowledge/relationships/` is never read as this layer.
  `kinds.json` carries no stamp; readers find it here.
- `kinds.json`: per kind, its origin, what its pins are (`pins`), and its counts
  of edges, references and unresolved targets, plus what was skipped and why. It
  is small enough to read without opening the others.

**An edge** aggregates every reference from one repository to another, per chart:

```json
{"repository": "<other end>", "chart": "", "references": 7,
 "kinds": {"terraform_module": 7},
 "refs_pinned": {"v4.2.0": 5}, "refs_unpinned": 2,
 "examples": [{"url": "https://github.com/<org>/<repo>/blob/<sha>/<path>#L12",
               "line": "source = \"git::…?ref=v4.2.0\""}]}
```

`refs_pinned` counts each pin as written, and `refs_unpinned` counts the
references that give none (floating), so the two sum to `references`, the full
count. What a pin is depends on the kind, and `kinds.json` says so in each kind's
`pins`:

| Kind | `pins` | A pin is |
|---|---|---|
| `helm_chart` | `chart versions` | a `Chart.yaml` dependency's `version`, or a `HelmRelease`'s chart `version` |
| `terraform_module` | `git refs` | the `ref=` of a module source |
| `maven_artifact` | `version specs` | a dependency's `<version>`, properties resolved within the POM |
| `npm_package` | `version specs` | the version range as written (`^2.1.0`) |

A store's own kind has `pins` `as the store's parser writes them`: the phrase is
the library's, never text from the store. `examples` holds at most
`KSB_RELATIONSHIP_EXAMPLES` (default 3), sorted by path and line, and each `line`
is passed through the secret mask before it is written, as is every pin, unresolved
target and conflict key. `chart` is `""` for every
kind but `helm_chart`, so one repository of shared charts yields an edge per
chart rather than one edge that hides which.

**A chart resolves** to the first of these that holds:

1. `KSB_CHART_REPO_PATTERN` with `{name}` replaced, when it is set and names a
   clone. It is empty by default.
2. The clone whose `Chart.yaml` has `name: <chart>`, other than an umbrella. The
   `Chart.yaml` can be at the clone's root or in any subdirectory
   (`<clone>/<chart>/Chart.yaml`), unless it is vendored (below).
3. An umbrella (`KSB_UMBRELLA_REPOS`) that defines it.
4. For a Flux `HelmRelease` only, a `sourceRef` that names a clone.
5. Otherwise, unresolved.

A `Chart.yaml` under another chart's `charts/` directory is a vendored copy of a
dependency, and is neither published nor read. When two clones publish one chart,
coordinate or package, the first by name wins and both are listed under
`conflicts`. A Terraform GitHub source belongs to the estate only when its
repository is a clone and its organisation is that clone's own, ignoring case. A
clone's organisation is read from its clone URL in `config/repositories.txt`, so a
store holding clones from several organisations matches each against its own. A
clone whose URL is not on GitHub, or that the list does not name, takes
`KSB_GITHUB_ORG`, and an unset organisation matches any. A source outside the
estate is reported as `<org>/<repo>`.

The graph reads the same declarations through the same parsers. The `packages`
stage's `USES_MODULE` edges and the `gaps` report take Terraform sources from a
module block's own literal `source`, with the organisation check above, and the
`deployments` stage's `uses_chart` edges resolve a chart in the order above. So
the graph, the report and this layer name the same repository for one
declaration, and the two chart settings apply to both.

A file that cannot be opened, a POM that does not parse, or a file naming a
`HelmRelease` that does not parse as YAML, is counted as `unreadable` for its kind
and named in the run's output. The rest of the estate is still read. A bare `=` in
a release's values (`op: =`) is read as the string `"="`, as Kubernetes reads it,
by this layer and by the `deployments` stage alike.

**No reference vanishes.** Every reference the parsers read ends as exactly one
of these: a reference in an edge, a reference to an unresolved target, or a skip
counted under its reason. Before writing, the stage checks this per kind: the
references read, plus the declarations a parser set aside (`local`), must equal
the references in edges, plus those of unresolved targets, plus every skip but
`unreadable`, which counts files rather than references. A kind that does not
balance stops the stage with exit 1, naming the kind and each figure, and nothing
is written. Two runs over the same clones write identical bytes, and a
repository no longer cloned loses its `by-repo` file in the same run.

**Comparing two builds.** `knowledgestore relationships --compare PATH` builds
the layer, writes nothing, and prints every (consumer, provider, chart, kind)
whose references or pins differ from PATH's, and every unresolved target that
differs, exiting 1 on any difference. PATH must be a
`repository-dependencies.json` in this stage's own layer format, such as one an
earlier run wrote; `--compare` reads nothing else. A file of any other shape, or
with no format stamp or another major, exits 1 with `Cannot compare against PATH: …` and compares nothing. To check
parity with a script a store already runs, the store converts that script's
output to this format first, then compares.

A Flux `HelmRelease` is read with PyYAML, which the `deploy` extra installs
(`pip install 'hmcts-knowledge-store-builder[deploy]'`). Without it, a run over a
clone holding a release stops with exit 1 and writes nothing, naming the file, the
missing library and the extra, because a release the stage cannot read would
leave its references out of a layer that reads as complete. A release is a file
with a `kind: HelmRelease` line; one that only mentions the word, such as a
pipeline running `flux reconcile helmrelease`, is not. An estate holding no
release builds without PyYAML.

### A store's own kinds

A store whose clones declare a dependency the four kinds do not read, such as an
event subscription or an internal manifest, gets its edges, pins and unresolved
targets in the same layer by registering a kind in
`config/relationship-kinds.json`:

```json
{"event_subscription": {"module": "scripts/subscriptions.py",
                        "references": "references",
                        "publishes": "publishes",
                        "globs": ["**/*.events"]}}
```

`knowledgestore relationships` then runs the kind over every clone. Its edges
carry the kind's name, and `kinds.json` lists it with `"origin": "store"` beside
the built-ins' `"origin": "builtin"`. The file is the entry point because a store
repository is not a Python package, so there is nothing to install one from.

The module defines two functions, named in the file:

- `references(clone, rel, text)` reads one file whose path matches `globs` (`rel`
  is relative to `clone`, a `pathlib.Path`) and returns a list of
  `knowledgestore.relationships.Reference`. A `ValueError` means the file cannot
  be read: it is counted as `unreadable` and named, and the stage goes on. Any
  other exception stops the stage with exit 1, naming the kind and the file, and
  writes nothing, so the previous layer stays as it was.
- `publishes(clone)` returns `(target, repo-relative path)` pairs for what the
  clone offers to others, and `[]` for a kind that only references.

Each `Reference` must carry the kind's registered name as its `kind`: a
reference under another name would resolve against another kind's publishers and
vanish, so the stage stops instead. `chart` must be empty, because only
`helm_chart` has one. A parser that returns anything but a list of `Reference`,
or a `publishes` that returns anything but pairs of strings, stops the stage the
same way, naming the kind and the file (for `publishes`, the clone).

A kind follows the rules the built-ins do. Resolution, the `self` rule,
conflicts, unresolved targets and their `referenced_by` are the layer's, not the
parser's. A credential in a `Reference` is dropped (`user:token@` in a URL) or
masked before anything is written, whichever kind produced it. Store kinds are
loaded in name order.

A declaration the stage refuses, with exit 1 and before any store code runs:

- a kind name that does not match `^[a-z][a-z0-9_]*$`, or that is a built-in's;
- a `module` that is absolute, contains `..`, or resolves (through a symlink or
  otherwise) outside the store root, or that is not a file;
- `globs` that is not a non-empty list, or a `references` or `publishes` that is
  not a function name.

Every declaration is checked before any module is imported, so a bad declaration
never follows store code that has already run. Only then are the modules
imported, and a named function that a module does not define stops the stage the
same way.

**Trust boundary.** A store's module is Python the stage runs with the builder's
own permissions: it can read and write anything the operator can. The stage loads
it only from inside the store root, and never adds the store directory to
`sys.path`, but it does not sandbox it. There is no timeout: a parser that
hangs hangs the stage, as any operator code would. A module cannot import a
sibling with `import x`, because the store directory is not on `sys.path`; load it
through `__file__`. Register code you would run from the same checkout by hand.

## The history layers

Commit history is exported in **one `git log` pass per repository**
(`src/knowledgestore/export_git_history.py`), with file statistics riding
along via `--numstat --diff-merges=cc` and machine-readable record
separators. The naive implementation — one `git show` per commit — was
measured spawning 195,360 extra processes on a real estate; the single-pass
rewrite is pinned by a test asserting the dataset is unchanged.

The **intent index** (`src/knowledgestore/build_intent_index.py`) mines those
datasets: ticket ids matching `[A-Z][A-Z0-9]{1,9}-\d{1,6}` (configurable) are
extracted from commit subjects — and from commit bodies where the subject names
none — producing a file → tickets map with first and last touch dates, and a
per-ticket record of what its commits said.

That record keeps the commit text in **three fields**, because one curated
description can only ever carry one source: a description (the subject where it
says something, the body's opening prose where it does not), the subjects as
their authors wrote them, and the body prose. Keeping them apart is what makes
the terse subjects a description filter rejects, and the bodies that sit behind a
perfectly serviceable subject, reachable at all — both are primary evidence, and
neither survives being collapsed into a single field.

A ticket that landed can also have commits found only on a branch. Their subjects
and bodies go into two more fields, `ps` and `pb`, and never into the three above,
so a draft nobody merged cannot be quoted as what the ticket changed. Branch text
the landed commits also carry stays in the landed fields, because a branch left
behind by a rebase repeats subjects the default branch already holds. A ticket
found only on a branch keeps its text in the three main fields: the whole record
is marked `proposed`, and that text is all the evidence it has.

A body is reduced to prose before it is read: separators, a merge's list of
commits, git trailers, anything an automated author wrote, and each repository's
own recurring template lines are discarded, because none of them evidences
intent. Trailers are matched by **shape** as well as by name — a hyphenated key,
or a value of a single token — because no fixed list of names holds the trailers
a team invents, and the recurring-line filter cannot rescue the miss: a trailer
whose value is a unique hash never repeats, so it never crosses the repetition
thresholds. The shape test deliberately spares `KEY: what changed`, where the key
is a ticket or a rule id, since those lines are among the most useful a body
carries.

**A body is evidence only when a person wrote it, and the reliable signal is the
identity the commit records — not the words in the body.** Matching what a
machine *says* does not transfer, because each tool writes differently: one
dependency bot announces "Bumps [package] from X to Y" and another writes "Update
dependency package to vY", so a filter tuned to the first catches none of the
second. Identity generalises instead. The `[bot]` account convention covers
current and future GitHub App automation with no list to maintain, and
`KSB_AUTOMATION_IDENTITIES` covers the older automation that predates it — which
is why that setting exists. One caution, because it is easy to get wrong: an
address on a shared no-reply domain is **not** a bot signal, since real
contributors use those addresses too; only the local part is read.

Together this is why a store can answer *why* a file exists without any
issue-tracker API access.

### Redacting text that identifies a person or a record

Commit messages are written for colleagues, not for publication. Some describe
one particular record rather than the software — a reference to a case, a claim, an
account, a patient, whatever the organisation's subject happens to be — sometimes
the people involved, sometimes what happened to them. A store commits the text it
mines and its page embeds it, so **whatever is mined is republished**, to everyone
who can read the store: a wider audience than the repository the commit sits in.

Anything in a mined value matching a rule in `KSB_SENSITIVE_PATTERNS` is
therefore replaced before it is stored — as a description, as a subject and as
body prose alike.

**One rule ships, and only one.** An email address has the same shape everywhere,
so it is the only identifier this library can recognise without assuming a
jurisdiction or a subject domain. Every other format belongs to an organisation:
reference numbers for cases, claims, accounts or patients are locally defined, and
national identifiers and postal codes vary by country. A library that shipped one
country's formats would protect that estate and quietly miss every other one —
worse than shipping none, because it reads as coverage. **So each estate declares
its own**, and the run prints the rules in force so an operator can see what is
actually being applied rather than assuming.

**The matched span is replaced; the words around it are kept.** A commit message
is usually an account of a defect that was found and fixed, and that account is
exactly what a knowledge store exists to hold. "Page not loading for case
`[case reference withheld]`" is useful evidence about the software; discarding it
to remove eleven characters trades the whole record for the identifier. Each
placeholder names what was taken, so a reader meets a stated omission rather than
an unexplained gap.

A value left with nothing but placeholders is not stored, because it no longer
says anything about the change. What the commit *links* is unaffected either way:
the ticket keeps its dates, repositories, commit count and file entries, none of
which identifies anybody.

Every run reports how many values were withheld and under which rule, including
when the answer is none — a silent filter is indistinguishable from an estate
with nothing to withhold. A count above zero is a finding about the estate as
much as about the store, because the commit messages still carry that text.

Two limits, both deliberate:

- **Personal names are not detected, and they survive redaction.** Recognising
  them in commit prose is unreliable in both directions, and a rule that
  half-works invites reliance on it. So a value can keep a surname beside a
  removed reference — the identifier goes, the name stays. That is the cost of
  replacing spans instead of discarding values, and it is the reason a small set
  of affected values is worth reading rather than assuming the rules covered it.
- **This reduces exposure; it does not certify a file.** A clean result means
  nothing matched the rules — never that a file holds no personal data, and never
  that what remains is safe to publish more widely than the store already is.

Filtering as text is mined does nothing for an artefact already committed and
already embedded in a published page, so `knowledgestore check-evidence` gates
what is there and **exits non-zero** on a match. It names the ticket, the field
and the rule, and never the value: a gate that printed the text would copy it
into a build log, read more widely and kept longer than the artefact. It is a
stage of its own rather than a flag on `status` because `status` never fails by
design.

## The prose layer, and how it is checked

Community summaries are the one layer a model writes. The pipeline constrains
that in both directions
(`src/knowledgestore/build_community_summaries.py`):

- **Inputs are digests**: per-community evidence packs (label, size,
  repositories, top nodes with source files, business features, tickets).
  Authors are instructed to claim nothing the digest does not show.
- **`merge` validates shape**: unknown community ids are rejected, as is any
  summary outside 60–700 characters.
- **`remap` protects continuity**: it carries a summary only onto a community
  holding exactly the node set it was written about, and withdraws the rest to
  `communities-withdrawn.json` (`--carry overlap` restores the older
  60%-of-the-old-members tolerance, `--bar`). Three refusals stop
  `knowledgestore summaries remap` running at all: an implausibly small summary set (`--floor`, default 10), fewer than half
  the graph's nodes carrying a community (`--coverage`), and a snapshot that
  shares no node ids with the graph. Each would otherwise write a bad file over a
  good one and report it as legitimate churn.
- **`verify` checks grounding, not shape**: identifiers cited in each
  summary's prose are normalised (case and punctuation stripped), expanded
  with spelling variants (`.java` suffixes, dotted-name parts, `Test`
  pairings), and compared against the identifiers its digest contains. A
  speculation lexicon (*probably*, *likely*, *appears to*…) flags where
  evidence ran out. A citation absent from the evidence is either fabrication
  or paraphrase, and both warrant a human read — the
  [grounding contract](grounding-and-verification.md) explains why findings
  are a starting point rather than a defect count.

## The semantic bridge

Lexical matching cannot connect "court outcomes" to "hearing results". The
fix ships no model (`src/knowledgestore/build_semantic_index.py`): at build
time, the graph's own vocabulary — node labels, summaries, business features;
tokens of 4–24 letters, the 15,000 most frequent with document frequency ≥ 3
— is embedded once with MiniLM (`all-MiniLM-L6-v2`, 384 dimensions), and each
token's nearest neighbours at cosine ≥ 0.55 are written to one committed,
gzipped map. The page expands query tokens through that map at query time
with a dictionary lookup. Heavy model at build, small artefact shipped,
nothing at query time — the shape every ML addition here has to fit.

## The page

`explorer.html` is one self-contained file
(`src/knowledgestore/build_explorer.py`): the application script plus
embedded JSON blocks — the entry index sorted by degree descending, a flat
edge-pair array, ticket titles and commit-mined detail, community summaries,
and the token-neighbour map. Inclusion is degree-gated (default: 3
connections) and test files — sources matching `.spec.`, `__tests__` or
`/test/` — are excluded, except in repositories the estate declares as
end-to-end suites, where the tests *are* the business documentation.

**The entry index is dictionary-encoded, column by column.** Most of its bytes
are repeats of strings already in it — a repository name, a source path, a
community label — so each column's values are replaced by indices into a
per-column table, and the page application restores every row before anything
reads one. Which columns are encoded is decided at build time from that page's
own data, by one inequality: the bytes a column costs, less the table and the
indices that would replace it, has to come out positive against
frequency-ordered indices. There is no list of column names, because the largest
column of one estate's page need not exist on another's. A column whose values
are no longer than their indices loses however often it repeats — a single-digit
number in every row is the extreme case — so the build prints the verdict and the
counts behind it for every column, and a store can read which of its own columns
paid. Nothing is compressed: the browser parses less JSON rather than gaining a
step before parsing it, and the page stays one file that opens from disk.

**Each row opens its file at the build commit.** The page carries one link base
per repository in its configuration block, `https://github.com/<org>/<repo>/blob/<sha>/`
from the same helper that writes each node's `url`, and every row carries only its
line anchor. The page joins the two and escapes the path as the graph does, so a
row's link and its node's `url` are the same bytes. A URL per row was the
alternative and was rejected on size: every URL is unique, so the column could not
be dictionary-encoded, and on real stores it would have added about a third to the
page. A repository the graph cannot link - another host, no recorded commit - gives
its rows no link at all.

The explorer stage therefore reads `config/repositories.txt` and
`knowledge/provenance.json` itself, while the graph's `url` was written from them
when `source-urls` ran. Build the page after `source-urls` and against the same
sync: a `sync` in between pins the page to the new commit and the graph to the old
one. The stage compares a sample of the graph's links against its own and warns
on stderr when they differ; re-run `source-urls`, then the explorer.

**What the commits said is a retrieval surface, indexed per ticket.** The page
searches all three evidence fields the ticket artefact carries — the curated
description, the commit subjects as written, and the body prose — because a body
is where a breaking change, a renamed schema field or a pointer to a decision
record gets written down, and none of that is in any node label. The index is
built over tickets rather than over entries deliberately: the entry haystack is
built once per indexed entry, and the same ticket is carried by many entries, so
evidence inlined there is multiplied by every entry citing it — hundreds of
megabytes of haystack in the browser on a real estate, against a few megabytes
for one index over tickets, which is scanned linearly for the same order of work
a question already costs. What matches is shown as an additional section, never
in place of the composed answer, and each piece of evidence appears under the
name of the field it came from: a reader has to be able to tell a tracker title
from a commit subject from a commit body, because the store's grounding contract
turns on which of the three a statement came from. A landed ticket's branch-only
subjects and bodies are searched too, and shown under a label saying they came
from branch-only commits.

**Which tickets it returns is decided by how much each matched word tells you.**
Counting matched words ranks the generic above the distinctive — the same defect
recorded below for the node ranker — and a section headed as evidence is worse
wrong than absent. So each word is weighted by how rare it is in the ticket
corpus itself, measured there rather than in the entry index, because a word can
be everywhere in code names and nowhere in commit prose. Rarity alone is not
enough: in a corpus of terse subjects, ordinary English is itself rare, so a long
body matching two unremarkable words can still outvote the one word that carries
the question. Two further measures settle it, both ordinary information
retrieval — a length discount, because a long body matches anything by surface
area, and a bar below which a word is not decisive enough to justify showing a
ticket at all. No stopword list: which words are ordinary depends on the corpus,
no fixed list is ever complete, and both measures recalibrate from the data.

**Absence is disclosed, not hidden.** The ranker alone cannot keep the store's
"absence of evidence is a finding" promise: a question about something the
estate does not contain still scores, because its ordinary words match
thousands of entries. Measured on a real estate, an out-of-domain question
outscored a legitimate one, so no score or coverage threshold separates them.
What does separate them is whether a word matches anything at all — so the page
names the query words the index holds no evidence for, and answers with that
finding alone when no word is evidenced. It discloses rather than abstains
because nothing distinguishes a question word from a subject without an English
lexicon: an earlier rule that abstained when the rarest word was absent
silenced four legitimate questions, because ordinary words like "used" and
"taken" are themselves missing from a small corpus. The rule reads both indexes,
the entries and the ticket evidence: a word only a commit body holds *is*
evidenced, and naming it as absent directly above its own commit body would be
the page contradicting itself.

Ask-mode ranking is a port of graphify's own query scorer, and is documented
in the application source in one line: *IDF-weighted terms,
exact/prefix/substring/source tiers, full-query bonus, squared term-coverage
scaling. Deterministic — no AI.* Pre-written topic briefs are served whole
when a question hits their keywords; everything else is composed from the
graph by template.

Three test layers pin this: unit tests on the scorer maths
(`tests/explorer/engine-unit.mjs`), a synthetic-estate fixture built through
the real pipeline (`tests/explorer/fixture.py`), and a page regression that
drives every answer shape and asserts the built page inlines the application
source **byte-for-byte** — so the code the tests exercise is provably the
code that ships.

## Query index

`knowledgestore index` (`src/knowledgestore/build_store_index.py`) writes
`knowledge/index/store.sqlite` and its archive `store.sqlite.gz`, so that a reader
opens one file read-only instead of parsing a graph of hundreds of thousands of
nodes. The graph is streamed in and never loaded (the module `store_index` holds
the schema).

Every long node id is stored once. Edges, files and tickets hold integer keys, and
repositories, files and relation names are interned. Repeating the id strings was
several times the size on a large store.

| Table | Holds |
|---|---|
| `meta` | `index_schema`, the store-relative `graph` path that was read, and the first 16 hex characters of `graph_sha256` and `file_tickets_sha256` |
| `repos`, `files`, `relations` | the interned names; `files` is unique on `(repo, path)`, and a node with no repository has a NULL `files.repo` rather than a repository named `""` |
| `nodes` | `id`, `label` (NULL on a structural node), `kind` in its current form, `file`, `url` and `community` (NULL when absent) |
| `edges` | `source`, `relation`, `target` as integer keys, with an index on `(target, relation, source)` for incoming edges |
| `tickets` | `file`, `ticket`, `count` and `landed`, from `file-tickets.json.gz`, joined to a node through `(repo, path)` |
| `labels` | an FTS5 index over the node labels, with the `nodes` table as its content; an unlabelled node is not in it |

An edge whose endpoint is not a node, and a repeated node id or link, is counted
and reported rather than stored, so the stage's report reconciles: nodes read =
nodes + duplicates, and links read = edges + dangling + duplicates.

**Determinism holds per SQLite build.** Two runs on the same inputs in one
environment give the same bytes in both files, and the archive carries no
timestamp or filename. The database header records the SQLite library version, so
two different SQLite builds can write different bytes from the same store.

`status` reports whether the index is current. It reads `meta` from the plain file
only and never decompresses the archive. When `meta.graph` names the committed
`.gz`, it hashes that file and the ticket layer and compares them with
`meta.graph_sha256` and `meta.file_tickets_sha256`. When the index was built from
the plain graph, which can be gigabytes, `status` says freshness is not checked
rather than read it.

The queries a reader runs against the index are below. Each block is held by
`tests/test_store_index.py`, which runs it as written against a built index and
compares the rows with a hand-written list, so what follows is what the schema
answers. Bind the `?` parameters; never build the SQL from text.

Open the file read-only (`sqlite3.connect("file:store.sqlite?mode=ro", uri=True)`).
A node with no repository has a NULL `files.repo`, so every query that names a
repository joins `repos` with `LEFT JOIN` and reads `coalesce(r.name, '')`: an
inner join would drop that node without an error. `''` therefore means "no
repository" in a parameter as well as in a result.

**`find`** answers nodes whose label matches a full-text search, best match first
(`bm25`, then id, so a tie is stable). `?1` is an FTS5 `MATCH` argument, `?2` a
repository name or NULL, `?3` a kind or NULL, `?4` the limit, where `-1` means no
limit. Columns: `id, label, repo, kind, path, url, community`.

<!-- index-query: find -->
```sql
SELECT n.id, n.label, coalesce(r.name, ''), n.kind, f.path, n.url, n.community
FROM labels
JOIN nodes n ON n.k = labels.rowid
JOIN files f ON f.k = n.file
LEFT JOIN repos r ON r.k = f.repo
WHERE labels MATCH ?1
  AND (?2 IS NULL OR coalesce(r.name, '') = ?2)
  AND (?3 IS NULL OR n.kind = ?3)
ORDER BY bm25(labels), n.id
LIMIT ?4
```

Free text is not a `MATCH` argument. `NEAR`, `OR`, `*`, a column filter and an
unbalanced quote are all FTS5 syntax, and a user's words can hold any of them.
Split the text into words with FTS5's own `unicode61` tokenizer, the one the index
was built with, so `pay_gate` and `feature-gateway` become two words each, as they
are in a label. Quote each word, double any `"` inside it, and end it with `*` for
a prefix match. The words are then ANDed and none is an operator. A Python
tokenizer is not a substitute: SQLite's Unicode tables are its own, and an
imitation disagrees with the index on some characters.

<!-- index-helper: match_argument -->
```python
import sqlite3

def match_argument(text):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE words USING fts5(text, tokenize='unicode61')")
    db.execute("CREATE VIRTUAL TABLE terms USING fts5vocab(words, 'instance')")
    db.execute("INSERT INTO words(text) VALUES (?)", (text,))
    found = [t for (t,) in db.execute("SELECT term FROM terms ORDER BY offset")]
    db.close()
    return " ".join('"' + t.replace('"', '""') + '"*' for t in dict.fromkeys(found))
```

An empty result from `match_argument` has nothing to search for, so do not run
`find` with it. A server should also bound the text, for example at 1024 bytes
and 32 words, because every word is a prefix scan of the label index: do it before
the text reaches the helper.

**`node`** answers one node by id. Same columns as `find`.

<!-- index-query: node -->
```sql
SELECT n.id, n.label, coalesce(r.name, ''), n.kind, f.path, n.url, n.community
FROM nodes n
JOIN files f ON f.k = n.file
LEFT JOIN repos r ON r.k = f.repo
WHERE n.id = ?1
```

**`neighbours`** answers the edges of one node with the relation and the id at the
other end. `?1` is the node id and `?2` a relation name or NULL. The direction
column is `out` when the node is the source and `in` when it is the target.
Ordered by relation, then the other end's id, then direction.

<!-- index-query: neighbours -->
```sql
SELECT 'out' AS direction, rel.name AS relation, t.id AS other
FROM nodes s
JOIN edges e ON e.source = s.k
JOIN relations rel ON rel.k = e.relation
JOIN nodes t ON t.k = e.target
WHERE s.id = ?1 AND (?2 IS NULL OR rel.name = ?2)
UNION ALL
SELECT 'in', rel.name, s.id
FROM nodes t
JOIN edges e ON e.target = t.k
JOIN relations rel ON rel.k = e.relation
JOIN nodes s ON s.k = e.source
WHERE t.id = ?1 AND (?2 IS NULL OR rel.name = ?2)
ORDER BY 2, 3, 1
```

**`tickets_for_file`** answers the tickets that touched a file, or every file of a
repository. `?1` is the repository (`''` for none) and `?2` the repository-relative
path, or NULL for every file of that repository. `landed` is 1 for a ticket in the
landed history and 0 for a proposed one. Landed first, then ticket id, then path.
Columns: `ticket, count, landed, repo, path`. An unknown repository returns no rows;
it is never read as "no repository". The `CASE` resolves the name to its key once,
so the lookup searches the `(repo, path)` index instead of scanning `files`.

<!-- index-query: tickets_for_file -->
```sql
SELECT t.ticket, t.count, t.landed, coalesce(r.name, ''), f.path
FROM files f
LEFT JOIN repos r ON r.k = f.repo
JOIN tickets t ON t.file = f.k
WHERE f.repo IS CASE WHEN ?1 = '' THEN NULL
                     ELSE coalesce((SELECT k FROM repos WHERE name = ?1), -1) END
  AND (?2 IS NULL OR f.path = ?2)
ORDER BY t.landed DESC, t.ticket, f.path
```

**`files_for_ticket`** answers the files one ticket touched, landed ones first,
then by repository and path. `?1` is the ticket id. Columns:
`repo, path, count, landed`.

<!-- index-query: files_for_ticket -->
```sql
SELECT coalesce(r.name, ''), f.path, t.count, t.landed
FROM tickets t
JOIN files f ON f.k = t.file
LEFT JOIN repos r ON r.k = f.repo
WHERE t.ticket = ?1
ORDER BY t.landed DESC, coalesce(r.name, ''), f.path
```

## The store manifest

`knowledge/store-manifest.json` says what a store holds, so a reader opens it
first instead of guessing the layout from whichever files exist. It is written
last by `knowledgestore manifest` and committed with the store.

- **One entry per layer**: the store-relative paths a reader should open, whether
  the layer is present, and a content `digest` (the first 16 hex digits of a
  sha256; for a directory, one over its files' names and digests). The graph is
  hashed, never parsed. A layer a stage can measure but a file cannot - source
  links, deployments, ticket reach - carries a flag read from
  `knowledge/telemetry.json`. A flag that is `null` means a library stage has not
  measured it, which is unknown and never "absent": `false` is what says the store
  was measured and lacks it.
- **The relationships layer says whether this library knows it.** Its
  `recognised` flag is true when `knowledge/relationships/repository-dependencies.json`
  carries a top-level `format` stamp naming the layer `relationships` at a major
  this library reads (1), and false for a missing, damaged or foreign stamp; the
  layer is still `present` and digested either way, so the reader decides. It is
  `null` when the layer is absent. The console summary marks a present layer that is
  not recognised `!` with the reason beside it, and `knowledgestore status` names it
  on a `not recognised:` line, because consumers read it as absent until it is
  rebuilt with this library.
- **Present means a clone receives it.** Measured when the manifest is
  computed, from files git would commit: a path a `.gitignore` rule ignores does
  not count, and a directory counts when it holds at least one such file. A layer
  that exists only on the builder's machine - `git-history/`, or a plain
  `graph.json` with no committed `.gz` - is `"present": false` with
  `"local_only": true`, no digest, and counts as absent in the store digest, so
  a builder and every clone compute the same one. A clone compares the manifest's
  `digest`, not the file's bytes or its own `local_only` flags: those describe the
  builder's tree. An empty directory has built nothing.
- **A store's own layers** are declared in `config/store-extra-layers.json`, each
  with a path below the store root, a description and optionally
  `"kind": "answers"` for written answers a reader can serve alongside the
  library's. A path that is absolute or leaves the store is refused, as is a link
  inside a layer that resolves outside it.
- **Freshness is the digest**, never a modification time: a clone and an
  identical rebuild move every mtime, so nothing here reads one. The top-level
  `digest` covers every layer, and `sources_as_of` is the newest commit date
  provenance records. The MCP server compares the `digest` to decide whether the
  store it holds has changed and reports `sources_as_of` as how recent the
  sources are; a store with no manifest yet has neither. `status` also lists the files under `knowledge/` that no layer, declared
  extra layer or library-excused path covers, so a file a reader would never be told
  about is visible.
- **The format version** is `format.major` and `format.minor`. The major number
  changes when a reader written for the old version would misread the store, the
  minor when a layer or field is added. A reader written for one major number
  should not read another. A test fails if the
  manifest's shape changes without a format bump.

The store's id is `KSB_STORE_ID`, else the repository name of the clone's
`origin` remote, never the directory name: readers identify a store by it.

## Reproducibility as a discipline

The repeated pattern across the codebase: a failure that happened once
becomes a gate, not a comment. `remap` refuses rather than degrades;
labels invalidate by signature rather than trust; the install-docs check
(`knowledgestore check-install-docs`) fails CI when a documented command
cannot work as written; the page regression fails when the shipped
application diverges from the tested one. Where behaviour cannot be gated —
clustering non-determinism, refresh economics — it is measured and written
down instead, in the operator guide, with the numbers from real refreshes.

This page is itself an instance. Its determinism claim was checked against
the code before publishing and turned out to be false — the gzip writers
embedded a timestamp and filename, so identical rebuilds produced different
bytes. The claim above became true in the same change that shipped this page,
gated by a test that fails on the old writers.

## Where to go next

- [Retrieval architecture](retrieval-architecture.md) — how this differs
  from vector RAG, and where each answer layer lives.
- [Grounding and verification](grounding-and-verification.md) — what
  "evidence-backed" means, precisely.
- [Building an effective knowledge store](building-a-knowledge-store.md) —
  the operator's judgement, and the measured economics behind this page's
  claims.
