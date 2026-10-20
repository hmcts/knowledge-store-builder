"""A SQLite query index of a knowledge store, so a reader never parses the graph.

The graph is one JSON document of hundreds of thousands of nodes. A reader that
wants "the neighbours of this node" or "which files did this ticket touch" should
open one file read-only and ask, not load the whole graph to answer. This module
writes that file, and a deterministic gzip of it beside it.

**Integer keys.** Every long node id appears once, in `nodes`. The edge table and
both of its indexes hold integers. Repeating the id string everywhere was measured
at five times the size (1,664 MB against 350 MB on the largest local store graph),
which is the difference between an archive GitHub accepts and one it refuses.

**Deterministic, per SQLite build.** Two runs on the same inputs give the same
bytes (CLAUDE.md 1.3). What makes that hold:

- a fixed `page_size` and `journal_mode=OFF`;
- every final table filled by one `INSERT ... SELECT ... ORDER BY`, so row keys
  follow sorted content and never the order the graph happened to list things in;
- a duplicate node id resolved by a content tiebreak, not by which came first;
- the full-text index filled in key order and then `optimize`d;
- `VACUUM` last, because freed staging pages otherwise keep whatever the input
  order left in them;
- no timestamps, and the archive written with `mtime=0` and no filename.

The database header records the SQLite library version, so two different SQLite
builds may produce different bytes from the same store. Within one environment a
rebuild is byte-identical.

**Never `io.load_graph`.** The graph is streamed, one object at a time, through
`graph_stream.iter_array` (CLAUDE.md 6.6).

**Disk.** Staging tables live in SQLite's temp directory (`TMPDIR`), pinned to a
file rather than memory. Allow for the staging and sort spill there, plus the
finished `.building` file and its archive beside the output. At 750,000 nodes and
1,450,000 links the index is about 380 MB and its archive 66 MB.

**Written once or not at all.** The build goes to a temporary file beside the
output and is renamed into place, so a graph that turns out to be truncated part
way through leaves the previous index untouched and no partial one behind.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import graph_files, graph_stream, io, kinds

SCHEMA: tuple[int, int] = (1, 0)

DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
CREATE TABLE repos(k INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE files(k INTEGER PRIMARY KEY, repo INTEGER REFERENCES repos(k),
                   path TEXT NOT NULL, UNIQUE(repo, path));
CREATE TABLE relations(k INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE nodes(k INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, label TEXT,
                   kind TEXT NOT NULL, file INTEGER NOT NULL REFERENCES files(k),
                   url TEXT, community INTEGER);
CREATE TABLE edges(source INTEGER NOT NULL, relation INTEGER NOT NULL, target INTEGER NOT NULL,
                   PRIMARY KEY(source, relation, target)) WITHOUT ROWID;
CREATE INDEX edges_in ON edges(target, relation, source);
CREATE TABLE tickets(file INTEGER NOT NULL REFERENCES files(k), ticket TEXT NOT NULL,
                     count INTEGER NOT NULL, landed INTEGER NOT NULL,
                     PRIMARY KEY(file, ticket)) WITHOUT ROWID;
CREATE INDEX tickets_by_id ON tickets(ticket, landed, file);
CREATE VIRTUAL TABLE labels USING fts5(label, content='nodes', content_rowid='k',
                                       tokenize='unicode61');
"""

STAGING = """
CREATE TEMP TABLE stg_nodes(id TEXT, label TEXT, repo TEXT, kind TEXT, file TEXT,
                            url TEXT, community INTEGER);
CREATE TEMP TABLE stg_links(source TEXT, relation TEXT, target TEXT);
CREATE TEMP TABLE stg_tickets(repo TEXT, path TEXT, ticket TEXT, count INTEGER, landed INTEGER);
CREATE INDEX temp.stg_nodes_id ON stg_nodes(id);
"""

# The two kinds of ticket-layer entry, in the order they are staged. An id in both
# stays landed only; the intent layer already guarantees that, and the staging
# keeps it so a hand-edited layer cannot make one file count a ticket twice.
_TICKET_SECTIONS = (("tickets", 1), ("proposed", 0))


@dataclass(frozen=True)
class IndexReport:
    """What was read and what was written, reconciled.

    `nodes_read` = `nodes` + `duplicate_nodes`, and `links_read` = `edges` +
    `dangling_edges` + `duplicate_edges` (CLAUDE.md 2.5): a count that does not
    reconcile is a row that went somewhere unreported.
    """

    nodes_read: int
    nodes: int
    duplicate_nodes: int
    links_read: int
    edges: int
    dangling_edges: int
    duplicate_edges: int
    ticket_files: int
    plain_bytes: int
    archive_bytes: int
    graph: str


def fts5_available() -> bool:
    """True when this SQLite build can create an FTS5 table."""
    try:
        with contextlib.closing(sqlite3.connect(":memory:")) as probe:
            probe.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    return True


def _text(value) -> str | None:
    return None if value is None else str(value)


def _node_rows(graph: Path):
    for node in graph_stream.iter_array(graph, "nodes"):
        yield (
            node["id"],
            _text(node.get("label")),
            node.get("repo") or "",
            kinds.node_kind(node),
            node.get("source_file") or "",
            _text(node.get("url")),
            node.get("community"),
        )


def _link_rows(graph: Path):
    for link in graph_files.iter_edges(graph):
        yield (link["source"], link.get("relation") or "", link["target"])


def _ticket_rows(layer: dict):
    """One row per (repo, path, ticket), sorted so the staging order is a function of
    the layer's content. Returns the rows and the number of files the layer names."""
    rows = []
    files = 0
    for repo, paths in layer.items():
        if not isinstance(paths, dict):
            continue
        for path, entry in paths.items():
            if not isinstance(entry, dict):
                continue
            files += 1
            seen: set[str] = set()
            for section, landed in _TICKET_SECTIONS:
                counts = entry.get(section)
                if not isinstance(counts, dict):
                    continue
                for ticket, count in counts.items():
                    if ticket in seen:
                        continue
                    seen.add(ticket)
                    rows.append((repo, path, ticket, count, landed))
    return rows, files


def _scalar(connection: sqlite3.Connection, sql: str) -> int:
    return connection.execute(sql).fetchone()[0]


def _fill(connection: sqlite3.Connection) -> dict:
    """Rewrite the staged rows into the final tables, each in sorted order, and
    return the counts the report needs."""
    execute = connection.execute

    execute(
        "INSERT INTO repos(name) SELECT repo FROM ("
        " SELECT repo FROM stg_nodes UNION SELECT repo FROM stg_tickets)"
        " WHERE repo <> '' ORDER BY repo"
    )
    execute(
        "INSERT INTO files(repo, path)"
        " SELECT r.k, u.path FROM ("
        "  SELECT repo, file AS path FROM stg_nodes"
        "  UNION SELECT repo, path FROM stg_tickets) u"
        " LEFT JOIN repos r ON r.name = u.repo ORDER BY r.k, u.path"
    )

    # Counted before each insert and never left to OR IGNORE: a row that goes
    # missing has to be in the report.
    nodes_read = _scalar(connection, "SELECT count(*) FROM stg_nodes")
    distinct_nodes = _scalar(connection, "SELECT count(DISTINCT id) FROM stg_nodes")
    links_read = _scalar(connection, "SELECT count(*) FROM stg_links")
    live = "source IN (SELECT id FROM stg_nodes) AND target IN (SELECT id FROM stg_nodes)"
    live_links = _scalar(connection, f"SELECT count(*) FROM stg_links WHERE {live}")
    distinct_links = _scalar(
        connection,
        "SELECT count(*) FROM (SELECT DISTINCT source, relation, target FROM stg_links"
        f" WHERE {live})",
    )
    dangling = _scalar(
        connection,
        "SELECT count(*) FROM stg_links WHERE"
        " source NOT IN (SELECT id FROM stg_nodes) OR target NOT IN (SELECT id FROM stg_nodes)",
    )

    execute(
        "INSERT INTO relations(name) SELECT DISTINCT relation FROM stg_links"
        " WHERE source IN (SELECT id FROM stg_nodes) AND target IN (SELECT id FROM stg_nodes)"
        " ORDER BY relation"
    )
    # The ORDER BY below stays although the window function happens to emit ids
    # sorted today: SQLite does not promise that order, and the node keys, every edge
    # keyed on them and the FTS rowids follow whatever order this insert produces.
    # A repeated id keeps the row that sorts first on its content, so which of two
    # disagreeing copies survives does not depend on the order the graph listed them.
    execute(
        "INSERT INTO nodes(id, label, kind, file, url, community)"
        " SELECT d.id, d.label, d.kind, f.k, d.url, d.community FROM ("
        "  SELECT *, ROW_NUMBER() OVER ("
        "   PARTITION BY id ORDER BY repo, file, kind, label, url, community) AS rn"
        "  FROM stg_nodes) d"
        " LEFT JOIN repos r ON r.name = d.repo"
        " JOIN files f ON f.repo IS r.k AND f.path = d.file"
        " WHERE d.rn = 1 ORDER BY d.id"
    )
    execute(
        "INSERT INTO edges(source, relation, target)"
        " SELECT s, r, t FROM ("
        "  SELECT (SELECT k FROM nodes WHERE id = l.source) AS s,"
        "         (SELECT k FROM relations WHERE name = l.relation) AS r,"
        "         (SELECT k FROM nodes WHERE id = l.target) AS t"
        "  FROM (SELECT DISTINCT source, relation, target FROM stg_links) l)"
        " WHERE s IS NOT NULL AND t IS NOT NULL ORDER BY s, r, t"
    )
    execute(
        "INSERT INTO tickets(file, ticket, count, landed)"
        " SELECT f.k, t.ticket, t.count, t.landed FROM stg_tickets t"
        " LEFT JOIN repos r ON r.name = t.repo"
        " JOIN files f ON f.repo IS r.k AND f.path = t.path ORDER BY f.k, t.ticket"
    )

    execute(
        "INSERT INTO labels(rowid, label) SELECT k, label FROM nodes"
        " WHERE label IS NOT NULL ORDER BY k"
    )
    execute("INSERT INTO labels(labels) VALUES('optimize')")

    nodes = _scalar(connection, "SELECT count(*) FROM nodes")
    edges = _scalar(connection, "SELECT count(*) FROM edges")
    # Each term was measured on its own, so the sum can disagree. A residual would
    # call an edge the insert lost a duplicate (CLAUDE.md 2.7).
    duplicate_edges = live_links - distinct_links
    if edges + dangling + duplicate_edges != links_read:
        raise RuntimeError(
            f"{links_read} links read but {edges} edges + {dangling} dangling + "
            f"{duplicate_edges} duplicate accounted for: an edge was lost"
        )
    return {
        "nodes_read": nodes_read,
        "nodes": nodes,
        "duplicate_nodes": nodes_read - distinct_nodes,
        "links_read": links_read,
        "edges": edges,
        "dangling_edges": dangling,
        "duplicate_edges": duplicate_edges,
    }


def _build_database(path: Path, graph: Path, layer: dict, meta: dict) -> dict:
    # isolation_level=None: transactions are explicit, because VACUUM cannot run
    # inside one and has to come after the commit.
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA page_size=4096")
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        # Staging belongs on disk, not in memory, whatever this SQLite was built with.
        connection.execute("PRAGMA temp_store=FILE")
        connection.executescript(DDL + STAGING)
        connection.execute("BEGIN")

        connection.executemany("INSERT INTO stg_nodes VALUES (?,?,?,?,?,?,?)", _node_rows(graph))
        connection.executemany("INSERT INTO stg_links VALUES (?,?,?)", _link_rows(graph))
        ticket_rows, ticket_files = _ticket_rows(layer)
        connection.executemany("INSERT INTO stg_tickets VALUES (?,?,?,?,?)", ticket_rows)

        counts = _fill(connection)
        counts["ticket_files"] = ticket_files

        connection.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", sorted(meta.items()))
        for table in ("stg_nodes", "stg_links", "stg_tickets"):
            connection.execute(f"DROP TABLE temp.{table}")
        connection.execute("COMMIT")
        connection.execute("VACUUM")
    finally:
        connection.close()
    return counts


def _write_archive(source: Path, destination: Path) -> None:
    """gzip -9 with no timestamp and no filename, through the one writer that
    guarantees it. The helper yields a text stream; its `buffer` is the gzip stream
    itself, which is what a binary file needs."""
    with open(source, "rb") as plain, io.gzip_text(destination) as packed:
        shutil.copyfileobj(plain, packed.buffer, 1 << 20)


def build(root: Path, graph: Path, file_tickets: Path, out: Path) -> IndexReport:
    """Write `out` and `out` + ".gz" from the graph and the ticket layer.

    `root` is the store root, only so `meta` can name its sources relative to it.
    A missing `file_tickets` gives an empty `tickets` table and a NULL digest.
    Raises, and writes nothing, if FTS5 is missing or the graph cannot be read to
    its end (`graph_stream.TruncatedJson`).
    """
    if not graph.is_file():
        # graph_stream yields nothing for an absent file, which would write a valid,
        # empty index over a good one.
        raise FileNotFoundError(graph)
    if not fts5_available():
        raise RuntimeError(
            "this SQLite build has no FTS5, which the label search needs. "
            "Install a Python built against SQLite with FTS5 enabled."
        )
    io.checked_write_target(out)
    archive = out.with_name(out.name + ".gz")
    out.parent.mkdir(parents=True, exist_ok=True)

    digests = io.layer_digests([graph, file_tickets], root)
    graph_name, tickets_name = list(digests)
    meta = {
        "index_schema": f"{SCHEMA[0]}.{SCHEMA[1]}",
        "graph": graph_name,
        "graph_sha256": digests[graph_name],
        "file_tickets_sha256": digests[tickets_name],
    }
    layer = io.read_gzip_json_dict(file_tickets)

    building = out.with_name(out.name + ".building")
    packing = archive.with_name(archive.name + ".building")
    try:
        building.unlink(missing_ok=True)
        counts = _build_database(building, graph, layer, meta)
        _write_archive(building, packing)
        # Both finished before either is renamed, so a failure above leaves the
        # previous pair exactly as it was.
        os.replace(building, out)
        os.replace(packing, archive)
    finally:
        building.unlink(missing_ok=True)
        packing.unlink(missing_ok=True)

    return IndexReport(
        **counts,
        plain_bytes=out.stat().st_size,
        archive_bytes=archive.stat().st_size,
        graph=graph_name,
    )
