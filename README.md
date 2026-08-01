# knowledge-store-builder

## What does this repository do?

Build a knowledge store from one or more GitHub repositories and use it to ask
questions about your software estate with cited answers.

For example:

> Which applications implement their own address formatting, and which tickets
> changed them?

Two independent products in one repository work together to provide this
capability.

**The Python library** builds the knowledge store. Point it at a GitHub
organisation and select the repositories to analyse. It reads source code,
commit history and Gherkin specifications, then generates static artefacts
including:

- a graph of the estate;
- business features as first-class nodes;
- links from every file to the tickets that changed it; and
- a self-contained HTML application that searches and answers without a server
  or an LLM.

**The Claude Code plugin** provides the Claude Code experience. It includes
three skills:

- ask a knowledge store questions and receive cited answers;
- build and refresh a knowledge store; and
- export findings with sensitive values removed.

The two products are designed to work together. The Python library provides
the `knowledgestore` commands that build the knowledge store. Querying reads
the committed artefacts directly through the `graphify` CLI, which the
plugin's query skill installs automatically. Building requires both products.
Querying an existing knowledge store requires only the plugin.

## What do you need?

Choose the path that matches your role:

- **Build a knowledge store** — You own or maintain a software estate and want
  to create or refresh its knowledge store.
  → [Creating and refreshing](docs/creating-a-store.md)
- **Use a knowledge store** — Someone has already built one, and you want to
  ask questions about it.
  → [Asking questions](docs/asking-questions.md)

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
