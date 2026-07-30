"""Deep-dive evidence bundles and dossier merging."""

from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
