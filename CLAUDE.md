# Working on this library

The README explains what the library does. This file is for whoever changes it:
the traps that are not visible in the code, and cost hours when rediscovered.

## Ground rules

- **No behaviour changes disguised as refactors.** Stage outputs are committed
  artefacts in consumer repositories; a change in what a stage emits is a
  change to their data. Say so explicitly in the PR.
- **Deterministic output is a feature.** Two runs on the same inputs must be
  byte-identical. Anything that iterates a `set` or a `dict` keyed by unordered
  data needs an explicit tiebreak — sort by name, not just by score. Hash
  randomisation across processes has broken this before and it is invisible
  until someone diffs two builds.
- **Gates are blocking on purpose:** ruff, `ruff format`, pyright, the scorer
  unit tests and the page regression. Do not make one non-blocking to land a
  change.

## Testing

Install the development tools and run the checks from the repository root:

```bash
pip install -e '.[dev]'
python3 -m unittest discover -s tests -v

ruff check src tests
ruff format --check src tests
pyright

node tests/explorer/engine-unit.mjs
python3 tests/explorer/fixture.py
node tests/explorer/page-regression.mjs
```

The explorer application is `src/knowledgestore/assets/app.js`. It is checked
with JSDoc and `tsc --checkJs`, inlined verbatim into the generated page, and
the page regression verifies that the tested code is the code that ships.

**Tests defend the product's designed behaviour under change. A test earns
its place by failing when the product breaks — name the break it catches
before writing it. Assert outcomes and artefacts, never that a mock was
called: prefer real components, or stubs at the true IO boundary, because
describing behaviour can easily become the test. When pinning a fixed bug,
prove the test fails against the broken code before trusting it. Coverage
that doesn't uphold product integrity is not coverage.**

In more detail (the house distillation of the superpowers
`test-driven-development` skill):

1. **Every test names the break it catches.** Before writing the body, name
   the production change that should make it fail — and make sure that
   change is a bug, not a decision. A test only intentional redesign can
   fail is a change detector: it fires on refactors and sleeps through
   bugs. Derive expected values by hand (literals, hand-checked fixtures),
   never with the code under test.

2. **Every test exercises the real thing.** Prefer real components, or
   stubs at the true IO boundary, over mocks. Mocks have their place, but
   describing behaviour easily *becomes* the test — an assertion on a mock
   passes when the mock is present and says nothing about the product.
   Never assert call counts or that a stand-in was invoked; assert
   outcomes and artefacts.

How that looks in this codebase:

- **Stub the IO boundary through the injectable seams** — `run=` on the
  git helpers, `runner=` on the gh helpers. Everything downstream of the
  seam is real code, and assertions land on what it produced: the
  provenance file's contents, the merged `dives.json`, the exit code.
- **The explorer harnesses build through the real pipeline.**
  `tests/explorer/fixture.py` writes real inputs and runs the real merge
  and page build; the regression then drives the shipped `app.js` against
  the page those produced. Never fake a generated artefact to test its
  consumer.
- **Bite-check regression tests.** A test pinning a fixed bug must be
  shown to fail against the broken code before it is trusted — check out
  or temporarily revert the fix and watch it fail. A pin that has never
  failed is unverified protection.
- **Mutation check before finishing:** mentally flip a branch, drop a side
  effect, return the default — at least one test should fail for each
  realistic mutation. A mutation nothing catches is unprotected behaviour
  or a tautological test.
- **A non-editable install of this library shadows `src/` for the whole
  suite.** Test modules put `src/` on `sys.path` at import time, but once any
  earlier module has imported `knowledgestore`, `sys.modules` is already bound
  and the insert does nothing — so discovery silently exercises the *installed*
  package. The symptom is a new test that passes when its module runs alone and
  fails under `discover`, with an error like `unexpected keyword argument`
  naming a parameter you just added. Use the editable install above, or run
  `PYTHONPATH=src python3 -m unittest discover -s tests`.
- Coverage percentages are a map of where to look, never the goal. A test
  written to move a number, with no break it can catch, costs maintenance
  forever and protects nothing.

## The skills are the enforcement point, not the docs

**Skills live in `skills/`**, the directory Claude Code scans by default, and
`plugin.json` declares no `skills` field. A store may reasonably choose the
other arrangement, so be precise about why this one fails:

- **`skills` is a valid manifest field.** What is invalid is a path that does
  not start with `./` — every component path must be relative to the plugin
  root and begin with `./`. `".claude/skills/"` fails install with
  `skills: Invalid input`; `"./.claude/skills/"` is accepted. The message names
  the field, which reads as though the field were unsupported. It is not.
- **`skills` adds to the default scan** rather than replacing it — except when
  a marketplace entry's `source` resolves to the marketplace root, as ours does
  (`"source": "./"`), where naming subdirectories replaces the default `skills/`
  scan instead.

So a store that wants its own skill auto-discovered while working inside its
clone should keep it at `.claude/skills/` and point the manifest at
`"./.claude/skills/"` — one copy serving both. This library has no such need:
its skills are for consumers of stores, not developers here, so it takes the
plain `skills/` layout and declares nothing. The trade is that these skills do
not auto-load while working in this repository; `CLAUDE.md` is what a developer
here needs.

`docs/` holds the reasoning; the three skills in `skills/` hold the
operative rules. **An agent reads the skill it was invoked with and may never
open `docs/`** — so a rule that exists only in a document does not bind anyone.

Consequences for anyone changing this library:

- The grounding contract (`docs/grounding-and-verification.md`) is stated
  **inside all three skills**, at the point each one needs it:
  `knowledge-store` (traceability and layer precedence in its honesty rules),
  `knowledge-store-build` (verify grounding not only coverage, at the subagent
  dispatch step), `knowledge-store-export` (re-derive anything a subagent found
  before publishing it). Adding a fourth skill that produces or reports store
  content means carrying the rule into it too. Do not rely on the pointer.
- **`docs/grounding-and-verification.md` is the master. The statements inside the
  skills are copies, and they must be updated whenever the master changes.** The
  obligation runs one way: editing the master without updating the skills leaves
  agents enforcing a superseded rule, and a skill that has drifted from the
  contract is worse than one that never mentioned it, because it reads as
  authoritative. The master lists where it is mirrored — read that list before
  you finish editing it.
- The same applies to any other document whose rules are restated in a skill.
  If you find yourself copying a rule into a skill, add the skill to the master
  document's mirror list in the same change.
- Keep the pointer as well as the rule: the skill carries the short operative
  form, the document carries the reasoning and the techniques. Neither replaces
  the other.

## Writing documentation

User-facing docs are **persona-led**: one document, one reader. The README
serves the evaluator and routes the other two personas to their guide.
`docs/asking-questions.md` serves the asker (plugin only).
`docs/creating-a-store.md` serves the builder (library + tools) and routes
maintenance and reference tasks to shorter builder subguides. Do not grow the
README back into a manual; usage detail belongs in the guide that owns the
persona.

Before writing or reworking any of them, use the `technical-writer` skill in
`.claude/skills/` — it carries the house register (value-first, commands before
prose, the banned-word list) and the verification steps, and it names its
source. Two hard rules from it:

- **Inbound deep links are load-bearing.** `hmcts/cp-knowledge-store` links
  into this repository's docs, so renaming a heading it targets breaks a
  consumer's README silently. Grep the consuming repository for the anchor
  before renaming or removing any heading — that check is what caught the
  README's install sections being removed while CP-KS still pointed at them.
- **Install detail lives in the guides, not the README.** `docs/asking-questions.md`
  owns the plugin install and `docs/creating-a-store.md` owns the library
  install. The README routes to them and carries no install commands of its own,
  so there is one copy of each to keep correct.
- **Docs and skills must not disagree.** The README once kept `graphify .` at
  the store root long after the build skill documented why that cannot work.
  When a skill changes an instruction, grep the docs for the old one.

## Library examples stay generic

This repository is **public** and reusable. Skills, docs and examples must not
carry any consuming estate's specifics: no repository names, no field names, no
counts, and above all no detail of a live finding in a store built with this
library. Illustrate with `N of M (P%)`, `xxxxx@xxxxx.example`, or an invented
field name.

This has already gone wrong once: examples in the export skill were drawn from
an unremediated personal-data finding in a consuming estate — the real domain
and the real counts — and pushed to a public branch. Estate figures and findings
belong in the store that owns them. The single deliberate exception is the
operator guide naming the estate it was written from, as attribution for a case
study.

## Graph handling

- **Never merge a graph that is already merged.** `graphify merge-graphs`
  namespaces node ids as `<repo>::<id>`; feeding it a merged graph namespaces
  them again as `repo::<repo>::<id>`, sets every `repo` attribute to the string
  `"repo"`, and silently breaks Gherkin de-duplication. Always merge flat, from
  every per-repo graph plus the knowledge corpus, in one call.
- **Extract per repository from inside that repository.** Running the
  extraction with a path like `repositories/<name>` prefixes every
  `source_file` with `repositories/<name>/`, which silently breaks the
  file-to-ticket join — the intent index is keyed on repo-relative paths. The
  only symptom is that nodes lose their tickets.
- **Community ids are not stable across re-clustering.** Community summaries
  are keyed by id, so re-clustering strands them. Remap by membership overlap
  (60% is a reasonable bar) or regenerate; never assume the old file still
  applies.
- **Structural nodes carry no label.** Newer graphify emits Java
  package-hierarchy nodes with neither `label` nor `source_file`. Anything
  iterating nodes must tolerate that — the explorer index and the summary
  digests skip them.
- **Node kinds are format-agnostic** (`feature`, `scenario`, `ticket`) with a
  separate `format` field. `kinds.py` still reads the old format-specific kinds
  so stores built earlier keep working. Write the current kind, read either.
- **`deepdive extract` loads the full graph** (can be ~1.6 GB decompressed);
  `status` deliberately never does. Keep it that way.
- **`status` never returns non-zero.** Drift and coverage gaps are normal
  operating conditions; the stage reports, humans decide.

## Repository sync

Per-repo graphs live untracked *inside* each clone, at
`repositories/<name>/graphify-out/`. `git clean -fd` therefore deletes them,
which once destroyed 61 of 81 graphs on a re-sync. The exclusion in
`sync_repositories.py` is load-bearing and has a regression test.

Full clones are deliberate: the history export diffs every commit, so a
`--filter=blob:none` clone re-fetches blobs one commit at a time and is far
slower overall.

## The shared-environment trap

This library and its consumer stores often share one Python environment.
Installing a store's pinned release (its `requirements.lock`) silently
replaces this repo's editable install — after which "local" test runs
exercise the released wheel, not your working tree: tests for new code
error while CI passes. Before trusting a local run, confirm
`python3 -c "import knowledgestore; print(knowledgestore.__file__)"` points
at `src/` here; `pip install --no-deps -e .` restores it.

## Things that look like bugs but are not

- `str.strip()` treats `\x1f` and `\x1e` as whitespace. The history export uses
  them as field separators, so stripping whitespace silently drops
  empty-body commits. Strip `"\n"` explicitly. There is a regression test.
- `graphify` is a peer CLI, not a dependency. The library prepares its inputs
  and enriches its output; it does not re-implement extraction.
- Very large graphs need `GRAPHIFY_VIZ_NODE_LIMIT` raised, or the HTML
  visualisation export refuses to run.

## Required checks and paths-ignore do not mix

The `main` ruleset requires the `tests` and `CodeQL` status checks. A workflow
skipped by `paths-ignore` **never reports its check at all** — GitHub waits for
a context that will never arrive — so a documentation-only pull request sits at
`BLOCKED` with every visible check green. That deadlocked a docs PR, and the
diagnosis is invisible from the checks list: you have to compare the required
contexts in the ruleset against the ones that reported.

So: never add `paths-ignore` to a workflow whose check is required. `build.yml`
and `lint.yml` may keep theirs because they are not required. If skipping a
required check on prose ever becomes worth the effort, add a companion job that
reports the same context for the ignored paths.

## Releases

**The version is the git tag** (hatch-vcs): creating a GitHub release is the
whole bump — no file mentions a version, so there is nothing to keep in step.
Pushes to main publish drafts as `<next>.devN+g<sha>` automatically, and
`knowledgestore.__version__` reads the installed metadata. Two consequences:
installing the package needs the git history and tags present (CI checkouts
use `fetch-depth: 0`), and `uv.lock` records the project as `(dynamic)`, so
releases can no longer desync the lockfile.

## SonarCloud, learned the hard way

- Automatic analysis **ignores `sonar-project.properties` entirely** —
  exclusions and issue-ignore rules alike. Scope exclusions are a UI action.
- `NOSONAR` comments work for the Python analyser and are ignored by the
  JavaScript one. Where a JS rule cannot be suppressed, remove the construct.
- The GitHub Actions rules read the **command**, not the files it references.
  Flags already set in `requirements.txt` or implied by `--no-sync` must be
  repeated on the command line to satisfy them.
- YAML trap: `run: pip install --only-binary :all: -r x.txt` fails to parse,
  because `: ` ends a plain scalar. Use a block scalar (`run: |`).
