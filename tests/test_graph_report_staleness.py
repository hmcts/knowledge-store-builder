"""GRAPH_REPORT.md must not be quoted as fact once the graph has moved on.

The query skill deliberately carries no counts, so figures cannot rot inside it,
and routes every "how big is the graph" question to this file. That makes the
file the single point where the anti-stale-numbers design can fail - and on a
real store it did: the report claimed 809,441 nodes beside a graph holding
779,551, a 29,890 disagreement in the same directory with nothing reporting it.

Worse than a README nobody re-derived, because the indirection means the reader
believes they are checking the source.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import status  # noqa: E402

SUMMARY = "- 809441 nodes · 1794775 edges · 27887 communities (24655 shown)\n"


class GraphReportTest(SettingsIsolated):
    def _store(self, report: str | None, report_at: str, graph_at: str, nodes: int = 3) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "graphify-out").mkdir(parents=True)
        if report is not None:
            (root / "graphify-out" / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
        graph = root / "graphify-out" / "graph.json"
        graph.write_text(json.dumps({"nodes": [{"id": str(i)} for i in range(nodes)]}), "utf-8")
        config.configure(
            ROOT=root,
            GRAPH_PATH=graph,
            GRAPH_REPORT_PATH=root / "graphify-out" / "GRAPH_REPORT.md",
        )
        # Commit dates are the cheap staleness signal; stub git rather than
        # building a repository, since the dates are the whole input.
        dates = {"graphify-out/GRAPH_REPORT.md": report_at, "graphify-out/graph.json": graph_at}
        self.addCleanup(setattr, status, "run_git", status.run_git)
        status.run_git = lambda args: dates.get(args[-1], "")
        return root

    def _run(self, verify: bool = False) -> str:
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            status._report_graph_report(verify)
        return out.getvalue()

    def test_a_report_older_than_the_graph_is_called_out(self):
        self._store(SUMMARY, "2026-07-31T00:00:00+00:00", "2026-08-11T00:00:00+00:00")
        text = self._run()
        self.assertIn("older than the graph", text)
        self.assertIn("809,441", text)
        self.assertIn(
            "authoritative",
            text,
            "the cost is that a skill quotes it as fact - say so, or it reads as tidiness",
        )

    def test_a_current_report_is_not_nagged_about(self):
        self._store(SUMMARY, "2026-08-11T00:00:00+00:00", "2026-08-11T00:00:00+00:00")
        self.assertEqual(self._run(), "")

    def test_a_missing_report_is_silent(self):
        self._store(None, "", "")
        self.assertEqual(self._run(), "")

    def test_a_report_without_a_summary_line_still_reports_staleness(self):
        """Absence of parsable counts is not absence of the problem."""
        self._store(
            "# Graph Report\n\nno summary here\n",
            "2026-07-01T00:00:00+00:00",
            "2026-08-11T00:00:00+00:00",
        )
        text = self._run()
        self.assertIn("older than the graph", text)

    def test_verify_names_the_disagreement(self):
        self._store(SUMMARY, "2026-07-31T00:00:00+00:00", "2026-08-11T00:00:00+00:00", nodes=3)
        text = self._run(verify=True)
        self.assertIn("809,441", text)
        self.assertIn("the graph has 3", text)
        self.assertIn("809,438", text, "the size of the gap is the actionable part")

    def test_verify_confirms_agreement_rather_than_staying_quiet(self):
        """Silence would be indistinguishable from a check that did not run."""
        self._store(
            "- 3 nodes · 1 edges\n",
            "2026-08-11T00:00:00+00:00",
            "2026-08-11T00:00:00+00:00",
            nodes=3,
        )
        self.assertIn("agrees with the graph", self._run(verify=True))

    def test_the_exact_comparison_is_opt_in(self):
        """It loads the whole graph; this stage must stay cheap by default."""
        self._store(SUMMARY, "2026-08-11T00:00:00+00:00", "2026-08-11T00:00:00+00:00")
        self.assertEqual(self._run(verify=False), "")


class WiredIntoStatusTest(SettingsIsolated):
    """The check must be reachable from the command anyone actually runs.

    Every unit test above calls `_report_graph_report` directly, so all of them
    pass with the call removed from `main()`. That is the same gap that shipped
    a KeyError in the symlink reporter: the behaviour was tested, the wiring was
    not, and only the end-to-end path exercises both.
    """

    def _store(self, report_at: str, graph_at: str, nodes: int = 3) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "graphify-out").mkdir(parents=True)
        (root / "graphify-out" / "GRAPH_REPORT.md").write_text(SUMMARY, encoding="utf-8")
        graph = root / "graphify-out" / "graph.json"
        graph.write_text(json.dumps({"nodes": [{"id": str(i)} for i in range(nodes)]}), "utf-8")
        config.configure(
            ROOT=root,
            GRAPH_PATH=graph,
            GRAPH_REPORT_PATH=root / "graphify-out" / "GRAPH_REPORT.md",
            PROVENANCE_PATH=root / "provenance.json",
            SUMMARIES_PATH=root / "summaries.json",
            SUMMARIES_INPUT_PATH=root / "digests.json",
            TOPICS_BRIEFS_PATH=root / "briefs.json",
            TOPICS_CONFIG_PATH=root / "topics.txt",
            INTENT_INDEX_PATH=root / "intent.json.gz",
            REPOSITORIES_DIR=root / "repositories",
            REPOSITORIES_CONFIG=root / "repositories.txt",
            EXTERNAL_CONFIG=root / "external.txt",
        )
        dates = {"graphify-out/GRAPH_REPORT.md": report_at, "graphify-out/graph.json": graph_at}
        self.addCleanup(setattr, status, "run_git", status.run_git)
        status.run_git = lambda args: dates.get(args[-1], "")

    def _main(self, argv: list) -> str:
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()):
            status.main(argv)
        return out.getvalue()

    def test_status_reports_a_stale_report(self):
        self._store("2026-07-31T00:00:00+00:00", "2026-08-11T00:00:00+00:00")
        self.assertIn("older than the graph", self._main([]))

    def test_the_verify_graph_flag_is_accepted_and_reaches_the_check(self):
        """Also pins the flag's name: a renamed argument would fail here."""
        self._store("2026-08-11T00:00:00+00:00", "2026-08-11T00:00:00+00:00")
        self.assertIn("the graph has 3", self._main(["--verify-graph"]))

    def test_verify_graph_also_reports_contentless_nodes(self):
        """Wiring, not behaviour. Unwiring the contentless report from
        `--verify-graph` failed no test until this one existed - the sixth time
        in two days that a check's behaviour was covered and its call site was
        not."""
        self._store("2026-08-11T00:00:00+00:00", "2026-08-11T00:00:00+00:00")
        self.assertIn("Contentless nodes", self._main(["--verify-graph"]))

    def test_the_default_run_reports_no_contentless_nodes(self):
        """It loads the graph to count them, so it must stay behind the flag."""
        self._store("2026-08-11T00:00:00+00:00", "2026-08-11T00:00:00+00:00")
        self.assertNotIn("Contentless nodes", self._main([]))

    def test_the_default_run_does_not_load_the_graph(self):
        self._store("2026-08-11T00:00:00+00:00", "2026-08-11T00:00:00+00:00")
        self.assertNotIn("the graph has", self._main([]))


class ParsingTest(SettingsIsolated):
    def _claims(self, report: str) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "graphify-out").mkdir(parents=True)
        (root / "graphify-out" / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
        (root / "graphify-out" / "graph.json").write_text("{}", encoding="utf-8")
        config.configure(
            ROOT=root,
            GRAPH_PATH=root / "graphify-out" / "graph.json",
            GRAPH_REPORT_PATH=root / "graphify-out" / "GRAPH_REPORT.md",
        )
        self.addCleanup(setattr, status, "run_git", status.run_git)
        status.run_git = lambda args: ""
        return status.graph_report_claims()

    def test_both_figures_are_parsed(self):
        claims = self._claims(SUMMARY)
        self.assertEqual((claims["nodes"], claims["edges"]), (809441, 1794775))

    def test_thousands_separators_are_parsed(self):
        """The report's format is graphify's to change, and the pattern allows
        them - so a comma must not silently truncate the figure to its first
        group."""
        claims = self._claims("- 809,441 nodes · 1,794,775 edges\n")
        self.assertEqual((claims["nodes"], claims["edges"]), (809441, 1794775))

    def test_no_git_history_is_not_reported_as_stale(self):
        """An uncommitted or non-git store has no dates to compare, which is not
        evidence that the report is old."""
        self.assertFalse(self._claims(SUMMARY)["stale"])

    def test_verify_on_an_unparsable_report_neither_crashes_nor_claims(self):
        """Without its guard this raises TypeError formatting None."""
        self._claims("# Graph Report\n\nno summary line\n")
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            status._report_graph_report(True)
        self.assertEqual(out.getvalue(), "")


class ContentlessNodesTest(unittest.TestCase):
    """Nodes that carry identity and nothing else.

    `merge-graphs` composes with networkx, which creates a node for any id an
    edge mentions, so an edge endpoint absent from its own per-repository graph
    arrives as identity alone. One estate carried 94,899 - 15% of its merged
    layer - and they pass every obvious guard: a dangling-endpoint check,
    because after the merge they really are nodes, and a repository filter,
    because the merge stamps `repo` on everything.
    """

    def test_identity_only_nodes_are_counted_by_repository(self):
        nodes = [
            {"id": "a::x", "local_id": "x", "repo": "a", "_origin": "ast"},
            {"id": "a::y", "local_id": "y", "repo": "a"},
            {"id": "b::z", "local_id": "z", "repo": "b"},
        ]
        self.assertEqual(status.contentless_nodes(nodes), {"a": 2, "b": 1})

    def test_a_node_with_a_label_but_no_source_file_is_not_contentless(self):
        """The loose rule reads well and is wrong. On the maintainer's own estate
        99,828 nodes lack a `source_file` while carrying a label, a normalised
        label and a file type, and every one is legitimate - a check reporting
        those would be dismissed the first time it ran."""
        nodes = [
            {"id": "a::any", "repo": "a", "label": "any", "norm_label": "any", "file_type": "code"}
        ]
        self.assertEqual(status.contentless_nodes(nodes), {})

    def test_a_node_with_only_a_source_file_is_not_contentless(self):
        nodes = [{"id": "a::f", "repo": "a", "source_file": "src/f.py"}]
        self.assertEqual(status.contentless_nodes(nodes), {})

    def test_community_membership_alone_does_not_rescue_a_node(self):
        """Clustering assigns a community to everything, phantoms included, so
        carrying one is not evidence of content - it is evidence that clustering
        ran."""
        nodes = [
            {
                "id": "a::x",
                "repo": "a",
                "local_id": "x",
                "community": 7,
                "community_name": "some cluster",
            }
        ]
        self.assertEqual(status.contentless_nodes(nodes), {"a": 1})

    def test_an_empty_node_is_not_counted(self):
        self.assertEqual(status.contentless_nodes([{}]), {})

    def test_a_clean_graph_says_so_rather_than_going_quiet(self):
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            status._report_contentless([{"id": "a", "label": "Thing", "repo": "a"}])
        self.assertIn("none of 1", out.getvalue())

    def test_the_report_explains_why_nothing_else_caught_them(self):
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            status._report_contentless([{"id": "a::x", "repo": "a", "local_id": "x"}])
        text = out.getvalue()
        self.assertIn("dangling-endpoint", text)
        self.assertIn("repository filter", text)


if __name__ == "__main__":
    unittest.main()
