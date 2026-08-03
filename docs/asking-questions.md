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

The output should list three skills: `knowledge-store`,
`knowledge-store-build` and `knowledge-store-export`. This command needs the
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

## Understand the answer

Under the [grounding contract](grounding-and-verification.md), every claim traces
to evidence in the store: a node, edge, source path, schema field, ticket or
commit subject. The answer identifies the repository and the evidence it used.
**Absence of evidence is reported as a finding** — where the store cannot show
something, the answer says so rather than filling the gap from general knowledge.

Treat same-named components in different repositories as independent
implementations unless an edge connects them. A shared name alone does not show
that one calls, owns or depends on another.

A store is a snapshot. It records the source commits used to build it, but a
later clone or pull can still contain a store built before recent source
changes. Answers about recent work should report possible staleness; authored
briefs and summaries can also be older than the graph evidence.

## Ask without Claude Code

### Use `explorer.html`

Clone the complete store and open `graphify-out/explorer.html` in a browser. The
page is self-contained: it needs neither Claude nor network access.

The explorer searches the estate and answers supported question shapes from
pre-computed evidence, including repository, usage, impact and ticket lookups.
It can also return topic briefs written by the store's maintainers. It cannot
compose a new prose answer for an unanticipated question, inspect runtime
behaviour or make a snapshot current.

### Use the terminal

From the root of a store clone:

```bash
pip install graphifyy                    # or: uv tool install graphifyy
gunzip -k graphify-out/graph.json.gz     # stores commit the graph compressed
graphify query "<question>"
```

Run `gunzip` before the query when `graphify-out/graph.json` is absent. The
command reads the uncompressed graph and accepts a question in ordinary
language.

## Other knowledge-store tasks

| Skill | Use it to |
|---|---|
| `/knowledge-store:knowledge-store` | Ask questions of an existing store |
| `/knowledge-store:knowledge-store-build` | Build or refresh a store; see [Creating a knowledge store](creating-a-store.md) or [Refreshing a knowledge store](refreshing-a-store.md) |
| `/knowledge-store:knowledge-store-export` | Produce a dated, evidence-backed finding for a ticket or owner without copying sensitive values into it |

Individual stores can provide an additional skill containing estate-specific
context. The three plugin skills provide the shared query, build and export
workflows.

## Update the plugin

The plugin installs from this repository's `main` branch and has no separate
version. Adding the marketplace again does not fetch newer skills. Update the
marketplace, reinstall the plugin and reload it:

```
/plugin marketplace update knowledge-store-builder
/plugin install knowledge-store@knowledge-store-builder
/reload-plugins
```

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
there. After it is corrected, update the marketplace, reinstall the plugin and
run `/reload-plugins`.
