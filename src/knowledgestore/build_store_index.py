"""Build the query index: one SQLite file a reader opens instead of parsing the graph.

Reads the graph and the ticket layer, writes `knowledge/index/store.sqlite` and its
deterministic archive beside it. Run it after any change to the graph or to
`file-tickets.json.gz`, because the index is a copy of both and `status` reports
when it has stopped matching.

Streams the graph and never loads it (CLAUDE.md 6.6). The schema and the
determinism rules are in `store_index`.

Exits 0 on success, 1 when there is no graph, the graph is truncated or this SQLite
has no FTS5, and 2 when the target cannot be written.
"""

from __future__ import annotations

import sqlite3
import sys
import zlib

from . import config, graph_files, graph_stream, store_index

GITHUB_LIMIT_MB = 100


def over_warning(archive_bytes: int, warn_mb: float) -> bool:
    """True when the archive is strictly larger than the warning threshold."""
    return archive_bytes > warn_mb * 1_000_000


def _describe(report: store_index.IndexReport) -> str:
    return (
        f"Query index from {report.graph}: "
        f"{report.nodes_read:,} nodes read = {report.nodes:,} nodes + "
        f"{report.duplicate_nodes:,} duplicate; "
        f"{report.links_read:,} links read = {report.edges:,} edges + "
        f"{report.dangling_edges:,} dangling + {report.duplicate_edges:,} duplicate; "
        f"{report.ticket_files:,} files carry tickets."
    )


def main() -> int:
    graph = graph_files.graph_to_read(config.GRAPH_PATH)
    if graph is None:
        candidates = [config.GRAPH_PATH, graph_files.counterpart(config.GRAPH_PATH)]
        print(
            "No graph found: looked for " + " and ".join(str(path) for path in candidates),
            file=sys.stderr,
        )
        return 1

    out = config.INDEX_PATH
    if not store_index.fts5_available():
        print(
            f"This SQLite (version {sqlite3.sqlite_version}) has no FTS5, which the label "
            "search needs. Install a Python built against SQLite with FTS5 enabled.",
            file=sys.stderr,
        )
        return 1
    try:
        report = store_index.build(config.ROOT, graph, config.INTENT_INDEX_PATH, out)
    except (graph_stream.TruncatedJson, EOFError, zlib.error) as error:
        print(
            f"Cannot read {graph} to its end ({error}). Nothing was written; any existing "
            "index is untouched.",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError) as error:
        print(f"Cannot write the index at {out}: {error}", file=sys.stderr)
        return 2

    print(_describe(report))
    print(f"{out}: {report.plain_bytes:,} bytes")
    print(f"{out}.gz: {report.archive_bytes:,} bytes")
    if over_warning(report.archive_bytes, config.INDEX_WARN_MB):
        relative = out.relative_to(config.ROOT) if out.is_relative_to(config.ROOT) else out
        print(
            f"Warning: the archive is over {config.INDEX_WARN_MB} MB, and GitHub rejects a "
            f"push carrying a file over {GITHUB_LIMIT_MB} MB. Either keep committing it, "
            f"if it stays under that, or gitignore both `{relative}` and `{relative}.gz` "
            "and publish the index some other way.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
