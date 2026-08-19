"""Streaming one array of a graph file, and this suite proving it can still tell.

Every assertion here is checked against loading the file, which is the
implementation the numbers used to come from. The last class breaks the streamer
on purpose and requires the comparison to notice - because a comparison that
cannot fail is worse than no comparison, and nothing in a green run says which
of the two you have.
"""

from __future__ import annotations

import gzip
import itertools
import json
import tempfile
import unittest
from pathlib import Path

from knowledgestore import graph_stream


def write(directory: Path, graph: dict, compress: bool = False) -> Path:
    path = directory / ("g.json.gz" if compress else "g.json")
    text = json.dumps(graph)
    if compress:
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


class ItYieldsWhatLoadingYields(unittest.TestCase):
    def _same(self, graph: dict, key: str = "nodes", compress: bool = False) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp), graph, compress)
            self.assertEqual(list(graph_stream.iter_array(path, key)), graph.get(key, []))

    def test_nodes_and_links_are_both_reachable(self):
        graph = {
            "nodes": [{"id": "a", "community": 1}, {"id": "b", "community": 2}],
            "links": [{"source": "a", "target": "b"}],
        }
        self._same(graph, "nodes")
        self._same(graph, "links")

    def test_an_absent_array_yields_nothing_rather_than_raising(self):
        """Absent and empty are both legitimately "no such content", and callers
        already report that in their own words."""
        self._same({"links": []}, "nodes")
        self._same({"nodes": []}, "nodes")

    def test_a_missing_file_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(list(graph_stream.iter_array(Path(tmp) / "absent.json")), [])

    def test_the_key_must_be_at_a_structural_position(self):
        """A label can contain `"nodes": [`. Starting there would stream the wrong array."""
        graph = {
            "graph": {"note": 'metadata mentioning "nodes": [{"id": "decoy"}] on purpose'},
            "nodes": [{"id": "real"}],
        }
        self._same(graph, "nodes")

    def test_nested_structures_and_unicode(self):
        self._same(
            {
                "nodes": [
                    {"id": "a", "meta": {"tickets": ["A-1"], "deep": [[1], [2]]}},
                    {"id": "b", "label": "Llŷn Peninsula – Cymraeg"},
                ]
            }
        )

    def test_a_graph_spanning_many_reads(self):
        """Where hand-rolled streaming actually breaks: objects straddling buffers."""
        graph = {"nodes": [{"id": f"n{i}", "label": "x" * 200} for i in range(20000)]}
        self._same(graph)
        self._same(graph, compress=True)


class TruncationRaises(unittest.TestCase):
    """An array that opens and never closes must raise, not return what it read.

    A caller counting nodes would otherwise get a smaller number that looks
    exactly like a real one - the shape of every expensive mistake in this
    pipeline.
    """

    def test_a_truncated_array_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cut.json"
            path.write_text('{"nodes": [{"id": "a"}, {"id": "b"}', encoding="utf-8")
            with self.assertRaises(graph_stream.TruncatedJson):
                list(graph_stream.iter_array(path))

    def test_a_truncated_array_raises_when_gzipped_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cut.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write('{"nodes": [{"id": "a"}')
            with self.assertRaises(graph_stream.TruncatedJson):
                list(graph_stream.iter_array(path))

    def test_a_complete_file_does_not_raise(self):
        """The sensitivity check on the two above: if a well-formed file also raised,
        they would pass while saying nothing about truncation."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp), {"nodes": [{"id": "a"}], "links": []})
            self.assertEqual(list(graph_stream.iter_array(path)), [{"id": "a"}])

    def test_it_is_a_value_error(self):
        """Callers already wrap file reads in `except ValueError`; this must not
        escape them as something unfamiliar."""
        self.assertTrue(issubclass(graph_stream.TruncatedJson, ValueError))


class ThisSuiteCanStillTell(unittest.TestCase):
    """Break the streamer, confirm the comparison notices, restore - in this run.

    A gate that can only pass or fail cannot report that it has become vacuous,
    and the way it goes vacuous is usually an improvement elsewhere. So the
    equivalence assertions above are checked for discriminating power here rather
    than trusted: each mutation is applied, the comparison must fail, and the
    original is restored in `finally`.
    """

    GRAPH = {"nodes": [{"id": f"n{i}", "community": i % 7} for i in range(50)]}

    def _comparison_fails_with(self, broken) -> None:
        original = graph_stream.iter_array
        graph_stream.iter_array = broken
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = write(Path(tmp), self.GRAPH)
                # Streamed outside the assertRaises so that only the comparison can
                # throw inside it. With both in there, a broken streamer that raised
                # would satisfy the test without the comparison ever running - the
                # assertion would be about the wrong failure.
                streamed = list(graph_stream.iter_array(path))
                with self.assertRaises(AssertionError):
                    self.assertEqual(streamed, self.GRAPH["nodes"])
        finally:
            graph_stream.iter_array = original

    def test_it_notices_a_streamer_that_drops_objects(self):
        original = graph_stream.iter_array
        self._comparison_fails_with(
            lambda path, key="nodes": itertools.islice(original(path, key), 10)
        )

    def test_it_notices_a_streamer_that_yields_nothing(self):
        self._comparison_fails_with(lambda path, key="nodes": iter(()))

    def test_it_notices_a_streamer_that_alters_objects(self):
        original = graph_stream.iter_array
        self._comparison_fails_with(
            lambda path, key="nodes": ({**n, "community": 0} for n in original(path, key))
        )

    def test_and_the_unbroken_streamer_passes_the_same_comparison(self):
        """Without this the three above would pass against a streamer that always
        failed, which is the same vacuity one level up."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp), self.GRAPH)
            self.assertEqual(list(graph_stream.iter_array(path)), self.GRAPH["nodes"])


if __name__ == "__main__":
    unittest.main()
