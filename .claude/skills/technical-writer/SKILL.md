---
name: technical-writer
description: Use when writing or reworking any user-facing documentation in this repository — the README, the docs/ guides, skill prose. Enforces the house register - persona-led, value-first, no AI slop - and the verification that documentation is code.
---

# Technical writer

Adapted from the `technical-writer` agent in
[VoltAgent/awesome-claude-code-subagents](https://github.com/VoltAgent/awesome-claude-code-subagents/blob/main/categories/08-business-product/technical-writer.md).
Three of its ideas survive here: know the audience before writing, work in
plan → write → verify phases, and test documentation like code. Its generic
checklist (readability scores, SEO, "positive user feedback") is deliberately
dropped — those are metrics about documentation, not qualities of it.

## Before writing: name the reader

Every document serves exactly one persona. Name them, then answer three
questions before drafting a word:

- **What do they already know?** Do not explain git to an engineer; do not
  assume an engineer knows what a knowledge store is.
- **What do they want in the first sixty seconds?** Engineers skim, decide,
  and leave. The value and the first runnable command belong on the first
  screen; justification comes after, for whoever stays.
- **What must they NOT need to read?** A document that serves two personas
  serves neither. Route the other persona away in the first table.

This repository's personas: the **asker** (has a question, wants the plugin
and nothing else), the **builder** (owns an estate, needs the library and the
tools), and the **evaluator** (deciding whether to adopt; reads only the
README). One document each.

## The register

- **Lead with what the reader gets**, not with what the software is. "Ask a
  question, get a cited answer" beats any architecture description.
- **Commands before prose.** A copy-pasteable block, then the one caveat that
  stops it failing, then explanation for those who want it.
- **Banned:** simply, just, easily, seamlessly, powerful, robust, leverage,
  delve, comprehensive, "it's worth noting", exclamation marks, emoji,
  rhetorical questions as headings.
- **Every claim testable or cited.** A number is measured or absent — counts
  drift, so prefer "measure the artefact" over quoting it. Every command has
  been run, in a clean environment, before it is written down.
- **State the failure alongside the command.** The line that stops someone
  ("`/reload-plugins` is not optional") is worth more than a paragraph of
  description.
- **Cut what the reader can infer.** If a sentence survives deletion without
  the reader losing an action or a warning, delete it.
- British English. Sentence-case headings.

## Verify — documentation is code

- Run every command in the document, or state in the PR why one was not run.
- Check every internal anchor and cross-repository link resolves. Inbound
  deep links from other repositories are load-bearing: renaming a heading
  breaks them silently.
- Read the finished page **as the persona**: can they act within a minute?
  Is the first screen worth their next five?
- Docs that restate a rule from a skill or a master document must name it,
  not copy it — copies drift, and this repository's convention is that the
  master lists its mirrors.
