"""The query index is a faithful, deterministic rewrite of the graph and ticket layer.

A reader opens `store.sqlite` instead of parsing the graph, so the index is only
worth having if it answers what the graph would, rebuilds to the same bytes, and
is never left half written. The fixture is small enough to check by eye: six node
records over two repositories (one a repeated id, one a structural node with no
label, one written with the old `gherkin_feature` kind) and five links, one of
them to a node that does not exist.

Expected values are written out by hand, never taken from the code under test.
Each test names the defect it catches.
"""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import settings_isolation  # noqa: F401  (puts the working tree on sys.path first)
from knowledgestore import graph_stream, store_index


def node(id, label=None, repo="svc-a", kind=None, file="", **extra):
    record = {"id": id, "repo": repo, "source_file": file, **extra}
    if label is not None:
        record["label"] = label
    if kind is not None:
        record["metadata"] = {"kind": kind}
    return record


NODES = [
    node(
        "a1",
        "Payment gateway feature",
        kind="gherkin_feature",
        file="features/pay.feature",
        community=0,
        url="https://example.test/svc-a/features/pay.feature",
    ),
    node("a2", "Payment retry scenario", kind="scenario", file="features/pay.feature", community=0),
    node("a3", "Gateway client", kind="class", file="src/Client.java", community=1),
    node("b1", "Order service", repo="svc-b", kind="class", file="src/Order.java", community=2),
    # A structural node: neither a label nor a source file (CLAUDE.md 6.4).
    {"id": "s1", "repo": "svc-b"},
    # The same id again, disagreeing on the label: which copy survives must not
    # depend on which the graph listed first.
    node("a1", "Payment gateway feature copy", kind="gherkin_feature", file="features/pay.feature"),
]

LINKS = [
    {"source": "a1", "target": "a2", "relation": "contains"},
    {"source": "a2", "target": "a3", "relation": "calls"},
    {"source": "a3", "target": "b1", "relation": "calls"},
    {"source": "b1", "target": "s1", "relation": "contains"},
    {"source": "a1", "target": "ghost", "relation": "calls"},
]

# The layout `file-tickets.json.gz` carries: repo -> repo-relative path -> entry.
TICKETS = {
    "svc-a": {
        "features/pay.feature": {
            "tickets": {"PROJ-1": 2},
            "proposed": {"PROJ-1": 1, "PROJ-2": 1},
            "first": "2024-01-01",
            "last": "2024-06-01",
        },
        "docs/readme.md": {"tickets": {"PROJ-3": 1}, "first": "2024-02-01", "last": "2024-02-01"},
    }
}


def write_graph(path: Path, nodes, links) -> None:
    text = json.dumps({"directed": True, "nodes": nodes, "links": links})
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(text)
    else:
        path.write_text(text, encoding="utf-8")


def write_tickets(path: Path, layer) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(layer, handle)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class IndexCase(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        (self.root / "knowledge").mkdir()
        self.graph = self.root / "knowledge" / "graph.json.gz"
        self.tickets = self.root / "knowledge" / "file-tickets.json.gz"
        self.out = self.root / "knowledge" / "index" / "store.sqlite"
        write_graph(self.graph, NODES, LINKS)
        write_tickets(self.tickets, TICKETS)

    def build(self, out=None, graph=None):
        return store_index.build(self.root, graph or self.graph, self.tickets, out or self.out)

    def query(self, sql, *args, path=None):
        connection = sqlite3.connect(f"file:{path or self.out}?mode=ro", uri=True)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()


class WhatTheIndexHolds(IndexCase):
    def test_the_report_and_row_counts_are_the_fixtures(self):
        """Breaks if a node, edge or ticket is dropped or doubled, or if the report
        stops reconciling: 6 nodes read = 5 rows + 1 duplicate, 5 links read = 4
        edges + 1 dangling + 0 duplicate (CLAUDE.md 2.5)."""
        report = self.build()
        self.assertEqual(
            dataclasses.replace(report, plain_bytes=0, archive_bytes=0),
            store_index.IndexReport(
                nodes_read=6,
                nodes=5,
                duplicate_nodes=1,
                links_read=5,
                edges=4,
                dangling_edges=1,
                duplicate_edges=0,
                ticket_files=2,
                plain_bytes=0,
                archive_bytes=0,
                graph="knowledge/graph.json.gz",
            ),
        )
        self.assertEqual(report.plain_bytes, self.out.stat().st_size)
        self.assertEqual(report.archive_bytes, self.out.with_name("store.sqlite.gz").stat().st_size)
        counts = {
            table: self.query(f"SELECT count(*) FROM {table}")[0][0]
            for table in ("repos", "files", "relations", "nodes", "edges", "tickets")
        }
        # files: three with a node, one for the structural node's empty path, one
        # that only the ticket layer names.
        self.assertEqual(
            counts,
            {"repos": 2, "files": 5, "relations": 2, "nodes": 5, "edges": 4, "tickets": 3},
        )

    def test_the_meta_rows_name_the_sources_and_the_schema(self):
        """Breaks if the index stops saying which graph and ticket layer it came
        from, or which schema a reader should expect."""
        self.build()
        meta = dict(self.query("SELECT key, value FROM meta"))
        self.assertEqual(
            meta,
            {
                "index_schema": "1.0",
                "graph": "knowledge/graph.json.gz",
                "graph_sha256": digest(self.graph)[:16],
                "file_tickets_sha256": digest(self.tickets)[:16],
            },
        )

    def test_a_repeated_node_id_keeps_the_copy_that_sorts_first(self):
        """Breaks if the survivor of a duplicate id is chosen by input order rather
        than content: the same graph, listed the other way, would index differently."""
        self.build()
        self.assertEqual(
            self.query("SELECT label FROM nodes WHERE id = 'a1'"),
            [("Payment gateway feature",)],
        )

    def test_a_label_search_finds_the_hand_listed_nodes(self):
        """Breaks if the full-text index is empty, mis-keyed to the wrong node, or
        stale against the nodes table."""
        self.build()
        sql = (
            "SELECT n.id FROM labels JOIN nodes n ON n.k = labels.rowid"
            " WHERE labels MATCH ? ORDER BY n.id"
        )
        self.assertEqual(self.query(sql, "gateway"), [("a1",), ("a3",)])
        self.assertEqual(self.query(sql, "payment"), [("a1",), ("a2",)])
        self.assertEqual(self.query(sql, "order"), [("b1",)])
        self.assertEqual(self.query(sql, "copy"), [])

    def test_the_neighbours_of_a_node_match_in_both_directions(self):
        """Breaks if an edge is stored with source and target swapped, or the
        reverse index does not answer `who points at this node`."""
        self.build()
        outgoing = self.query(
            "SELECT r.name, t.id FROM edges e JOIN nodes s ON s.k = e.source"
            " JOIN nodes t ON t.k = e.target JOIN relations r ON r.k = e.relation"
            " WHERE s.id = 'a2' ORDER BY r.name, t.id"
        )
        incoming = self.query(
            "SELECT r.name, s.id FROM edges e JOIN nodes s ON s.k = e.source"
            " JOIN nodes t ON t.k = e.target JOIN relations r ON r.k = e.relation"
            " WHERE t.id = 'a2' ORDER BY r.name, s.id"
        )
        self.assertEqual(outgoing, [("calls", "a3")])
        self.assertEqual(incoming, [("contains", "a1")])

    def test_a_dangling_edge_is_counted_and_not_stored(self):
        """Breaks if an edge to a node that is not in the graph reaches the edge
        table, where a reader would follow it to nothing."""
        self.build()
        self.assertEqual(
            self.query(
                "SELECT count(*) FROM edges e JOIN relations r ON r.k = e.relation"
                " JOIN nodes s ON s.k = e.source WHERE s.id = 'a1' AND r.name = 'calls'"
            ),
            [(0,)],
        )

    def test_a_repeated_link_is_counted_and_stored_once(self):
        """Breaks if a duplicate link is dropped without being reported, so that
        links read no longer equal edges + dangling + duplicates."""
        write_graph(self.graph, NODES, LINKS + [LINKS[0], LINKS[0]])
        report = self.build()
        self.assertEqual(
            (report.links_read, report.edges, report.dangling_edges, report.duplicate_edges),
            (7, 4, 1, 2),
        )

    def test_the_unlabeled_node_is_a_node_and_is_not_searchable(self):
        """Breaks if a structural node is lost, or is entered into the full-text
        index with a NULL label (CLAUDE.md 6.4). `labels_docsize` is FTS5's own
        record of which rows it indexed."""
        self.build()
        self.assertEqual(self.query("SELECT label FROM nodes WHERE id = 's1'"), [(None,)])
        indexed = {k for (k,) in self.query("SELECT id FROM labels_docsize")}
        unlabeled = self.query("SELECT k FROM nodes WHERE id = 's1'")[0][0]
        self.assertNotIn(unlabeled, indexed)
        self.assertEqual(len(indexed), 4)

    def test_an_old_kind_is_stored_in_its_current_form(self):
        """Breaks if the raw `metadata.kind` is stored: a reader filtering on
        `feature` would miss every node a pre-rename store wrote (CLAUDE.md 6.5)."""
        self.build()
        self.assertEqual(
            self.query("SELECT id, kind FROM nodes ORDER BY id"),
            [("a1", "feature"), ("a2", "scenario"), ("a3", "class"), ("b1", "class"), ("s1", "")],
        )

    def test_a_missing_field_is_null_and_a_present_one_is_kept(self):
        """Breaks if url or community is invented where the graph has none, or lost
        where it has one."""
        self.build()
        self.assertEqual(
            self.query("SELECT id, url, community FROM nodes WHERE id IN ('a1', 'a3') ORDER BY id"),
            [("a1", "https://example.test/svc-a/features/pay.feature", 0), ("a3", None, 1)],
        )

    def test_tickets_join_to_the_node_through_repo_and_path(self):
        """Breaks if the ticket join key is not (repo, path), or if a ticket in both
        the landed and proposed sections is kept as proposed."""
        self.build()
        rows = self.query(
            "SELECT t.ticket, t.count, t.landed FROM nodes n"
            " JOIN files f ON f.k = n.file JOIN repos r ON r.k = f.repo"
            " JOIN tickets t ON t.file = f.k"
            " WHERE n.id = 'a1' AND r.name = 'svc-a' ORDER BY t.ticket"
        )
        self.assertEqual(rows, [("PROJ-1", 2, 1), ("PROJ-2", 1, 0)])
        by_ticket = self.query(
            "SELECT f.path FROM tickets t JOIN files f ON f.k = t.file"
            " WHERE t.ticket = 'PROJ-3' AND t.landed = 1"
        )
        self.assertEqual(by_ticket, [("docs/readme.md",)])

    def test_a_missing_ticket_layer_gives_an_empty_table_and_a_null_digest(self):
        """Breaks if a store without the ticket layer cannot be indexed, or if the
        digest of an absent layer is written as something other than NULL."""
        self.tickets.unlink()
        report = self.build()
        self.assertEqual(self.query("SELECT count(*) FROM tickets"), [(0,)])
        self.assertEqual(
            self.query("SELECT value FROM meta WHERE key = 'file_tickets_sha256'"), [(None,)]
        )
        self.assertEqual(report.ticket_files, 0)
        self.assertEqual(report.nodes, 5)


def logical_dump(path: Path) -> dict:
    """Every table except `meta`, row keys included, and FTS5's own shadow tables.

    `meta` records a digest of each input file, which differs whenever the input's
    bytes do, so a graph listed in another order cannot give an identical file. What
    must not change is every row, the key it was given, and the full-text segments.
    """
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'meta'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        dump = {}
        for name in tables:
            try:
                sql = f'SELECT rowid, * FROM "{name}" ORDER BY rowid'
                dump[name] = connection.execute(sql).fetchall()
            except sqlite3.OperationalError:
                # WITHOUT ROWID: a scan already returns primary-key order.
                dump[name] = connection.execute(f'SELECT * FROM "{name}"').fetchall()
        return dump
    finally:
        connection.close()


def bulk_nodes(count: int) -> list:
    return [
        node(f"n{i:05d}", f"word{i} alpha{i % 97} beta{i} gamma{i * 7}", file=f"f{i % 50}.py")
        for i in range(count)
    ]


class ABuildIsReproducible(IndexCase):
    def files(self, out: Path) -> tuple[bytes, bytes]:
        return out.read_bytes(), out.with_name(out.name + ".gz").read_bytes()

    def test_a_rebuild_over_the_same_inputs_is_byte_identical(self):
        """Breaks if the plain file or the archive carries anything that changes
        between runs: a timestamp, a filename, an unordered fill, an unmerged
        full-text index. The second build lands on a different path and then over
        the first, so both a fresh write and a replacement are compared."""
        self.build()
        first = self.files(self.out)
        elsewhere = self.root / "knowledge" / "other" / "store.sqlite"
        self.build(out=elsewhere)
        self.assertEqual(self.files(elsewhere), first)
        self.build()
        self.assertEqual(self.files(self.out), first)

    def test_changing_one_label_changes_both_files(self):
        """Breaks if the identity test above is vacuous: a build that ignored its
        input would also be identical. One changed label must change the plain file
        and the archive."""
        self.build()
        before = self.files(self.out)
        changed = [dict(n) for n in NODES]
        changed[3]["label"] = "Order service v2"
        write_graph(self.graph, changed, LINKS)
        self.build()
        after = self.files(self.out)
        self.assertNotEqual(before[0], after[0])
        self.assertNotEqual(before[1], after[1])

    def test_the_order_the_graph_lists_things_in_reaches_no_row_or_key(self):
        """Breaks if any table is filled in input order rather than sorted order, so
        that a node's key, an edge's endpoints or the full-text segments depend on how
        the graph happened to list its nodes and links. The second graph is the first
        with nodes and links reversed, and the ticket layer's entries and sections
        likewise."""
        self.build()
        reordered = self.root / "reordered"
        (reordered / "knowledge").mkdir(parents=True)
        graph = reordered / "knowledge" / "graph.json.gz"
        layer = reordered / "knowledge" / "file-tickets.json.gz"
        write_graph(graph, NODES[::-1], LINKS[::-1])
        write_tickets(
            layer,
            {
                "svc-a": {
                    path: {key: entry[key] for key in reversed(list(entry))}
                    for path, entry in reversed(list(TICKETS["svc-a"].items()))
                }
            },
        )
        out = reordered / "knowledge" / "index" / "store.sqlite"
        store_index.build(reordered, graph, layer, out)
        original = logical_dump(self.out)
        self.assertEqual(logical_dump(out), original)
        # The comparison reads real rows: a dump of nothing would also be equal.
        self.assertEqual(len(original["nodes"]), 5)
        self.assertEqual(len(original["edges"]), 4)
        self.assertEqual([row[2] for row in original["nodes"]], ["a1", "a2", "a3", "b1", "s1"])

    def test_the_archive_is_the_plain_file_without_a_timestamp_or_name(self):
        """Breaks if the archive carries an mtime or filename, which would change its
        bytes on every run, or if it is not a faithful copy of the plain file."""
        self.build()
        plain, packed = self.files(self.out)
        self.assertEqual(packed[4:8], b"\x00\x00\x00\x00")  # MTIME
        self.assertEqual(packed[3], 0)  # FLG: no FNAME
        self.assertEqual(gzip.decompress(packed), plain)


class ABulkBuildIsFinished(IndexCase):
    """20,000 labels is where an unfinished build shows. Smaller sets fit one
    full-text flush and one tidy file whether or not the final steps ran."""

    @classmethod
    def setUpClass(cls):
        # Held on the class: an unreferenced TemporaryDirectory deletes itself.
        cls.scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.scratch.cleanup)
        root = Path(cls.scratch.name)
        graph = root / "graph.json"
        write_graph(graph, bulk_nodes(20000), [])
        cls.bulk = root / "store.sqlite"
        store_index.build(root, graph, root / "absent.json.gz", cls.bulk)

    def test_the_full_text_index_is_merged_to_one_segment(self):
        """Breaks if `optimize` is not run: a bulk insert leaves five segments here,
        each a reader must consult for every search."""
        self.assertEqual(
            self.query("SELECT count(DISTINCT segid) FROM labels_idx", path=self.bulk), [(1,)]
        )

    def test_the_file_has_no_free_pages(self):
        """Breaks if `VACUUM` is not run last: merging the full-text segments frees
        pages, and unvacuumed they stay in the file holding stale bytes that follow
        input order."""
        self.assertEqual(self.query("PRAGMA freelist_count", path=self.bulk), [(0,)])


class NothingIsWrittenHalfDone(IndexCase):
    def pair(self):
        archive = self.out.with_name("store.sqlite.gz")
        return digest(self.out), digest(archive)

    def test_a_truncated_graph_raises_and_creates_no_index(self):
        """Breaks if a graph cut short part way through yields a partial index that a
        reader would trust."""
        text = json.dumps({"nodes": NODES, "links": LINKS})
        truncated = self.root / "knowledge" / "graph.json"
        truncated.write_text(text[: text.rindex("]")], encoding="utf-8")
        with self.assertRaises(graph_stream.TruncatedJson):
            self.build(graph=truncated)
        self.assertFalse(self.out.exists())
        self.assertFalse(self.out.with_name("store.sqlite.gz").exists())
        self.assertEqual(list(self.out.parent.iterdir()), [])

    def test_a_failed_rebuild_leaves_the_previous_index_unchanged(self):
        """Breaks if a rebuild that fails overwrites or deletes the index already
        there, by hash, for both files."""
        self.build()
        before = self.pair()
        text = json.dumps({"nodes": NODES, "links": LINKS})
        truncated = self.root / "knowledge" / "graph.json"
        truncated.write_text(text[: text.rindex("]")], encoding="utf-8")
        with self.assertRaises(graph_stream.TruncatedJson):
            self.build(graph=truncated)
        self.assertEqual(self.pair(), before)
        self.assertEqual(
            sorted(p.name for p in self.out.parent.iterdir()),
            ["store.sqlite", "store.sqlite.gz"],
        )


class FullTextSearchIsAvailable(unittest.TestCase):
    def test_this_environment_can_build_the_index(self):
        """Breaks if the probe reports FTS5 missing where it works, which would refuse
        every build here."""
        self.assertTrue(store_index.fts5_available())


if __name__ == "__main__":
    unittest.main()
