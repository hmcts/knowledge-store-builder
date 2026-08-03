# UI estate knowledge context

## Purpose

This repository creates a combined knowledge graph of the UI estate.

It combines:

1. the current checked-out source code from each UI repository;
2. historical Git commit metadata;
3. changed-file information for each commit;
4. cross-repository architectural and product context;
5. Graphify's generated graph and architecture report.

## Source-of-truth rules

The checked-out code under `repositories/` is the source of truth for the
current implementation.

The files under `knowledge/git-history/` describe how the implementation
changed over time. Historical commit messages may be incomplete, outdated
or inaccurate and must not override evidence in the current source.

Generated Graphify output is an index over the underlying material. It must
not be treated as more authoritative than the source files from which it
was produced.

## Included repositories

- `repo-a`

See [the repository manifest](knowledge/repository-manifest.md) for source
locations and dataset details.

## Dataset layout

Each repository has:

- `index.md`: repository-level history overview;
- `<year>.md`: human-readable and Graphify-readable commit history;
- `commits.ndjson`: complete structured commit metadata.

The Markdown dataset contains commit messages, dates, parent relationships,
references, change statistics and changed-file paths.

Complete historical patches are intentionally excluded. The present source
code already provides the current implementation, while commit metadata
provides historical reasoning and provenance without duplicating every
historical version of every file.

## How to interpret relationships

A commit changing a file indicates historical modification, not necessarily
architectural ownership.

Frequent modification by an author does not necessarily imply current team
ownership.

Branches, tags and merge commits provide release and integration context,
but repository-specific branching practices may differ.

Similar class, module or service names across repositories do not prove that
they represent the same logical capability. Cross-repository relationships
should be supported by imports, API contracts, shared packages,
documentation or explicit historical evidence.

## Useful questions

The graph should support questions such as:

- Which repositories implement a particular user journey?
- Which UI applications consume a particular API?
- How are authentication and authorisation implemented across the estate?
- Which repositories use the same shared components?
- When was a capability introduced?
- Which commits changed a particular component?
- Which files commonly change together?
- Where have similar architectural decisions been implemented differently?
- Which parts of the UI estate appear duplicated?
- Which repositories have the greatest dependency on legacy components?

## Refresh process

The graph must be regenerated after source repositories or generated history
files change.

The normal process is:

1. synchronise all source repositories;
2. regenerate Git-history datasets;
3. regenerate this context and the repository manifest;
4. run Graphify;
5. review the generated report;
6. commit the changed datasets and `graphify-out/`.
