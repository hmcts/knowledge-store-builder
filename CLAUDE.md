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
- Coverage percentages are a map of where to look, never the goal. A test
  written to move a number, with no break it can catch, costs maintenance
  forever and protects nothing.

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

## Releases

`pyproject.toml` holds the version, but the publish workflow **stamps the
artefact version at build time** from `artefact-version-action` — a draft on
push to main, a clean version on a published Release. Keep the declared version
in step with releases, and remember `uv.lock` records the project's own
version: bumping without `uv lock` fails the lint workflow's frozen sync.

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
