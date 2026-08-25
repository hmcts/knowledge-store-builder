"""`iter_array` must find the top-level array, not the first one in byte order (#210).

`iter_array(path, key="nodes")` locked onto the first `"nodes": [` anywhere in the
file. A merged graph carrying hyperedges has `graph.hyperedges[].nodes` — a list of
id **strings** — before its top-level node array, so the iterator yielded strings.
Every consumer that type-checks the item then saw nothing:

    community = node.get("community") if isinstance(node, dict) else None

So `graph_counts` returned `(0, 0)` for a fully clustered graph, and the two guards
built on those counts compared `(0, 0)` against `(0, 0)`, found them equal, and
concluded the two graph files agreed.

**One of those guards was a refusal protecting against an irreversible overwrite.**
A store hit that window during a normal refresh with the two graphs differing by
tens of thousands of communities — the most detectable disagreement possible — and
the guard would have permitted the write. A data-loss guard that cannot fire is
worse than none, because it is believed.

`iter_array`'s docstring already promised "the named top-level array". These tests
pin the code to what it said.

The fixture carries the real shape rather than a convenient one: `graph.hyperedges`
is where the colliding key actually lives, and a fixture without it is what let the
original tests pass while the function returned zero on every real graph.
"""

from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledgestore import graph_files  # noqa: E402
from knowledgestore import graph_stream  # noqa: E402


def _with_hyperedges(nodes, links=None):
    """A graph in the shape a merged estate graph actually has.

    `graph.hyperedges[].nodes` precedes the top-level `nodes` array in byte order,
    which is the whole mechanism.
    """
    return {
        "directed": True,
        "multigraph": False,
        "graph": {"hyperedges": [{"nodes": [n["id"] for n in nodes], "type": "h"}]},
        "nodes": nodes,
        "links": links or [],
    }


class TopLevelArrayTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name, payload):
        path = self.dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    # --- the defect ----------------------------------------------------------

    def test_it_yields_nodes_not_hyperedge_id_strings(self):
        """Breaks if the nested key wins again. The iterator yielded `str` where
        every consumer expects `dict`, and type-checking consumers saw nothing."""
        path = self.write("g.json", _with_hyperedges([{"id": "a"}, {"id": "b"}]))

        got = list(graph_stream.iter_array(path))

        self.assertTrue(all(isinstance(item, dict) for item in got), got)
        self.assertEqual([item["id"] for item in got], ["a", "b"])

    def test_graph_counts_is_not_zero_on_a_clustered_graph(self):
        """Breaks if the counts go back to zero. `(0, 0)` on a fully clustered
        graph is the observable symptom, and it is indistinguishable from an
        unclustered graph — which is why nothing noticed."""
        path = self.write(
            "g.json", _with_hyperedges([{"id": "a", "community": 1}, {"id": "b", "community": 2}])
        )

        self.assertEqual(graph_files.graph_counts(path), (2, 2))

    def test_the_data_loss_refusal_fires_on_a_graph_with_hyperedges(self):
        """Breaks if the #198 refusal is vacuous again.

        This is the case that matters: two graphs that differ, both in the real
        shape. Before the fix both read `(0, 0)`, compared equal, and the stage was
        permitted to overwrite the committed archive from the stale one.
        """
        plain = self.write(
            "graph.json",
            _with_hyperedges([{"id": "a", "community": 1}, {"id": "b", "community": 2}]),
        )
        with gzip.open(self.dir / "graph.json.gz", "wt", encoding="utf-8") as handle:
            json.dump(_with_hyperedges([{"id": "a", "community": 9}]), handle)

        self.assertIn("Refusing to run", graph_files.stale_refusal(plain))

    def test_the_mismatch_line_fires_on_a_graph_with_hyperedges(self):
        """Breaks if the #197 report is vacuous again. Same cause, quieter symptom:
        a stage reports agreement between two graphs it never counted."""
        plain = self.write(
            "graph.json",
            _with_hyperedges([{"id": "a", "community": 1}, {"id": "b", "community": 2}]),
        )
        with gzip.open(self.dir / "graph.json.gz", "wt", encoding="utf-8") as handle:
            json.dump(_with_hyperedges([{"id": "a", "community": 9}]), handle)

        note = graph_files.disagreement(plain, graph_files.graph_counts(plain), "remedy")

        self.assertIn("MISMATCH", note)

    # --- the property, stated generally --------------------------------------

    def test_a_nested_array_of_the_same_name_is_ignored(self):
        """Breaks if depth stops being tracked. Stated without hyperedges so the
        property is pinned generally rather than for one graphify quirk."""
        path = self.write(
            "g.json",
            {"wrapper": {"nodes": ["not", "these"]}, "nodes": [{"id": "real"}]},
        )

        self.assertEqual([n["id"] for n in graph_stream.iter_array(path)], ["real"])

    def test_a_top_level_array_before_a_nested_one_still_works(self):
        """Breaks if the fix only handles the nested-first ordering.

        Byte order was the original bug; a fix that depended on it would be the
        same bug with the sign flipped.
        """
        path = self.write(
            "g.json",
            {"nodes": [{"id": "first"}], "graph": {"hyperedges": [{"nodes": ["x"]}]}},
        )

        self.assertEqual([n["id"] for n in graph_stream.iter_array(path)], ["first"])

    def test_links_and_edges_are_still_found(self):
        """Breaks if only the default key works. `iter_edges` reads both, and a
        graph whose edges silently read as empty ranks nothing downstream."""
        with_links = self.write("l.json", _with_hyperedges([{"id": "a"}], [{"source": "a"}]))
        with_edges = self.write("e.json", {"nodes": [{"id": "a"}], "edges": [{"source": "a"}]})

        self.assertEqual(len(list(graph_stream.iter_array(with_links, key="links"))), 1)
        self.assertEqual(len(list(graph_stream.iter_array(with_edges, key="edges"))), 1)

    def test_a_key_name_inside_a_string_value_is_not_matched(self):
        """Breaks if the scanner counts brackets inside strings.

        A label containing the key name is ordinary in a graph of source code, and
        matching it would restart the scan mid-file at an arbitrary offset.
        """
        path = self.write(
            "g.json",
            {"note": 'a value mentioning "nodes": [ inside a string', "nodes": [{"id": "a"}]},
        )

        self.assertEqual([n["id"] for n in graph_stream.iter_array(path)], ["a"])

    def test_an_absent_array_yields_nothing(self):
        """Breaks if a missing array raises rather than yielding nothing — callers
        rely on the empty case for a layer that has none."""
        path = self.write("g.json", {"graph": {}})
        self.assertEqual(list(graph_stream.iter_array(path)), [])

    def test_a_truncated_array_still_raises(self):
        """Breaks if the fix swallows truncation.

        Returning what was read would be the quiet direction, and this module's
        docstring is explicit that the quiet direction is the dangerous one.
        """
        path = self.dir / "cut.json"
        path.write_text('{"nodes": [{"id": "a"}, {"id": "b"}', encoding="utf-8")

        with self.assertRaises(graph_stream.TruncatedJson):
            list(graph_stream.iter_array(path))

    def test_it_reads_a_gzipped_graph(self):
        """Breaks if the fix works only on the uncompressed path — which is the one
        a store gitignores, so the compressed case is the one that matters."""
        path = self.dir / "g.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(_with_hyperedges([{"id": "a", "community": 3}]), handle)

        self.assertEqual(graph_files.graph_counts(path), (1, 1))


if __name__ == "__main__":
    unittest.main()
