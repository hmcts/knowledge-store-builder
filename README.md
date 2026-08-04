# knowledge-store-builder

## What this repository does

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
- links from source files to the tickets that changed them; and
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
Querying with Claude Code requires only the plugin; the browser page requires
neither Claude nor the plugin.

## What do you need?

Choose the path that matches your role:

- **Build a knowledge store** — You own a software estate and want to create
  its knowledge store.
  → [Creating a knowledge store](docs/creating-a-store.md)
- **Refresh a knowledge store** — You maintain an existing store and want to
  update its sources, generated layers or library version.
  → [Refreshing and maintaining a knowledge store](docs/refreshing-a-store.md)
- **Use a knowledge store** — Someone has already built one, and you want to
  ask questions about it.
  → [Asking questions](docs/asking-questions.md)

**Using** a store needs the plugin and nothing else — no Python, no `pip`. The
query skill installs the one tool it needs,
[graphify](https://github.com/safishamsi/graphify). Without a Claude licence,
`explorer.html` answers in a browser with no network access at all.

**Building** a store needs the plugin, the Python library for the
`knowledgestore` commands, and graphify for extraction — plus Python 3.10 or
later, Git and the GitHub CLI. The guides list them.

## How it is designed

- **The store is the product.** Outputs are committed static files. Consumers
  clone and read; nothing is built at query time.
- **The browser has no query-time LLM.** Whoever builds a store may have a
  licence; the people querying it may not, and `explorer.html` is committed for
  them. Everything an LLM writes during the build is committed as reviewed
  static text. Claude Code reads the same evidence when a question needs a new
  prose answer.
- **Deterministic where it can be.** Extraction, indexing and page composition
  are pure functions of the sources; two runs on the same inputs produce
  byte-identical output.
- **Per-commit history stays out of the graph.** It is exported alongside as
  NDJSON, because "what changed last sprint" is a dataset query, not a graph
  traversal — and it keeps the committed graph an order of magnitude smaller.
- **Absence of evidence is a finding.** Same-named components with no
  connecting edge are independent implementations, and the tooling says that
  rather than guessing.

## Reference documentation

| Document | For |
|---|---|
| [`CHEATSHEET.md`](CHEATSHEET.md) | the commands, per surface, with nothing else around them |
| [`docs/asking-questions.md`](docs/asking-questions.md) | asking questions with Claude Code, `explorer.html` or `graphify query` |
| [`docs/creating-a-store.md`](docs/creating-a-store.md) | creating, building and publishing a new store |
| [`docs/refreshing-a-store.md`](docs/refreshing-a-store.md) | refreshing an existing store and changing its pinned library version |
| [`docs/configuring-a-store.md`](docs/configuring-a-store.md) | pipeline settings, BDD support and stage outputs |
| [`docs/building-a-knowledge-store.md`](docs/building-a-knowledge-store.md) | the operator's judgement: defining an estate, what extraction yields, refresh economics, the traps |
| [`docs/grounding-and-verification.md`](docs/grounding-and-verification.md) | whether a store's answers are fact-based, and how to verify subagent-authored content |
| [`docs/retrieval-architecture.md`](docs/retrieval-architecture.md) | how this differs from vector RAG, and where each answer layer lives |
| [`docs/how-it-works.md`](docs/how-it-works.md) | the science: each mechanism, its constants, and where its behaviour is proven |
| [`CLAUDE.md`](CLAUDE.md) | working on this repository: the dev install, the checks, and what has bitten us |

## Licence

MIT. See [LICENSE](LICENSE).
