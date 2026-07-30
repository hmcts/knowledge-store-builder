
from __future__ import annotations

import json
from pathlib import Path


from . import config

HISTORY_DIR = config.HISTORY_DIR
MANIFEST_PATH = config.MANIFEST_PATH
CONTEXT_PATH = config.CONTEXT_PATH


def read_first_record(ndjson_path: Path) -> dict[str, object]:
    with ndjson_path.open("r", encoding="utf-8") as source:
        first_line = source.readline().strip()

    if not first_line:
        return {}

    return json.loads(first_line)


def count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8") as source:
        return sum(1 for _ in source)


def build_manifest(repository_dirs: list[Path]) -> None:
    lines = [
        "# Repository manifest",
        "",
        "This manifest describes the source repositories represented by "
        "the combined Graphify knowledge graph.",
        "",
        "| Repository | Commits | History | Current source |",
        "|---|---:|---|---|",
    ]

    for repository_dir in repository_dirs:
        ndjson_path = repository_dir / "commits.ndjson"
        first_record = read_first_record(ndjson_path)
        commit_count = count_lines(ndjson_path)

        repository_name = repository_dir.name
        source_url = str(
            first_record.get("repository_url", "Unknown")
        )

        lines.append(
            f"| `{repository_name}` "
            f"| {commit_count} "
            f"| [History](git-history/{repository_name}/index.md) "
            f"| `{source_url}` |"
        )

    lines.extend(
        [
            "",
            "The repositories themselves are cloned into `repositories/` "
            "during graph generation and are not committed to this parent "
            "repository.",
            "",
        ]
    )

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def build_context(repository_dirs: list[Path]) -> None:
    repository_names = [
        repository_dir.name
        for repository_dir in repository_dirs
    ]

    repository_list = "\n".join(
        f"- `{repository_name}`"
        for repository_name in repository_names
    )

    context = f"""# UI estate knowledge context

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

{repository_list}

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
"""

    CONTEXT_PATH.write_text(context, encoding="utf-8")


def main() -> None:
    repository_dirs = sorted(
        path
        for path in HISTORY_DIR.iterdir()
        if path.is_dir() and (path / "commits.ndjson").is_file()
    )

    build_manifest(repository_dirs)
    build_context(repository_dirs)

    print(
        f"Generated context for {len(repository_dirs)} repositories"
    )


if __name__ == "__main__":
    main()