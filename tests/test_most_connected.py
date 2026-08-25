"""What dominates the graph must be visible to a person (#112, hazard 1).

After the first build of one estate the most central entities were `c()`, `push()`,
`s()` and `a()` — minified helpers from two committed dependency bundles, which
supplied 36% of the AST nodes and 60% of the AST edges and formed the two largest
communities. Centrality and community detection are both degree-driven, so a dense
blob of interlinked vendored helpers wins every ranking, and topics, summaries and
the explorer are all generated downstream of clusters.

Nothing upstream catches it: graphify's `detect` honours `.gitignore`, and a
zero-install dependency bundle is deliberately committed, so it is ignored nowhere.

This reports rather than decides, and the tests are written to keep it that way.
Size is not the signal — `values.schema.json` and `variables.tf` are high
node-count and are real declarations of an estate's own surface — so there is
deliberately no test asserting that anything is excluded. What is asserted is that
a person is shown the ten names they need to make that judgement.
"""

from __future__ import annotations

import contextlib
import io as io_module
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import graph_files  # noqa: E402
from knowledgestore import status  # noqa: E402


class MostConnectedTest(unittest.TestCase):
    def _graph(self, payload: dict) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "graph.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_it_ranks_by_degree_most_connected_first(self):
        """Breaks if the ranking is by anything other than connectivity.

        Degree is what centrality and community detection are driven by, so degree
        is what has to be shown — a ranking by node count or by label would not
        surface the case this exists for.
        """
        path = self._graph(
            {
                "nodes": [{"id": "hub", "label": "c()"}, {"id": "leaf", "label": "Real"}],
                "links": [
                    {"source": "hub", "target": "leaf"},
                    {"source": "hub", "target": "leaf"},
                ],
            }
        )

        ranked = graph_files.most_connected(path, top=2)

        self.assertEqual([label for _id, label, _d in ranked], ["c()", "Real"])
        self.assertEqual(ranked[0][2], 2)

    def test_equal_degrees_break_ties_by_id(self):
        """Breaks if two equally connected nodes can swap between runs.

        Stage outputs are committed artefacts and two runs on the same inputs must
        be byte-identical. Anything iterating a dict keyed by unordered data needs
        an explicit tiebreak, and hash randomisation across processes has broken
        that here before, invisibly until someone diffed two builds.
        """
        path = self._graph(
            {
                "nodes": [{"id": "b", "label": "B"}, {"id": "a", "label": "A"}],
                "links": [{"source": "a", "target": "b"}],
            }
        )

        first = graph_files.most_connected(path, top=2)
        second = graph_files.most_connected(path, top=2)

        self.assertEqual(first, second)
        self.assertEqual([node_id for node_id, _l, _d in first], ["a", "b"])

    def test_it_reads_edges_as_well_as_links(self):
        """Breaks if a graph written with the other edge key ranks nothing.

        graphify writes `links` in node-link JSON and `edges` in its extract files.
        Reading one key and finding none is indistinguishable from a graph that has
        no edges, and the report would be silently empty on half the artefacts it
        is pointed at.
        """
        path = self._graph(
            {
                "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                "edges": [{"source": "a", "target": "b"}],
            }
        )

        self.assertEqual(len(graph_files.most_connected(path, top=2)), 2)

    def test_a_graph_with_no_edges_ranks_nothing(self):
        """Breaks if an edgeless graph produces a ranking, which would be a
        statement about connectivity derived from no connectivity."""
        path = self._graph({"nodes": [{"id": "a", "label": "A"}], "links": []})
        self.assertEqual(graph_files.most_connected(path), [])

    def test_a_label_less_node_falls_back_to_its_id(self):
        """Breaks if the report drops nodes with no label.

        Newer graphify emits package-hierarchy nodes with neither `label` nor
        `source_file`, and one of those dominating the graph is exactly the kind of
        thing an operator needs to see rather than have hidden.
        """
        path = self._graph(
            {
                "nodes": [{"id": "pkg"}, {"id": "b", "label": "B"}],
                "links": [{"source": "pkg", "target": "b"}],
            }
        )

        ranked = graph_files.most_connected(path, top=1)

        self.assertEqual(ranked[0][0], "b" if ranked[0][1] == "B" else "pkg")
        self.assertEqual(len(ranked), 1)


class StatusCentralTest(SettingsIsolated):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "graphify-out").mkdir(parents=True)
        self._old_root = config.ROOT
        config.configure(root=str(self.root))
        config.GRAPH_PATH.write_text(
            json.dumps(
                {
                    "nodes": [{"id": "hub", "label": "c()"}, {"id": "leaf", "label": "Real"}],
                    "links": [{"source": "hub", "target": "leaf"}],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        config.configure(root=str(self._old_root))
        self._tmp.cleanup()
        super().tearDown()

    def _run(self, argv):
        out = io_module.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            with contextlib.suppress(Exception):
                status.main(argv)
        return out.getvalue()

    def test_the_flag_reports_the_ranking(self):
        """Breaks if the check is written and never reaches `main`.

        Reporting through the function while nothing drives the CLI is the most
        repeated escape in this repository's mutation gate — four existing entries
        are exactly that, three of them in this module.
        """
        output = self._run(["--central"])

        self.assertIn("Most connected", output)
        self.assertIn("c()", output)

    def test_without_the_flag_it_stays_silent(self):
        """Breaks if the streamed pass runs on every `status`.

        `status` is the cheap stage an operator runs constantly and it never loads
        the graph. A ranking that cost a full pass on every invocation would be
        turned off rather than kept.
        """
        output = self._run([])

        self.assertNotIn("Most connected", output)


if __name__ == "__main__":
    unittest.main()
