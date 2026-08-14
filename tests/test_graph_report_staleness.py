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


if __name__ == "__main__":
    unittest.main()
