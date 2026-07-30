"""Deep-dive evidence bundles and dossier merging."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from knowledgestore import build_deep_dives as dives


def node(nid, repo, label, community, source_file=None, kind=None):
    return {
        "id": nid,
        "repo": repo,
        "label": label,
        "community": community,
        "source_file": source_file,
        "metadata": {"kind": kind} if kind else {},
    }


GRAPH = {
    "nodes": [
        node("t::a", "target", "CaseAggregate", 1, "src/CaseAggregate.java"),
        node("t::b", "target", "HearingAggregate", 1, "src/HearingAggregate.java"),
        node("t::c", "target", "progression.case.json", 2, "raml/progression.case.json"),
        node("o::c", "other", "progression.case.json", 7, "schema/progression.case.json"),
        node("o::x", "other", "Unrelated", 8, "src/Unrelated.java"),
        node(
            "e::f", "e2e", "Progress a case", 9, "features/progress.feature", kind="gherkin_feature"
        ),
    ],
    "links": [
        {"source": "t::a", "target": "t::b"},
        {"source": "t::a", "target": "t::c"},
    ],
}
GRAPH["nodes"][5]["metadata"]["tickets"] = ["DD-1"]

LABELS = {"1": "Case handling", "2": "Case schema"}
SUMMARIES = {"1": "The case handling cluster."}
INTENT_FILES = {
    "src/CaseAggregate.java": {
        "tickets": {"DD-1": 3, "DD-2": 1, "DD-3": 1},
        "first": "2020-01-01",
        "last": "2026-07-01",
    },
    "src/HearingAggregate.java": {
        "tickets": {"DD-1": 1, "DD-2": 2},
        "first": "2021-01-01",
        "last": "2026-06-01",
    },
    "pom.xml": {"tickets": {"DD-3": 1}, "first": "2020-01-01", "last": "2020-02-01"},
}
DESCRIPTIONS = {
    "DD-1": {"d": ["Fix defect in case progression"], "first": "2020-03-04"},
    "DD-2": {"d": ["Revert hearing change"], "first": "2021-05-06"},
    "DD-3": {"d": ["Add feature toggles"], "first": "2020-07-08"},
}


class ScaleTest(unittest.TestCase):
    def test_counts_nodes_communities_and_summarised_top(self):
        got = dives.scale_section(GRAPH, "target", LABELS, SUMMARIES)
        self.assertEqual(got["nodes"], 3)
        self.assertAlmostEqual(got["share"], 3 / 6)
        self.assertEqual(got["communities"], 2)
        top = got["top_communities"][0]
        self.assertEqual((top["id"], top["label"], top["size"]), (1, "Case handling", 2))
        self.assertEqual(top["summary"], "The case handling cluster.")
        self.assertIsNone(got["top_communities"][1]["summary"])


class ChurnTest(unittest.TestCase):
    def test_orders_files_by_distinct_tickets(self):
        got = dives.churn_section(INTENT_FILES)
        self.assertEqual(got["files_with_history"], 3)
        self.assertEqual(got["top_files"][0]["path"], "src/CaseAggregate.java")
        self.assertEqual(got["top_files"][0]["tickets"], 3)


class InstabilityTest(unittest.TestCase):
    def test_measures_revert_and_fix_shares_with_samples(self):
        tickets = {"DD-1", "DD-2", "DD-3"}
        got = dives.instability_section(tickets, DESCRIPTIONS)
        self.assertEqual(got["tickets"], 3)
        self.assertAlmostEqual(got["revert_share"], 1 / 3)
        self.assertAlmostEqual(got["fix_share"], 1 / 3)
        self.assertEqual(got["sample_reverts"], ["DD-2: Revert hearing change"])

    def test_timeline_buckets_by_first_seen_year(self):
        got = dives.timeline_section({"DD-1", "DD-2", "DD-3"}, DESCRIPTIONS)
        self.assertEqual(got, {"2020": 2, "2021": 1})


class RepoTicketsTest(unittest.TestCase):
    def test_union_of_file_tickets(self):
        self.assertEqual(dives.repo_tickets(INTENT_FILES), {"DD-1", "DD-2", "DD-3"})


class CochangeTest(unittest.TestCase):
    def _files(self, pairs_count):
        # DD-n tickets each touching both files -> co-change support
        tickets = {f"DD-{i}": 1 for i in range(pairs_count)}
        return {
            "src/A.java": {"tickets": dict(tickets)},
            "src/B.java": {"tickets": dict(tickets)},
            "src/ATest.java": {"tickets": dict(tickets)},
        }

    def test_pairs_meet_threshold_and_test_pairs_are_excluded(self):
        self.addCleanup(setattr, dives, "MIN_COCHANGE", dives.MIN_COCHANGE)
        dives.MIN_COCHANGE = 10
        got = dives.cochange_section(self._files(12))
        self.assertIn({"a": "src/A.java", "b": "src/B.java", "n": 12}, got)
        pairs = {(p["a"], p["b"]) for p in got}
        self.assertNotIn(("src/A.java", "src/ATest.java"), pairs)
        self.assertIn(("src/A.java", "src/B.java"), pairs)

    def test_sweeping_tickets_are_ignored(self):
        files = {f"f{i}.java": {"tickets": {"BIG-1": 1}} for i in range(60)}
        self.assertEqual(dives.cochange_section(files), [])


class CouplingSurfaceTest(unittest.TestCase):
    def test_shared_schema_labels_name_the_other_repos(self):
        got = dives.coupling_surface(GRAPH, "target")
        self.assertEqual(got, [{"label": "progression.case.json", "other_repos": ["other"]}])


class FeatureSectionTest(unittest.TestCase):
    def test_features_sharing_tickets_are_linked(self):
        got = dives.feature_section(GRAPH, {"DD-1", "DD-9"})
        self.assertEqual(got, [{"label": "Progress a case", "tickets": ["DD-1"]}])


class HotspotTest(unittest.TestCase):
    def test_high_churn_high_degree_files_flagged(self):
        got = dives.hotspot_section(INTENT_FILES, GRAPH, "target")
        paths = [h["path"] for h in got]
        self.assertIn("src/CaseAggregate.java", paths)  # churn 3, degree 2
        self.assertNotIn("pom.xml", paths)  # churn 1, no nodes


class ExtractTest(unittest.TestCase):
    def test_extract_writes_a_complete_bundle(self):
        import gzip
        import json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for attr, rel in {
                "GRAPH_PATH": "graph.json",
                "LABELS_PATH": "labels.json",
                "INTENT_PATH": "intent.json.gz",
                "DESCRIPTIONS_PATH": "desc.json.gz",
                "SUMMARIES_PATH": "summaries.json",
                "INPUT_DIR": "deep-dives",
            }.items():
                self.addCleanup(setattr, dives, attr, getattr(dives, attr))
                setattr(dives, attr, root / rel)
            from knowledgestore import provenance

            self.addCleanup(setattr, provenance, "PROVENANCE_PATH", provenance.PROVENANCE_PATH)
            provenance.PROVENANCE_PATH = root / "provenance.json"
            (root / "graph.json").write_text(json.dumps(GRAPH))
            (root / "labels.json").write_text(json.dumps(LABELS))
            (root / "summaries.json").write_text(json.dumps(SUMMARIES))
            with gzip.open(root / "intent.json.gz", "wt") as f:
                json.dump({"target": INTENT_FILES}, f)
            with gzip.open(root / "desc.json.gz", "wt") as f:
                json.dump(DESCRIPTIONS, f)
            self.assertEqual(dives.extract("target"), 0)
            bundle = json.loads((root / "deep-dives" / "target-input.json").read_text())
        for key in (
            "repo",
            "provenance",
            "scale",
            "churn",
            "instability",
            "timeline",
            "cochange",
            "hotspots",
            "coupling_surface",
            "features",
            "summary_coverage",
        ):
            self.assertIn(key, bundle)
        self.assertEqual(bundle["repo"], "target")

    def test_extract_unknown_repo_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            import json

            root = Path(tmp)
            self.addCleanup(setattr, dives, "GRAPH_PATH", dives.GRAPH_PATH)
            dives.GRAPH_PATH = root / "graph.json"
            (root / "graph.json").write_text(json.dumps({"nodes": [], "links": []}))
            self.assertEqual(dives.extract("nope"), 1)


if __name__ == "__main__":
    unittest.main()
