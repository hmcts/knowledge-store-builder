"""A graph with no `repo` attribute must say so, not blame the estate.

Every per-repository feature keys on `n["repo"]`. A graph built without it does
not fail: `deepdive extract <repo>` reports "No nodes for repository X - is it in
the estate?", which sends an operator to check a manifest that is perfectly
correct, and `summaries extract` produces well-formed digests whose
`repositories` and `tickets` are simply empty — reading as a thin estate rather
than a broken precondition (issue #104).

The reporter on that estate spent time on the manifest before finding the real
cause. These tests are about which of two very different causes the message
names.
"""

from __future__ import annotations

import io as _io
import contextlib
import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import build_deep_dives as dives  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402


def _graph(nodes):
    return {"nodes": nodes, "links": []}


class DeepDiveDiagnosisTest(SettingsIsolated):
    def _run(self, nodes, repo="wanted"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "graph.json"
            path.write_text(json.dumps(_graph(nodes)), encoding="utf-8")
            config.configure(GRAPH_PATH=path)
            err = _io.StringIO()
            with contextlib.redirect_stderr(err):
                code = dives.extract(repo)
            return code, err.getvalue()

    def test_a_graph_without_the_attribute_says_so(self):
        code, message = self._run([{"id": "a", "label": "X"}, {"id": "b", "label": "Y"}])
        self.assertEqual(code, 1)
        self.assertIn("carries a `repo` attribute", message)
        self.assertIn("not specific to", message, "it must say the problem is estate-wide")

    def test_it_does_not_blame_the_estate_when_the_attribute_is_absent(self):
        _, message = self._run([{"id": "a", "label": "X"}])
        self.assertNotIn(
            "is it in the estate",
            message,
            "this sends an operator to check a manifest that is correct",
        )

    def test_a_genuinely_absent_repository_still_gets_the_original_message(self):
        """The other cause must keep its own diagnosis."""
        code, message = self._run([{"id": "a", "label": "X", "repo": "something-else"}])
        self.assertEqual(code, 1)
        self.assertIn("is it in the estate", message)
        self.assertNotIn("carries a `repo` attribute", message)


class SummariesWarningTest(SettingsIsolated):
    def test_extract_warns_once_when_no_node_carries_a_repository(self):
        nodes = [{"id": str(i), "label": f"N{i}", "community": 1} for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "graph.json"
            graph.write_text(json.dumps(_graph(nodes)), encoding="utf-8")
            config.configure(
                GRAPH_PATH=graph,
                LABELS_PATH=root / "labels.json",
                INTENT_INDEX_PATH=root / "intent.json.gz",
                SUMMARIES_INPUT_PATH=root / "in.json",
            )
            err = _io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(_io.StringIO()):
                summaries.extract()
            self.assertIn("carries a `repo` attribute", err.getvalue())
            self.assertIn(
                "guesswork", err.getvalue(), "it must say the summaries cannot be trusted"
            )

    def test_a_healthy_graph_is_not_warned_about(self):
        nodes = [{"id": "a", "label": "N", "community": 1, "repo": "svc"}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "graph.json"
            graph.write_text(json.dumps(_graph(nodes)), encoding="utf-8")
            config.configure(
                GRAPH_PATH=graph,
                LABELS_PATH=root / "labels.json",
                INTENT_INDEX_PATH=root / "intent.json.gz",
                SUMMARIES_INPUT_PATH=root / "in.json",
            )
            err = _io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(_io.StringIO()):
                summaries.extract()
            self.assertNotIn("carries a `repo` attribute", err.getvalue())


if __name__ == "__main__":
    unittest.main()
