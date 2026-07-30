"""Tests for knowledgestore/import_ticket_titles.py and knowledgestore/build_community_summaries.py."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path


from knowledgestore import import_ticket_titles as titles  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402


class FindColumnsTest(unittest.TestCase):
    def test_finds_jira_export_headers(self):
        self.assertEqual(titles.find_columns(["Issue key", "Summary", "Status"]), (0, 1))
        self.assertEqual(titles.find_columns(["Status", "Key", "Summary"]), (1, 2))

    def test_rejects_missing_columns(self):
        with self.assertRaises(ValueError):
            titles.find_columns(["Status", "Assignee"])


class MergeCsvTest(unittest.TestCase):
    def test_merges_valid_rows_and_skips_junk(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "export.csv"
            csv_path.write_text(
                "Issue key,Summary\n"
                "CRC-12016,Address pipe production incident\n"
                "not-a-ticket,ignored\n"
                "DD-1,\n",  # empty summary - skipped
                encoding="utf-8",
            )
            store = {}
            added = titles.merge_csv(csv_path, store)
        self.assertEqual(added, 1)
        self.assertEqual(store, {"CRC-12016": "Address pipe production incident"})


class SummariesMergeTest(unittest.TestCase):
    def _with_paths(self, tmp):
        summaries.INPUT_PATH = Path(tmp) / "communities-input.json"
        summaries.OUTPUT_PATH = Path(tmp) / "communities.json"
        summaries.INPUT_PATH.write_text(json.dumps(
            [{"id": 3, "label": "Hearing State Store", "size": 100}]
        ), encoding="utf-8")

    def test_valid_summary_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._with_paths(tmp)
            batch = Path(tmp) / "gen.json"
            batch.write_text(json.dumps(
                {"3": "The NgRx state store for hearing results, managing draft "
                      "results as they are built during a hearing."}
            ), encoding="utf-8")
            code = summaries.merge([str(batch)])
            merged = json.loads(summaries.OUTPUT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertIn("3", merged)

    def test_unknown_id_and_bad_length_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._with_paths(tmp)
            batch = Path(tmp) / "gen.json"
            batch.write_text(json.dumps({"999": "x" * 100, "3": "too short"}),
                             encoding="utf-8")
            code = summaries.merge([str(batch)])
            merged = json.loads(summaries.OUTPUT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(merged, {})


class TicketDescriptionsOutputTest(unittest.TestCase):
    """The committed ticket-descriptions artefact keeps its contract."""

    def test_shape_of_committed_artifact(self):
        path = Path(__file__).resolve().parent.parent / "knowledge" / "intent" / "ticket-descriptions.json.gz"
        if not path.exists():
            self.skipTest("artefact not built")
        data = json.load(gzip.open(path, "rt", encoding="utf-8"))
        self.assertGreater(len(data), 1000)
        sample = next(iter(data.values()))
        self.assertEqual(set(sample.keys()), {"d", "first", "last", "repos", "n"})


class CommunityDigestTest(unittest.TestCase):
    def test_digest_gathers_repos_features_and_tickets(self):
        nodes = [
            {"id": "a", "label": "BigHub", "repo": "repo-a",
             "source_file": "src/hub.ts", "metadata": {}},
            {"id": "b", "label": "Amend address", "repo": "repo-a",
             "source_file": "features/a.feature",
             "metadata": {"kind": "gherkin_feature", "tickets": ["DD-1"]}},
        ]
        intent = {"repo-a": {"src/hub.ts": {"tickets": {"DD-2": 4}}}}
        degree = {"a": 10, "b": 2}
        digest = summaries.community_digest(7, nodes, {"7": "Area"}, intent, degree)
        self.assertEqual(digest["id"], 7)
        self.assertEqual(digest["label"], "Area")
        self.assertEqual(digest["repositories"], ["repo-a"])
        self.assertIn("BigHub (src/hub.ts)", digest["top_nodes"][0])
        self.assertEqual(digest["business_features"], ["Amend address"])
        self.assertCountEqual(digest["tickets"], ["DD-1", "DD-2"])


class SummariesExtractTest(unittest.TestCase):
    def test_extract_filters_small_communities(self):
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summaries.GRAPH_PATH = root / "graph.json"
            summaries.LABELS_PATH = root / "labels.json"
            summaries.INTENT_PATH = root / "missing.json.gz"
            summaries.INPUT_PATH = root / "communities-input.json"
            big = [{"id": f"n{i}", "label": f"L{i}", "community": 1,
                    "repo": "repo-a", "source_file": "x.ts", "metadata": {}}
                   for i in range(summaries.MIN_COMMUNITY_SIZE)]
            small = [{"id": "s", "label": "S", "community": 2,
                      "repo": "repo-a", "source_file": "y.ts", "metadata": {}}]
            summaries.GRAPH_PATH.write_text(_json.dumps(
                {"nodes": big + small, "links": []}), encoding="utf-8")
            summaries.LABELS_PATH.write_text(
                _json.dumps({"1": "Big Area"}), encoding="utf-8")
            code = summaries.extract()
            digests = _json.loads(summaries.INPUT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual([d["id"] for d in digests], [1])
        self.assertEqual(digests[0]["label"], "Big Area")


if __name__ == "__main__":
    unittest.main()
