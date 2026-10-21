# Asking questions of a knowledge store

A knowledge store can answer questions about an estate's structure, ownership,
history and intent: which repositories implement something, where a component
is used, what a change could affect, how a user journey works, and which commits
or tickets explain why code exists. It cannot establish runtime behaviour or
answer from sources that were not included in the store.

## Choose how to ask

| Route | What you need | What it provides |
|---|---|---|
| Claude Code | The Knowledge Store plugin | Evidence-backed answers in ordinary language, including questions that need several store layers |
| `graphify-out/explorer.html` | A browser | Search and basic answers from a self-contained page |
| `graphify query` | A terminal and the `graphify` CLI | Graph results for a question written in ordinary language |
| GitHub Copilot or another MCP client | The knowledge-store MCP server | The same cited answers, in the assistant you already use; see [Use another assistant](#use-another-assistant-github-copilot-a-hosted-agent) |

For a command-only reference, see [CHEATSHEET.md](../CHEATSHEET.md).

## Ask with Claude Code

### Install the plugin

This route needs no Python or `pip`. The query skill installs the `graphify` CLI
on first use if it is not already available.

Run these commands in Claude Code in a terminal:

```
/plugin marketplace add hmcts/knowledge-store-builder
/plugin install knowledge-store@knowledge-store-builder
/reload-plugins
```

The install command asks for a scope:

- **user**: available to you in every project
- **project**: available to everyone working in the current repository
- **local**: available only to you in the current repository

`/reload-plugins` is required. Without it, the plugin is installed but inactive
in the current session.

In the **VS Code or JetBrains extension**, install through the interface instead:
type `/plugins` to open **Manage plugins**, add `hmcts/knowledge-store-builder`
on the **Marketplaces** tab, then install `knowledge-store` from the **Plugins**
tab. The extension exposes only a subset of Claude Code's commands, so the three
commands above are not all available there. What you configure either way is
shared: plugins added in the extension are available in the CLI, and the reverse.

Check the installation:

```bash
claude plugin details knowledge-store
```

The output should list four skills: `knowledge-store`,
`knowledge-store-build`, `knowledge-store-export` and `knowledge-store-assess`.
This command needs the
standalone CLI on your `PATH`; installing an IDE extension does not put it there,
so use the extension's own **Manage plugins** view instead.

The cheatsheet also covers installation in the Claude desktop app, cloud
sessions and environments where plugins are unavailable.

### Ask a question

Ask in ordinary language. There is no query syntax. For example:

- Which repositories implement their own address formatting?
- What is impacted if this component changes?
- Walk me through what happens when a user submits this form.
- Why does this module exist, and which tickets shaped it?

`/knowledge-store:knowledge-store` looks for a store in this order:

1. the current working directory
2. the path in `$KNOWLEDGE_STORE`
3. locations remembered from earlier queries
4. likely locations under your home directory

If it finds more than one possible store, it asks which estate you mean. If it
finds none, it asks whether you already have a clone or which store to clone and
where to put it.

### Where the query skills remember your stores

Every store's query skill shares one file, `~/.config/knowledge-store/locations`.
A server started with `--store-id` resolves the id through it, so the format is a
contract:

- **Path:** `~/.config/knowledge-store/locations`, shared by every store's query
  skill. A store-specific file is not read.
- **Format:** UTF-8 text, one absolute path per line, each the root of a store
  clone. There are no comments and no other fields.
- **Writing:** append only, and only when that exact line is absent. The
  `knowledge-store` skill does this on first clone:
  `grep -qxF "$(pwd)" ~/.config/knowledge-store/locations || printf '%s\n' "$(pwd)" >> ~/.config/knowledge-store/locations`.
  A user may query several stores, so the file is a list and is never
  overwritten.
- **Reading:** a reader skips blank lines and lines whose path no longer exists.
  It identifies a store by the `store.id` in that path's
  `knowledge/store-manifest.json`, never by the directory name. When two entries
  carry the same id, the reader refuses and names both, rather than picking one.

A store's own skill that records its clone somewhere else should also append to
this file, so a server started with `--store-id` can find the clone.

## Understand the answer

Under the [grounding contract](grounding-and-verification.md), every claim traces
to evidence in the store: a node, edge, source path, schema field, ticket or
commit message. The answer identifies the repository and the evidence it used.
**Absence of evidence is reported as a finding** — where the store cannot show
something, the answer says so rather than filling the gap from general knowledge.

Treat same-named components in different repositories as independent
implementations unless an edge connects them. A shared name alone does not show
that one calls, owns or depends on another.

A store is a snapshot. It records the source commits used to build it, but a
later clone or pull can still contain a store built before recent source
changes. Answers about recent work should report possible staleness; authored
briefs and summaries can also be older than the graph evidence.

To learn how to read an answer, whichever route gave it, see
[How to read an answer](grounding-and-verification.md#how-to-read-an-answer):
citations, negatives, proposed tickets, near misses and how current it is.

## Ask without Claude Code

### Use `explorer.html`

Clone the complete store and open `graphify-out/explorer.html` in a browser. The
page is self-contained: it needs neither Claude nor network access.

The explorer searches the estate and answers supported question shapes from
pre-computed evidence, including repository, usage, impact and ticket lookups.
It searches what the commits said as well as the code: a question whose words
appear only in a commit subject or a commit body still finds the tickets that
carry them, and every quotation is shown under the name of the field it came
from. It can also return topic briefs written by the store's maintainers. It
cannot compose a new prose answer for an unanticipated question, inspect runtime
behaviour or make a snapshot current.

### Use the terminal

From the root of a store clone:

```bash
pip install graphifyy                    # or: uv tool install graphifyy
gunzip -kf graphify-out/graph.json.gz    # stores commit the graph compressed
graphify query "<question>"
```

`graphify query` reads the uncompressed graph and accepts a question in ordinary
language.

**Decompress whenever the `.gz` is newer, not only when `graph.json` is absent.**
`graph.json` is gitignored, so nothing updates it: decompressing only when it is
missing leaves every later question answered from the first copy, however old it
has become. `git pull` makes that worse rather than better, because it moves the
`.gz` and leaves the stale plain file in place. Use `-f`, or `gunzip` refuses to
overwrite:

```bash
[ -f graphify-out/graph.json.gz ] \
  && [ ! graphify-out/graph.json -nt graphify-out/graph.json.gz ] \
  && gunzip -kf graphify-out/graph.json.gz
```

### Search the corpus when the graph comes back empty

Search the content set, not the tree:

```bash
tr '\n' '\0' < knowledge/corpus/content-files.txt \
  | xargs -0 grep -nIs -- '<term>'
```

**`grep -r repositories/` is the wrong command**, and it fails in the direction
that costs most: on the fallback, after the graph has already not answered you.
The pipeline writes into the tree it reads, so `repositories/` holds each clone's
extraction cache and graph, its VCS pack files and any vendored dependency bundles
alongside the corpus. Measured on two estates, a naive recursive search sees several
times as many files as the store considers content, and most of what it returns
matched inside something nobody in the estate wrote.

`knowledge/corpus/content-files.txt` is the set the pipeline itself computed, one
store-relative path per line. It arrives with a sparse clone; the corpus it names
does not, so this route needs a full `git clone`. If the file is missing, the store
predates it — a builder adds it with `knowledgestore content-set`, and
`knowledgestore status` reports whether it is present and current.

## Use another assistant (GitHub Copilot, a hosted agent)

Ask from GitHub Copilot, or any other MCP client, through the knowledge-store
MCP server. It answers from a local clone of the store, with the same read-only
tools in every client. Setup for each client is in the server's
[Front doors](https://github.com/hmcts/knowledge-store-mcp#front-doors) section,
and the server is available from the first knowledge-store-mcp release.

| You use | Take |
|---|---|
| Claude Code | The plugin: [Ask with Claude Code](#ask-with-claude-code) |
| GitHub Copilot in VS Code, the Copilot CLI or the Copilot coding agent, or any other MCP client | The MCP server: [Front doors](https://github.com/hmcts/knowledge-store-mcp#front-doors) |
| A browser, to look around | The explorer: [Use `explorer.html`](#use-explorerhtml) |
| A terminal or a script | The CLI: [Use the terminal](#use-the-terminal) |

Every route reads the same store and gives the same cited answers. The explorer
and the terminal answer fewer kinds of question; they do not answer differently.

Read the coding agent's privacy note in Front doors before enabling it: the agent
repeats what it reads into the repository it works in.

## Other knowledge-store tasks

| Skill | Use it to |
|---|---|
| `/knowledge-store:knowledge-store` | Ask questions of an existing store |
| `/knowledge-store:knowledge-store-build` | Build or refresh a store; see [Creating a knowledge store](creating-a-store.md) or [Refreshing a knowledge store](refreshing-a-store.md) |
| `/knowledge-store:knowledge-store-export` | Produce a dated, evidence-backed finding for a ticket or owner without copying sensitive values into it |

Individual stores can provide an additional skill containing estate-specific
context. The four plugin skills provide the shared query, build, export and
assess workflows.

## Update the plugin

The plugin installs from this repository's `main` branch and has no separate
version. Refresh the marketplace, update the plugin, then reload it:

```
/plugin marketplace update knowledge-store-builder
```

Then open `/plugin`, go to the **Installed** tab, select `knowledge-store` and
choose **Update now**, and load the new skills into the session:

```
/reload-plugins
```

From a shell, outside a session, the same update is two commands. Claude Code
needs a restart to apply it:

```bash
claude plugin marketplace update knowledge-store-builder
claude plugin update knowledge-store@knowledge-store-builder
```

Installing again, or adding the marketplace again, is not the update path.

This marketplace does not update itself. Claude Code auto-updates only the
marketplaces that have auto-update switched on, and third-party marketplaces
start with it off. To turn it on, open `/plugin`, go to the **Marketplaces**
tab, select `knowledge-store-builder` and choose **Enable auto-update**.

Run `claude plugin details knowledge-store` again to check the installed skills.

## Troubleshooting

### The skills do not appear

Run `/reload-plugins` first. If the skills still do not appear, clear Claude
Code's plugin cache, restart Claude Code, then install and reload the plugin
again:

```bash
rm -rf ~/.claude/plugins/cache
```

```
/plugin marketplace add hmcts/knowledge-store-builder
/plugin install knowledge-store@knowledge-store-builder
/reload-plugins
```

If the marketplace is already configured, replace `marketplace add` with:

```
/plugin marketplace update knowledge-store-builder
```

### Installation reports `skills: Invalid input`

This message indicates an invalid component path in the plugin manifest, not an
unsupported `skills` field. Component paths must be relative to the plugin root
and start with `./`; a path such as `.claude/skills/` is invalid, while
`./.claude/skills/` is valid.

The plugin installs from `main`, so a published manifest error must be corrected
there. After it is corrected, follow [Update the plugin](#update-the-plugin).
