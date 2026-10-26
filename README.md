# knowledge-store-builder

Ask questions about a software estate and get answers that cite the code,
the commits and the tickets behind them.

<img src="docs/images/explorer-answering-a-question.png" alt="The explorer page answering 'how are addresses validated?'. A headline verdict reads: each application formats addresses with its own copy of AddressPipe; there is no shared implementation. Below it, sections headed How it works, Where it lives, and What this is NOT, then the business features in the area and the commits that changed them, each citing a repository, a file path or a ticket id." width="580">

That is `explorer.html`: one file, opened from disk, answering with no server,
no network and no LLM. `python3 tests/explorer/fixture.py` builds it.

## Ask

| Where | How |
|---|---|
| Claude Code | install the plugin: [Asking questions](docs/asking-questions.md) |
| GitHub Copilot or another MCP client | the same server and tools: [Use another assistant](docs/asking-questions.md#use-another-assistant-github-copilot-a-hosted-agent) |
| A browser | open the store's `explorer.html` |
| A terminal | `graphify query` against the committed graph |

Every answer names the evidence it came from, and says so when the store cannot
support one. Two components that share a name and have no edge between them are
reported as separate implementations, not guessed to be one.

## Build

The `knowledgestore` command builds a store from a GitHub organisation's
repositories, one stage at a time, using
[graphify](https://github.com/safishamsi/graphify) for extraction. The output is
static files committed with the store: the graph, the explorer page, commit
history, the tickets behind each file, and reviewed prose. Nothing is computed
when someone asks.

Start at [Creating a knowledge store](docs/creating-a-store.md).
`knowledgestore` with no arguments lists the stages.

## Design

- **The store is the product.** Committed static files; consumers clone and read.
- **No LLM at query time in the browser.** Prose an LLM writes during the build is
  reviewed and committed.
- **Deterministic.** Two runs on the same inputs produce byte-identical output.
- **Absence of evidence is a finding**, reported rather than filled in.

## Documentation

| Document | For |
|---|---|
| [Asking questions](docs/asking-questions.md) | asking a store someone built |
| [Creating a knowledge store](docs/creating-a-store.md) | building one for your estate |
| [Refreshing a store](docs/refreshing-a-store.md) | keeping one current |
| [`CHEATSHEET.md`](CHEATSHEET.md) | every command, nothing around it |
| [Configuring a store](docs/configuring-a-store.md) | settings and stage outputs |
| [Building a knowledge store](docs/building-a-knowledge-store.md) | the operator's judgement and the traps |
| [Grounding and verification](docs/grounding-and-verification.md) | whether answers are fact-based |
| [Retrieval architecture](docs/retrieval-architecture.md) | how this differs from vector RAG |
| [How it works](docs/how-it-works.md) | each mechanism and where it is proven |
| [`CLAUDE.md`](CLAUDE.md) | working on this repository |

## Licence

MIT. See [LICENSE](LICENSE).
