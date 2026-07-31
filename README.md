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

Built for the HMCTS Common Platform crime estate, where it answers questions
like "which applications implement their own address formatting, and which
tickets changed them" without anyone reading the code.

## Two installs, and which one you need

There are **two separate installs** here. They share nothing but the source
repository — different channels, different audiences, different versioning — and
conflating them is the most common confusion:

| | **1. The Python library** | **2. The Claude Code plugin** |
|---|---|---|
| What you get | the `knowledgestore` CLI — the pipeline that builds a store | three skills for querying, building and exporting |
| Comes from | a wheel on the `hmcts-lib` Azure Artifacts feed | a clone of this repository on GitHub |
| Install with | `pip`, pinned in your store's requirements | `/plugin marketplace add` |
| You need it to | **rebuild** a store | **use** one |
| Version | a git tag, resolved at build time and pinned by consumers | none — whatever is on `main` |
| How to install | [Install: the Python library](#install-the-python-library) | [Install: the Claude Code plugin](#install-the-claude-code-plugin) |

**Asking questions of an existing store needs only the plugin** — no `pip`, no
feed, no Python at all. Only someone refreshing a store needs the library, and
they usually want both.

The versioning asymmetry is worth stating plainly, because it surprises people:
**cutting a release does not ship a skill change, and merging a skill change
needs no release.** A store can therefore pin library `v0.5.1` while running
skills from a later `main`. Neither is wrong — they are separate channels — but
if a skill's behaviour does not match its documentation, the release number is
not the thing to check.

## Install: the Python library

Releases are published to the **`hmcts-lib`** Azure Artifacts feed, not to PyPI,
so the feed has to be named explicitly. It reads anonymously, so this needs no
credentials and no HMCTS account:

```bash
pip install --extra-index-url \
  https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/ \
  hmcts-knowledge-store-builder
```

Opening the feed's root URL in a browser does prompt for a sign-in, which makes
it look private. It is not: `pip` requests the per-package path, which serves
the distribution list and the artefacts without authentication. Credentials are
only needed to *publish*, and that is the release workflow's job.

For a store repository, put the feed and a pinned version in a requirements
file so every rebuild resolves the same way, and lock it with
`uv pip compile --generate-hashes` so CI installs exactly what you resolved:

```
--extra-index-url https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/
--only-binary :all:
hmcts-knowledge-store-builder==X.Y.Z   # the release you mean: see the Releases page
```

To run an unreleased change, install from the branch instead of the feed:

```bash
pip install git+https://github.com/hmcts/knowledge-store-builder.git@main
```

**The embedding stage** needs a model, and is the one optional extra:

```bash
pip install 'hmcts-knowledge-store-builder[semantic]'
```

Everything else is standard library only, by design: the pipeline runs anywhere
Python does, with no supply chain to review.

## Two tools it does not replace

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

`discover` warns about any selecting rule that matched no repository, which is
what catches a typo or a rule whose value swallowed a comment. `--strict` turns
those warnings into a non-zero exit for CI.

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
| `gherkin` | (updates the graph) | Features, scenarios and ticket nodes in business language, linked to the step definitions implementing them (Java, Python and TypeScript) |
| `summaries` | `knowledge/summaries/` | Plain-English descriptions of each cluster |
| `semantic` | `knowledge/semantic/` | Token-neighbour map, so "outcomes" finds "results" |
| `topics` | `docs/topics/`, `briefs.json` | Pre-written answers to anticipated questions |
| `deepdive` | `knowledge/deep-dives/`, `docs/deep-dives/` | Evidence-grounded dossier on one repository: churn, instability, co-change coupling, hotspots |
| `status` | (report only) | Provenance, layer coverage, dangling citations, page freshness; `--drift` checks GitHub for commits since the build |
| `explorer` | `graphify-out/explorer.html` | One self-contained page: search and basic Q&A |

## The two-part stages

`summaries`, `topics` and `deepdive` are the stages where an LLM writes prose,
and they are deliberately split so it runs at **build** time, never at query
time:

```bash
knowledgestore summaries extract              # evidence digests, deterministic
# write the prose (in your coding agent, from the digests), then:
knowledgestore summaries merge written.json   # validated on the way in
```

`merge` rejects unknown cluster ids and out-of-range lengths, so a mismatched
batch cannot silently enter the store — but shape is all it can check. Two more
sub-commands cover what shape cannot:

```bash
knowledgestore summaries verify --sample 200   # is the prose grounded in its digest?
knowledgestore summaries snapshot              # before re-clustering
knowledgestore summaries remap                 # after: carry summaries onto new ids
```

`verify` compares the identifiers each summary cites against those its digest
contains, because a confidently fabricated batch passes every length and id
check. `snapshot` and `remap` survive a re-cluster: adding repositories moves
community ids, and `remap` carries prose across by membership overlap, drops what
it cannot place, and reports retention. See `docs/grounding-and-verification.md`.

`topics`
works the same way: `extract` gathers a per-topic evidence dossier, you write
`docs/topics/<slug>.md` from it, and `merge` validates and renders it.

This split is the point of the design. Whoever builds the store may have an
LLM; the people querying it may not. Everything the LLM produces is committed
as reviewed static text.

`deepdive` is the third two-part stage, scoped to a single repository:

```bash
knowledgestore deepdive extract cpp-context-progression   # loads the full graph
# write docs/deep-dives/cpp-context-progression.md from the bundle, then:
knowledgestore deepdive merge
knowledgestore explorer
```

A dossier must state which build it measured (the bundle's short commit SHA);
`merge` rejects one that does not, because churn and instability figures go
stale with every commit and a dossier that does not say when it was true is
misleading rather than useful.

## Configuration

Every setting has a working default and an environment variable. The ones that
matter when adopting the library:

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

From Python, `config.configure()` does the same thing and is what the tests
use:

```python
from knowledgestore import config, build_explorer

config.configure(root="/path/to/store", EXPLORER_TITLE="Payments Estate")
build_explorer.main()
```

## Install: the Claude Code plugin

Three skills ship here, so any store gets them. This is the second of the
[two installs](#two-installs-and-which-one-you-need) and is independent of the
first: it clones this repository from GitHub, and needs no `pip`, no feed
credentials and no Python.

```
/plugin marketplace add hmcts/knowledge-store-builder
/plugin install knowledge-store@knowledge-store-builder
/reload-plugins
```

`install` opens the plugin's details so you can pick a scope — **user** for
yourself everywhere, **project** to commit it for everyone on the repository,
**local** for yourself here only. **`/reload-plugins` is not optional**: without
it the plugin is installed but not active in the session you are sitting in.

Then confirm all three arrived — `/plugin` lists the installed plugin, and
`knowledge-store`, `knowledge-store-build` and `knowledge-store-export` should
appear in the skills available to the session. Note that `/reload-plugins`
counts only a plugin's `commands/` directory, so it can report `0 skills` while
having loaded all three correctly — trust the skill list, not that number.

If the plugin installs but its skills never appear, the documented fix is to
clear the cache, restart, and reinstall:

```bash
rm -rf ~/.claude/plugins/cache
```

**Installs come from `main`, not from your checkout.** The marketplace clones
this repository, so a skill change is only installable once it is merged;
having the branch locally does nothing. Two consequences:

- **Already added the marketplace?** `add` does not re-fetch. Take a newer
  release of the skills with:

  ```
  /plugin marketplace update knowledge-store-builder
  ```

- **Do not install from a local path** to test an unmerged change. It resolves
  to a `temp_local_*` copy that is not the plugin consumers get, and any
  manifest problem it reports is about that copy. Merge, update, install.

`skills: Invalid input` on install means the manifest on `main` is wrong, not
your setup. Despite the wording it rarely means the field is unsupported —
usually a component path is missing its `./` prefix, which every path in
`plugin.json` requires. `CLAUDE.md` has the layout rules, including how a store
keeps one copy of its skill for both in-clone discovery and plugin install.

| Skill | Use it to | Covers |
|---|---|---|
| **`knowledge-store`** | **ask a store questions** | Finds a store (working directory, `$KNOWLEDGE_STORE`, a remembered location, or a look under your home directory) and offers to clone one if there is none. Traversal recipes, the business-intent and journey recipes, the interpretation rules that matter — same-named nodes in different repositories are independent implementations unless an edge says otherwise — and what the graph cannot answer. Every claim traces to evidence; absence of evidence is reported as a finding. |
| **`knowledge-store-build`** | **build or refresh one** | The stage order, and the three loops that need an LLM: community summaries from digests, topic briefs from evidence dossiers, and repository deep dives. Fanning those across parallel subagents, then verifying what comes back — coverage *and* grounding, because a fabricated batch passes every shape check. |
| **`knowledge-store-export`** | **hand a finding to someone else** | A dated, self-contained markdown export for a ticket or a data-protection owner: required sections, provenance, honest limits, and a plain register so it reads as measurement rather than persuasion. Sensitive values stay out — locations, masked shapes and a regeneration recipe instead. |

Pick by direction of travel: `knowledge-store` reads, `knowledge-store-build`
writes, `knowledge-store-export` sends outward.

Individual stores can add their own thin skill for estate specifics — which
repositories matter, their clone URL, where journeys are written up, what is
known stale — and leave the mechanics to these three.

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

## BDD specifications

The `gherkin` stage reads `.feature` files — the Cucumber format, not anything
estate-specific — and links each feature to the code implementing its steps.
Step definitions are matched across languages, so a mixed estate works:

| Language | Where it looks | What it recognises |
|---|---|---|
| Java | `src/test/java/**/*.java` | `@Given("...")`, named by the enclosing class |
| Python | `**/*.py` | `@given("...")` (behave, pytest-bdd), named by module |
| TypeScript | `**/*.ts` | `Given("...")` (cucumber-js), named by module |

Steps are normalised before matching — Cucumber expressions (`{int}`),
behave's typed parameters (`{amount:d}`), regex groups and quoted values all
collapse to a placeholder — so the same business step matches whichever
language declared it.

Add a language, or narrow a glob for an unusual layout, by assigning to
`config.STEP_DEFINITION_LANGUAGES`.

Graph nodes carry a format-agnostic `kind` (`feature`, `scenario`, `ticket`)
plus `format` recording the parser that produced them, so a second
specification format can be added without changing consumers.

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

Two documents go further than this README:

- **`docs/building-a-knowledge-store.md`** — the operator's judgement:
  how to define an estate (and how to tell when your definition is wrong),
  what extraction actually yields per content type, what to gate before you
  build, what a refresh costs, and the traps that have cost real time. Read
  this before standing up a new store.
- **`docs/grounding-and-verification.md`** — the contract that every claim in a
  store traces to evidence in it, why a subagent's report is not evidence that
  its work is correct, and how to check grounding rather than only coverage.
  Read this if you are asked whether a store's answers are fact-based.
- **`docs/retrieval-architecture.md`** — the retrieval design in full: how
  this differs from vector RAG, what the graph is and is not good for, and
  where each answer layer lives.

## Development

```bash
pip install -e '.[dev]'                      # tooling pinned in the dev extra
python3 -m unittest discover -s tests -v     # the whole suite, in under a second

ruff check src tests                         # lint
ruff format src tests                        # format (checked in CI)
pyright                                      # type-check (clean; keep it so)

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
| `lint` | ruff check, `ruff format --check` and pyright — all blocking |
| `tests` | unit tests, eslint, `tsc --checkJs`, the scorer tests and the page regression |
| `build` | builds the wheel and sdist, checks metadata, publishes to the `hmcts-lib` feed on main and on releases |
| `codeql` | security analysis of the Python pipeline and the JavaScript explorer application (auto-enables when the repository is public) |
| `secrets-scanner` | gitleaks over the full history, weekly and on every pull request |
| `dependabot-auto-merge` | merges grouped minor and patch bumps once checks are green; majors go to review |

Locally, `pre-commit install` gives the same lint and secret checks before a
commit is made (needs `gitleaks` on PATH).

## Licence

MIT. See [LICENSE](LICENSE).
