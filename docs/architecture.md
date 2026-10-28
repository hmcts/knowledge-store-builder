# Deployable artefacts

This page is for store maintainers and contributors who need to know what is
released, by whom, and what runs on an asker's machine. Askers do not need it.

Six things get released. Two kinds of repository own them: this library's
repository, and each store's own. The asker's machine only ever holds three of
them, and the server reads the store from local disk with no network access.

![The library, generic plugin, fixture store and MCP server all come from knowledge-store-builder, the server installed with its [server] extra; store data and the store plugin come from each store repository. On the asker's runtime the store plugin launches the MCP server, which reads a local clone of the store and talks to the agent over stdio, with no network or credentials.](architecture.svg)

Teal marks what the MCP server adds. The dashed teal line is the trust boundary:
the server sees only the clone and the agent. Every front door, Claude Code
included, answers only through the server. The `knowledge-store` skill keeps
cloning, freshness, repair and recording the clone's location, but it no longer
answers by reading files.

## Who releases what

| Artefact | Released by | Versioned by | Consumed by |
|---|---|---|---|
| ① Library | The maintainer cuts a GitHub release | Git tag (hatch-vcs) | Store builds, pinned in the store's lock file |
| ② Generic plugin | Merging to `main` | Nothing: installs from `main` | Builders, through the marketplace |
| ③ Fixture store | Each ① release | The ① tag | ⑥'s contract tests |
| ④ Store data | The store build pushes to its default branch | Commit, with the format declared in `store-manifest.json` | ⑥, through the asker's clone |
| ⑤ Store plugin | The store repository's marketplace entry | Its default branch | Askers |
| ⑥ MCP server | The same release as ①: it is `knowledgestore serve`, installed with the `[server]` extra | The ① tag, `v0.22.0` or later | ⑤'s `mcpServers` entry, run with `uvx` at that tag |

## Decided

- **Delivery of ⑥.** Running `uvx --from "hmcts-knowledge-store-builder[server] @ git+https://github.com/hmcts/knowledge-store-builder@v0.22.0" knowledgestore serve --store-id <id>` keeps the asker free of a Python install step beyond `uv` itself. It needs `uv` on the host, and Python 3.13, which `uv` downloads unless managed Python downloads are disabled. [Use it now](mcp-server.md#use-it-now) gives the form to copy.
- **One tag for the library and the server.** The server's tests run against the library's own fixture and frozen stores in the same repository, so a library change that breaks what the server reads fails the library's own suite. A store pins the server by pinning that tag in its plugin, and `knowledgestore check-plugin` holds the pin to an exact tag.
- **The `[server]` extra, not a separate package.** The MCP SDK is a dependency only `serve` needs, so builders and CI that never run the server do not install it.
