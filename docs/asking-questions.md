# Asking questions of a knowledge store

You have a question about a codebase estate — which repositories implement a
thing, what breaks if a component changes, why some code exists — and the
estate already has a knowledge store. This page gets you from nothing to a
cited answer.

## What you need

**Claude Code with this repository's plugin. Nothing else** — no Python, no
`pip`, and no access to the estate's source code.

The plugin is instructions, not software: skills that tell Claude how to find
a store, traverse it and cite what it finds. The one tool the query skill
runs is the `graphify` CLI, and it installs that itself on first use.

## Install

```
/plugin marketplace add hmcts/knowledge-store-builder
/plugin install knowledge-store@knowledge-store-builder
/reload-plugins
```

- `install` asks for a scope: **user** (you, everywhere), **project**
  (everyone on the current repository), **local** (you, here only).
- **`/reload-plugins` is not optional.** Without it the plugin is installed
  but inactive in the session you are sitting in — which looks exactly like a
  broken install.

Confirm it worked: `/reload-plugins` reports `3 skills`, and
`claude plugin details knowledge-store` lists them by name alongside the
token cost the plugin adds to each session.

## Ask

Ask in your own words — there is no query syntax:

- *Which repositories implement their own address formatting?*
- *What is impacted if this component changes?*
- *Walk me through what happens when a user submits this form.*
- *Why does this module exist, and which tickets shaped it?*

The `knowledge-store` skill finds a store before answering: the working
directory first, then `$KNOWLEDGE_STORE`, then a location it remembered from
last time, then the obvious places under your home directory — and if there
is none, it asks where you want one and clones it.

## What an answer is, and is not

- **Every claim traces to evidence in the store** — a node, an edge, a commit,
  a schema field. If the store cannot show it, the answer says so rather than
  guessing: absence of evidence is reported as a finding.
- **Same-named components in different repositories are independent
  implementations** unless an edge connects them. Proving that is one of the
  most useful answers a store gives.
- **The store is a snapshot.** It records the commit it was built from; if
  your question concerns last week's changes, expect the answer to flag
  staleness.

## No Claude licence?

Every store ships `explorer.html` — open it in a browser, nothing to install,
no network access. It searches the whole estate and answers recognised
question shapes (which repositories, where used, what is impacted, ticket
lookups) from pre-computed evidence, plus any topic briefs the store's
maintainers have written. It cannot compose prose for a question nobody
anticipated, and the page says so.

There is also `graphify query "<question>"` from a terminal, inside a clone
of the store.

## The three skills

| Skill | Use it to |
|---|---|
| `knowledge-store` | ask a store questions — this page |
| `knowledge-store-build` | build or refresh a store — see [Creating a store](creating-a-store.md) |
| `knowledge-store-export` | hand a finding to a ticket or a data-protection owner, without the sensitive values |

Individual stores can add their own thin skill for estate specifics — which
repositories matter, where journeys are written up — and leave the mechanics
to these three.

## Keeping the plugin current

Installs come from this repository's `main`, and the plugin has no version —
whatever is on `main` is what installs. Two consequences:

- **`add` does not re-fetch.** Take newer skills with
  `/plugin marketplace update knowledge-store-builder`, then reinstall.
- **A local branch is not installable.** A skill change ships when it merges,
  not before — and do not install from a local path to test one: that
  resolves to a temporary copy that is not what consumers get, and any
  manifest error it reports is about that copy.

If skills never appear after an install, the documented fix is to clear the
plugin cache, restart Claude Code, and reinstall:

```bash
rm -rf ~/.claude/plugins/cache
```

`skills: Invalid input` on install means the plugin manifest on `main` is
broken — usually a component path missing its `./` prefix — not a problem
with your machine. `CLAUDE.md` in this repository has the layout rules.
