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

For whoever holds the corpus, the checked-out code under `repositories/` is
the source of truth for the current implementation. The corpus is not
committed, so it is never what a citation points at.

In an answer or an exported finding, cite a file as a link pinned to the
commit the store was built from:

    https://github.com/<org>/<repo>/blob/<provenance sha>/<path>#L<line>

Graph nodes carry this link in their `url` field: copy it. In a store where
nodes carry `url`, a node without one was refused a link for a reason (another
host, no recorded commit, a path outside its repository): cite its repository
and repo-relative path, and say no link is available. Build the form yourself
only for a file no node names, such as a corpus-search match, or in a store
where no node carries `url`, from the repository's github.com clone URL in
`config/repositories.txt` and its sha in `knowledge/provenance.json`; leave
off `#L<line>` when no line is known. Never cite `repositories/<repo>/...`:
no reader can open that path.

Authored topic briefs and deep dives keep file paths in backticks instead:
the page renderer links them, and renders no hand-written link.

The files under `knowledge/git-history/` describe how the implementation
changed over time. Historical commit messages may be incomplete, outdated
or inaccurate and must not override evidence in the current source.

Generated Graphify output is an index over the underlying material. It must
not be treated as more authoritative than the source files from which it
was produced.

A ticket listed under `proposed` in the intent index
(`knowledge/intent/file-tickets.json.gz`) was found on a branch, and that work
is not confirmed on the default branch. Say it is proposed on branch X, naming
the branch from the ticket's `branches` in `ticket-descriptions.json.gz`, and
check the checked-out code before saying anything about what the code does. A
squash merge can land a branch's work without its commits, so a proposed
ticket is not evidence that the code lacks the change either.

## Included repositories

- `alpha-lib`
- `beta-app`
- `delta-deploy`
- `eta-portal`

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
