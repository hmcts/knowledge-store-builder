# knowledge-store-builder

Build a committed, queryable knowledge store from a fleet of git repositories.

Point it at a GitHub organisation. It reads the source, the commit history and
the Gherkin specifications of the repositories you select, and produces static
files: a graph of the estate, an index from every file to the tickets that
changed it, business features as first-class nodes, and one HTML page that
searches and answers questions with no server and no LLM.

You commit the output. Anyone who clones it can then query the estate — best
through a coding agent, which traverses the graph and writes a cited answer,
and adequately through the HTML page with no licence at all.

Built for the HMCTS Common Platform crime estate, where it answers questions
like "which applications implement their own address formatting, and which
tickets changed them" without anyone reading the code.

## Who you are decides what you install

| You want to | You need | Start here |
|---|---|---|
| **Ask questions** of an estate that already has a store | the Claude Code plugin — no Python, no `pip` | [Asking questions](docs/asking-questions.md) |
| **Create or refresh** a store for your own estate | the Python library, plus the plugin | [Creating a store](docs/creating-a-store.md) |
| **Decide** whether this is worth adopting | the rest of this page | — |

The two installs share nothing but this repository. The **plugin** is
instructions for Claude Code — skills that drive tools on your machine. The
**library** is the pipeline those instructions drive when building. Askers
never need the library: the only tool the query skill uses is the `graphify`
CLI, and it installs that itself.

## Install: the Claude Code plugin

```
/plugin marketplace add hmcts/knowledge-store-builder
/plugin install knowledge-store@knowledge-store-builder
/reload-plugins
```

**`/reload-plugins` is not optional** — without it the plugin is installed but
inactive in the session you are sitting in. Verification, updates and
troubleshooting: [Asking questions](docs/asking-questions.md).

## Install: the Python library

```bash
pip install --extra-index-url \
  https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/ \
  hmcts-knowledge-store-builder
```

The feed reads anonymously — no credentials, no HMCTS account. Pinning,
installing from source, the optional semantic extra, and the pipeline itself:
[Creating a store](docs/creating-a-store.md).

## How it is designed

- **The store is the product.** Outputs are committed static files. Consumers
  clone and read; nothing is built at query time.
- **The LLM runs at build time, never at query time.** Whoever builds a store
  may have a licence; the people querying it may not. Everything an LLM writes
  is committed as reviewed static text.
- **Deterministic where it can be.** Extraction, indexing and page composition
  are pure functions of the sources; two runs on the same inputs produce
  byte-identical output.
- **Per-commit history stays out of the graph.** It is exported alongside as
  NDJSON, because "what changed last sprint" is a dataset query, not a graph
  traversal — and it keeps the committed graph an order of magnitude smaller.
- **Absence of evidence is a finding.** Same-named components with no
  connecting edge are independent implementations, and the tooling says that
  rather than guessing.

## Going deeper

| Document | For |
|---|---|
| [`docs/asking-questions.md`](docs/asking-questions.md) | getting answers from an existing store |
| [`docs/creating-a-store.md`](docs/creating-a-store.md) | building and refreshing a store: setup, the pipeline, configuration |
| [`docs/building-a-knowledge-store.md`](docs/building-a-knowledge-store.md) | the operator's judgement: defining an estate, what extraction yields, refresh economics, the traps |
| [`docs/grounding-and-verification.md`](docs/grounding-and-verification.md) | whether a store's answers are fact-based, and how to verify subagent-authored content |
| [`docs/retrieval-architecture.md`](docs/retrieval-architecture.md) | how this differs from vector RAG, and where each answer layer lives |

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
