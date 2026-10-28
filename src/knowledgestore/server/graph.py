"""The graph read whole: what answers when the query index is absent or unusable.

Every node is built exactly as the library's index builder builds its row, so the
fallback answers as the index would. The source is knowledge-store-builder
`src/knowledgestore/store_index.py` (#457, `feat/query-index` at e4eab86):
`_node_rows` and `_link_rows` for each field, `graph_files.iter_edges` for which
key holds the edges, and the staging tables and `_fill` for how SQLite stores each
value, which copy of a repeated id survives, which repositories are listed, and
what makes the build fail. Change one, change this. One difference is deliberate:
the index no longer lists the empty repository name, and `repositories()` here
still does; `results.repository_names` leaves it out above both readers.

One representation is kept: a tuple per node, the edges between them, and an FTS5
index of their labels. The parsed graph JSON they are built from is read without
being cached, so it can be freed once they exist. The FTS5 table is contentless:
it holds the index of the labels and not their text, which the tuples already
carry, and bm25 reads only the index, so it ranks exactly as the index's
external-content `labels` table does.
"""

import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from typing import Any

from .store import Store

# Copied from knowledge-store-builder `src/knowledgestore/kinds.py` `_ALIASES`
# (last changed at a3ed9c5): every kind a store has ever written, mapped to the kind
# written now (library CLAUDE.md 6.5). The index stores the current kind.
LEGACY_KINDS = {
    "feature": "feature",
    "gherkin_feature": "feature",
    "scenario": "scenario",
    "gherkin_scenario": "scenario",
    "ticket": "ticket",
    "jira_ticket": "ticket",
}

# The tokenizer the index's `labels` table declares (index schema 1.0).
TOKENIZER = "unicode61"

# A node as both readers hold it, in this order. `file` is "" for a node with no
# source file, as the index stores it.
Row = tuple[str, str | None, str, str, str, str | None, Any]
# (direction from the asking node, relation, the other node's id)
Edge = tuple[str, str, str]


def as_node(row: Row) -> dict[str, Any]:
    """A node as every tool returns it."""
    node_id, label, repo, kind, file, url, community = row
    return {
        "id": node_id,
        "label": label,
        "repo": repo,
        "kind": kind,
        "file": file or None,
        "url": url,
        "community": community,
    }


def _text(value: Any) -> str | None:
    """`store_index._text`: a label or url that is not text is stored as its text."""
    return None if value is None else str(value)


def _kind(node: dict) -> Any:
    """`kinds.node_kind`, line for line: a legacy kind becomes the current one, any
    other value is returned as it is, and staging stores it as the index would."""
    raw = (node.get("metadata") or {}).get("kind", "")
    return LEGACY_KINDS[raw] if raw in LEGACY_KINDS else raw


def _node_rows(nodes: list) -> Iterator[tuple]:
    """`store_index._node_rows`, field for field, before SQLite converts them."""
    for node in nodes:
        yield (
            node["id"],
            _text(node.get("label")),
            node.get("repo") or "",
            _kind(node),
            node.get("source_file") or "",
            _text(node.get("url")),
            node.get("community"),
        )


def _link_rows(links: list) -> Iterator[tuple]:
    """`store_index._link_rows`."""
    for link in links:
        yield (link["source"], link.get("relation") or "", link["target"])


def _links(graph: dict) -> list:
    """`graph_files.iter_edges`: `links`, or `edges` when `links` has none."""
    links = graph.get("links")
    if not isinstance(links, list) or not links:
        links = graph.get("edges")
    return links if isinstance(links, list) else []


# The library's staging tables and the parts of `_fill` that decide which nodes and
# edges the index holds, copied from `store_index.py` (STAGING and `_fill`, at
# e4eab86). Staging through SQLite, not imitating it, is the point: column affinity
# turns 7 into "7", " 0" and "1e3" into integers, and -0.0 into "0.0", and rejects
# what cannot be bound, exactly as it does for the index.
_STAGING = """
CREATE TABLE stg_nodes(id TEXT, label TEXT, repo TEXT, kind TEXT, file TEXT,
                       url TEXT, community INTEGER);
CREATE TABLE stg_links(source TEXT, relation TEXT, target TEXT);
CREATE INDEX stg_nodes_id ON stg_nodes(id);
"""
_SURVIVORS = (
    "SELECT d.id, d.label, d.repo, d.kind, d.file, d.url, d.community FROM ("
    " SELECT *, ROW_NUMBER() OVER ("
    "  PARTITION BY id ORDER BY repo, file, kind, label, url, community) AS rn"
    " FROM stg_nodes) d"
    " WHERE d.rn = 1 ORDER BY d.id"
)
_LIVE = "source IN (SELECT id FROM stg_nodes) AND target IN (SELECT id FROM stg_nodes)"
_EDGES = f"SELECT DISTINCT source, relation, target FROM stg_links WHERE {_LIVE}"
_DANGLING = (
    "SELECT count(*) FROM stg_links WHERE"
    " source NOT IN (SELECT id FROM stg_nodes) OR target NOT IN (SELECT id FROM stg_nodes)"
)

# What the index builder raises on, building its rows or binding them: a graph it
# cannot index is one this reader refuses, rather than answering differently.
_BUILDER_FAILURES = (
    KeyError,
    TypeError,
    AttributeError,
    ValueError,
    OverflowError,
    sqlite3.Error,
)


class GraphRefused(ValueError):
    """The graph holds something the library's index builder cannot store, so this
    reader refuses it too. The message names the problem, never store text."""


def _stage(graph: dict) -> tuple[list[tuple], list[tuple], list[str]]:
    """The surviving node rows, the edges and the repositories, as the index builder
    would store them. Raises `GraphRefused` where the builder would raise."""
    raw_nodes = graph.get("nodes")
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(_STAGING)
        try:
            db.executemany(
                "INSERT INTO stg_nodes VALUES (?,?,?,?,?,?,?)",
                _node_rows(raw_nodes if isinstance(raw_nodes, list) else []),
            )
        except _BUILDER_FAILURES as error:
            raise GraphRefused(
                f"a graph node the index builder cannot store ({type(error).__name__})"
            ) from None
        try:
            db.executemany("INSERT INTO stg_links VALUES (?,?,?)", _link_rows(_links(graph)))
        except _BUILDER_FAILURES as error:
            raise GraphRefused(
                f"a graph link the index builder cannot store ({type(error).__name__})"
            ) from None
        survivors = db.execute(_SURVIVORS).fetchall()
        # nodes.id and nodes.kind are NOT NULL: the builder's insert fails on either.
        if any(row[0] is None or row[3] is None for row in survivors):
            raise GraphRefused("a graph node with a null id or kind, which the index refuses")
        edges = db.execute(_EDGES).fetchall()
        # The builder's own reconciliation, edges + dangling + duplicates = links read,
        # which is live + dangling = read: a link counted in neither (a null end
        # beside a live one) fails its build.
        links_read = db.execute("SELECT count(*) FROM stg_links").fetchone()[0]
        live = db.execute(f"SELECT count(*) FROM stg_links WHERE {_LIVE}").fetchone()[0]
        dangling = db.execute(_DANGLING).fetchone()[0]
        if live + dangling != links_read:
            raise GraphRefused("a graph link the index builder cannot account for")
        repositories = [
            r for (r,) in db.execute("SELECT DISTINCT repo FROM stg_nodes ORDER BY repo")
        ]
    return survivors, edges, repositories


class GraphReader:
    """The graph, read whole once per loaded store, answering what the index answers."""

    layer = "graph"

    def __init__(self, graph: Any):
        survivors, links, repositories = _stage(graph if isinstance(graph, dict) else {})
        rows: dict[str, Row] = {}
        for node_id, label, repo, kind, file, url, community in survivors:
            # A repository and a kind recur on many nodes: each is held once.
            rows[node_id] = (
                node_id,
                label,
                sys.intern(repo),
                sys.intern(kind),
                file,
                url,
                community,
            )
        self._rows = rows
        self._ids = list(rows)  # in the index's order: ORDER BY id
        self._repositories = repositories
        # Each node's edges both ways, once each, as the index's unique edge key holds.
        edges: dict[str, list[Edge]] = {}
        for source, relation, target in links:
            # The node's own id strings, not the staging copies of them.
            source, target = rows[source][0], rows[target][0]
            relation = sys.intern(relation)
            edges.setdefault(source, []).append(("out", relation, target))
            edges.setdefault(target, []).append(("in", relation, source))
        self._edges = {node_id: sorted(found) for node_id, found in edges.items()}
        self._labels: sqlite3.Connection | None = None

    def repositories(self) -> list[str]:
        """Every repository a staged node names, a repeated id's losing copies
        included, as the index builder collects them. The empty name is kept, on
        purpose, when a node has none: the index's `repos` leaves it out (e4eab86),
        and `results.repository_names` does the same above both readers."""
        return list(self._repositories)

    def node(self, node_id: str) -> dict[str, Any] | None:
        row = self._rows.get(node_id)
        return as_node(row) if row else None

    def edges(self, node_id: str, relation: str | None) -> list[Edge]:
        found = self._edges.get(node_id, [])
        return [edge for edge in found if relation is None or edge[1] == relation]

    def members(self, community: int) -> list[dict[str, Any]]:
        return [as_node(self._rows[i]) for i in self._ids if self._rows[i][6] == community]

    def search(self, query: str, repo: str | None, kind: str | None) -> list[dict[str, Any]]:
        rows = self._label_index().execute(
            "SELECT rowid, bm25(labels) FROM labels WHERE labels MATCH ?", (query,)
        )
        ranked = sorted((score, self._ids[rowid]) for rowid, score in rows)
        return [
            as_node(self._rows[node_id])
            for _, node_id in ranked
            if (repo is None or self._rows[node_id][2] == repo)
            and (kind is None or self._rows[node_id][3] == kind)
        ]

    def _label_index(self) -> sqlite3.Connection:
        """Built on the first search; a store that is never searched never pays for it."""
        if self._labels is None:
            db = sqlite3.connect(":memory:")
            db.execute(
                f"CREATE VIRTUAL TABLE labels USING fts5(label, content='', tokenize='{TOKENIZER}')"
            )
            db.executemany(
                "INSERT INTO labels(rowid, label) VALUES (?, ?)",
                (
                    (k, self._rows[node_id][1])
                    for k, node_id in enumerate(self._ids)
                    if self._rows[node_id][1] is not None
                ),
            )
            self._labels = db
        return self._labels

    def close(self) -> None:
        if self._labels is not None:
            self._labels.close()


# The builder's `_TICKET_SECTIONS`: the two kinds of ticket-layer entry.
_TICKET_SECTIONS = ("tickets", "proposed")


def ticket_repositories(layer: Any) -> set[str]:
    """The repositories `store_index._ticket_rows` stages from the ticket layer
    (`file-tickets.json.gz`): those with at least one ticket under a path, as the
    builder walks it, skipping any entry that is not an object."""
    found: set[str] = set()
    for repo, paths in (layer if isinstance(layer, dict) else {}).items():
        if not isinstance(paths, dict):
            continue
        for entry in paths.values():
            if not isinstance(entry, dict):
                continue
            if any(isinstance(entry.get(s), dict) and entry[s] for s in _TICKET_SECTIONS):
                found.add(repo)
    return found


def read(store: Store) -> GraphReader:
    """The store's graph reader, built once per loaded store and closed with it. A
    refused graph is remembered too, so it is not read again on every call."""
    held = store.memo("graph.reader", lambda: _build(store), close=_close)
    if isinstance(held, GraphRefused):
        raise GraphRefused(str(held))
    return held


def _build(store: Store) -> "GraphReader | GraphRefused":
    try:
        return GraphReader(store.read("graph"))
    except GraphRefused as refused:
        # A fresh copy, never raised: the caught one's traceback and context hold
        # the frames that parsed the graph, which would keep it all alive.
        return GraphRefused(str(refused))


def _close(held: "GraphReader | GraphRefused") -> None:
    if isinstance(held, GraphReader):
        held.close()
