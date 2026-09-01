"""Tests for knowledgestore/import_ticket_titles.py and knowledgestore/build_community_summaries.py."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path


from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import io as store_io  # noqa: E402
from knowledgestore import import_ticket_titles as titles  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402


class FindColumnsTest(SettingsIsolated):
    def test_finds_jira_export_headers(self):
        self.assertEqual(titles.find_columns(["Issue key", "Summary", "Status"]), (0, 1))
        self.assertEqual(titles.find_columns(["Status", "Key", "Summary"]), (1, 2))

    def test_rejects_missing_columns(self):
        with self.assertRaises(ValueError):
            titles.find_columns(["Status", "Assignee"])


class MergeCsvTest(SettingsIsolated):
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


class SummariesMergeTest(SettingsIsolated):
    def _with_paths(self, tmp):
        config.configure(SUMMARIES_INPUT_PATH=Path(tmp) / "communities-input.json")
        config.configure(SUMMARIES_PATH=Path(tmp) / "communities.json")
        config.SUMMARIES_INPUT_PATH.write_text(
            json.dumps([{"id": 3, "label": "Hearing State Store", "size": 100}]), encoding="utf-8"
        )

    def test_valid_summary_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._with_paths(tmp)
            batch = Path(tmp) / "gen.json"
            batch.write_text(
                json.dumps(
                    {
                        "3": "The NgRx state store for hearing results, managing draft "
                        "results as they are built during a hearing."
                    }
                ),
                encoding="utf-8",
            )
            code = summaries.merge([str(batch)])
            merged = json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertIn("3", merged)

    def test_summaries_kept_on_now_insignificant_clusters_do_not_go_negative(self):
        """After a re-cluster, communities.json can legitimately hold summaries
        for clusters that fell below the significance threshold. The coverage
        arithmetic used to be len(known) - len(merged), which printed
        "-54 significant communities still lack a summary" - a negative count
        that reads as a defect. Coverage is a set difference, not a size
        difference."""
        from contextlib import redirect_stdout
        from io import StringIO

        with tempfile.TemporaryDirectory() as tmp:
            self._with_paths(tmp)
            # pre-existing summary for a cluster the digests no longer contain
            config.SUMMARIES_PATH.write_text(
                json.dumps({"7": "Prose for a cluster now below the threshold. " * 3}),
                encoding="utf-8",
            )
            batch = Path(tmp) / "gen.json"
            batch.write_text(
                json.dumps(
                    {
                        "3": "A valid summary for the one significant community, "
                        "long enough to clear the lower bound."
                    }
                ),
                encoding="utf-8",
            )
            out = StringIO()
            with redirect_stdout(out):
                code = summaries.merge([str(batch)])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertNotIn("-1 significant", text, "coverage must never be negative")
        self.assertIn("1 summaries cover clusters now below the significance", text)

    def test_unknown_id_and_bad_length_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._with_paths(tmp)
            batch = Path(tmp) / "gen.json"
            batch.write_text(json.dumps({"999": "x" * 100, "3": "too short"}), encoding="utf-8")
            code = summaries.merge([str(batch)])
            # the prose alone: the artefact also carries a metadata block, which is
            # written whether or not any summary was accepted
            merged = store_io.read_summaries(config.SUMMARIES_PATH)
        self.assertEqual(code, 1)
        self.assertEqual(merged, {})


class TicketDescriptionsOutputTest(SettingsIsolated):
    """The committed ticket-descriptions artefact keeps its contract."""

    def test_shape_of_committed_artifact(self):
        path = (
            Path(__file__).resolve().parent.parent
            / "knowledge"
            / "intent"
            / "ticket-descriptions.json.gz"
        )
        if not path.exists():
            self.skipTest("artefact not built")
        data = json.load(gzip.open(path, "rt", encoding="utf-8"))
        self.assertGreater(len(data), 1000)
        sample = next(iter(data.values()))
        # `s` and `b` are present only where the commits offered that evidence.
        self.assertEqual(set(sample.keys()) - {"s", "b"}, {"d", "first", "last", "repos", "n"})


class CommunityDigestTest(SettingsIsolated):
    def test_digest_gathers_repos_features_and_tickets(self):
        nodes = [
            {
                "id": "a",
                "label": "BigHub",
                "repo": "repo-a",
                "source_file": "src/hub.ts",
                "metadata": {},
            },
            {
                "id": "b",
                "label": "Amend address",
                "repo": "repo-a",
                "source_file": "features/a.feature",
                "metadata": {"kind": "gherkin_feature", "tickets": ["DD-1"]},
            },
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


class SummariesExtractTest(SettingsIsolated):
    def test_extract_filters_small_communities(self):
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config.configure(GRAPH_PATH=root / "graph.json")
            config.configure(LABELS_PATH=root / "labels.json")
            config.configure(INTENT_INDEX_PATH=root / "missing.json.gz")
            config.configure(SUMMARIES_INPUT_PATH=root / "communities-input.json")
            big = [
                {
                    "id": f"n{i}",
                    "label": f"L{i}",
                    "community": 1,
                    "repo": "repo-a",
                    "source_file": "x.ts",
                    "metadata": {},
                }
                for i in range(config.MIN_COMMUNITY_SIZE)
            ]
            small = [
                {
                    "id": "s",
                    "label": "S",
                    "community": 2,
                    "repo": "repo-a",
                    "source_file": "y.ts",
                    "metadata": {},
                }
            ]
            config.GRAPH_PATH.write_text(
                _json.dumps({"nodes": big + small, "links": []}), encoding="utf-8"
            )
            config.LABELS_PATH.write_text(_json.dumps({"1": "Big Area"}), encoding="utf-8")
            code = summaries.extract()
            digests = _json.loads(config.SUMMARIES_INPUT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual([d["id"] for d in digests], [1])
        self.assertEqual(digests[0]["label"], "Big Area")


if __name__ == "__main__":
    unittest.main()
