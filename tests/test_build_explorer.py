"""Tests for knowledgestore/build_explorer.py - the explorer's embedded index."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


from knowledgestore import build_explorer as explorer  # noqa: E402


def node(
    node_id, label, kind=None, community=1, repo="repo-a", source_file="src/a.ts", file_type="code"
):
    metadata = {"kind": kind} if kind else {}
    return {
        "id": node_id,
        "label": label,
        "repo": repo,
        "community": community,
        "source_file": source_file,
        "file_type": file_type,
        "metadata": metadata,
    }


class NodeKindTest(unittest.TestCase):
    def test_gherkin_and_ticket_kinds(self):
        self.assertEqual(explorer.node_kind(node("a", "F", kind="gherkin_feature")), "feature")
        self.assertEqual(explorer.node_kind(node("a", "S", kind="gherkin_scenario")), "scenario")
        self.assertEqual(explorer.node_kind(node("a", "T", kind="jira_ticket")), "ticket")

    def test_code_and_concept(self):
        self.assertEqual(explorer.node_kind(node("a", "X")), "code")
        concept = node("a", "X", file_type="concept")
        self.assertEqual(explorer.node_kind(concept), "concept")


class NoiseTest(unittest.TestCase):
    def test_minified_symbols_are_noise(self):
        for label in ("Zt()", "e", "$m()", "abc"):
            self.assertTrue(explorer.is_noise(node("a", label), "code"), label)

    def test_real_symbols_and_business_nodes_are_kept(self):
        self.assertFalse(explorer.is_noise(node("a", "AddressPipe"), "code"))
        self.assertFalse(explorer.is_noise(node("a", "e"), "feature"))


class BuildIndexTest(unittest.TestCase):
    def setUp(self):
        self._min_degree = explorer.MIN_ENTRY_DEGREE
        explorer.MIN_ENTRY_DEGREE = 0

    def tearDown(self):
        explorer.MIN_ENTRY_DEGREE = self._min_degree

    def test_entries_and_edges_reference_kept_nodes_only(self):
        graph = {
            "nodes": [
                node("n1", "AddressPipe"),
                node("n2", "AddressInputComponent"),
                node("n3", "Zt()"),  # noise - must be dropped
            ],
            "links": [
                {"source": "n1", "target": "n2"},
                {"source": "n1", "target": "n3"},
            ],
        }
        entries, edges = explorer.build_index(graph, {"1": "Address Handling"}, {})

        labels = [e[0] for e in entries]
        self.assertEqual(sorted(labels), ["AddressInputComponent", "AddressPipe"])
        # entry schema: community label + id present
        for entry in entries:
            self.assertEqual(entry[3], "Address Handling")
            self.assertEqual(entry[8], 1)
        # the noise edge disappears; the kept edge uses in-range indexes
        self.assertEqual(len(edges), 2)
        self.assertTrue(all(0 <= i < len(entries) for i in edges))

    def test_intent_tickets_attach_to_code_entries(self):
        graph = {"nodes": [node("n1", "AddressPipe")], "links": []}
        intent = {
            "repo-a": {
                "src/a.ts": {
                    "tickets": {"CRC-12016": 1},
                    "first": "2019-10-01",
                    "last": "2019-11-14",
                }
            }
        }
        entries, _ = explorer.build_index(graph, {}, intent)
        self.assertEqual(entries[0][7], ["CRC-12016"])


class NodeTicketsTest(unittest.TestCase):
    def test_feature_tickets_come_from_metadata(self):
        feature = node("a", "F", kind="gherkin_feature")
        feature["metadata"]["tickets"] = ["DD-1", "DD-2"]
        self.assertEqual(explorer.node_tickets(feature, "feature", {}), ["DD-1", "DD-2"])

    def test_ticket_node_is_its_own_ticket(self):
        ticket = node("a", "DD-9", kind="jira_ticket")
        self.assertEqual(explorer.node_tickets(ticket, "ticket", {}), ["DD-9"])

    def test_code_tickets_come_from_intent_index(self):
        intent = {"repo-a": {"src/a.ts": {"tickets": {"DD-1": 3, "DD-2": 1}}}}
        self.assertEqual(explorer.node_tickets(node("a", "X"), "code", intent), ["DD-1", "DD-2"])


class EntryConnectionsTest(unittest.TestCase):
    def test_orders_by_degree_dedupes_labels_and_caps(self):
        nodes = {f"n{i}": {"label": f"L{i}"} for i in range(8)}
        nodes["dup"] = {"label": "L1"}
        adjacency = {"root": set(list(nodes))}
        degree = {nid: i for i, nid in enumerate(sorted(nodes))}
        got = explorer.entry_connections("root", adjacency, degree, nodes)
        self.assertLessEqual(len(got), explorer.MAX_CONNECTIONS)
        self.assertEqual(len(got), len(set(got)))  # deduped


class KeptEdgesTest(unittest.TestCase):
    def test_emits_each_kept_edge_once_and_skips_dropped_nodes(self):
        kept = [("a", {}, "code"), ("b", {}, "code")]
        adjacency = {"a": {"b", "dropped"}, "b": {"a"}}
        index_of = {"a": 0, "b": 1}
        self.assertEqual(explorer.kept_edges(kept, adjacency, index_of), [0, 1])


class BuildPageSmokeTest(unittest.TestCase):
    """main() inlines app.js and all data blocks into one page."""

    def test_main_produces_page_with_all_blocks(self):
        self.addCleanup(setattr, explorer, "MIN_ENTRY_DEGREE", explorer.MIN_ENTRY_DEGREE)
        explorer.MIN_ENTRY_DEGREE = 0
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer.GRAPH_PATH = root / "graph.json"
            explorer.LABELS_PATH = root / "labels.json"
            explorer.INTENT_PATH = root / "missing-intent.json.gz"
            explorer.TITLES_PATH = root / "missing-titles.json.gz"
            explorer.SUMMARIES_PATH = root / "missing-summaries.json"
            explorer.SYNONYMS_PATH = root / "missing-syn.json.gz"
            explorer.TICKET_DESC_PATH = root / "missing-desc.json.gz"
            explorer.TOPICS_PATH = root / "missing-topics.json"
            explorer.OUTPUT = root / "explorer.html"
            explorer.PROVENANCE_PATH = root / "provenance.json"
            with open(explorer.PROVENANCE_PATH, "w") as pf:
                _json.dump(
                    {
                        "repositories": {
                            "r": {
                                "sha": "a" * 40,
                                "branch": "main",
                                "committed": "2026-07-30T09:14:02+01:00",
                            }
                        }
                    },
                    pf,
                )
            explorer.GRAPH_PATH.write_text(
                _json.dumps({"nodes": [node("n1", "AddressPipe")], "links": []}), encoding="utf-8"
            )
            explorer.LABELS_PATH.write_text("{}", encoding="utf-8")
            code = explorer.main()
            html = explorer.OUTPUT.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        for block in (
            "data",
            "edges",
            "titles",
            "summaries",
            "synonyms",
            "tickets",
            "config",
            "topics",
        ):
            self.assertIn(f'<script id="{block}"', html)
        self.assertIn("function runAsk", html)  # app.js inlined
        self.assertIn("sources synced to 2026-07-30", html)


class IncludeEntryPolicyTest(unittest.TestCase):
    def test_business_kinds_always_included(self):
        feature = node("a", "F", kind="gherkin_feature")
        self.assertTrue(explorer.include_entry(feature, "feature", 0))

    def test_labelless_structural_nodes_excluded(self):
        # Java package-hierarchy nodes from newer graphify carry no label
        pkg = {"id": "r::pkg_uk_gov_moj", "repo": "r", "file_type": "concept"}
        self.assertFalse(explorer.include_entry(pkg, "concept", 99))

    def test_methods_and_backend_tests_excluded(self):
        method = node("a", ".getThing()")
        self.assertFalse(explorer.include_entry(method, "code", 99))
        backend_test = node("a", "FooTest", source_file="src/test/java/FooTest.java")
        self.assertFalse(explorer.include_entry(backend_test, "code", 99))

    def test_e2e_test_artifacts_kept(self):
        # an estate names its E2E suites; their tests are documentation
        self.addCleanup(setattr, explorer, "E2E_REPOS", explorer.E2E_REPOS)
        explorer.E2E_REPOS = {"my-e2e"}
        po = node(
            "a",
            "SjpCaseDecisionPage",
            repo="my-e2e",
            source_file="src/test/pages/SjpCaseDecisionPage.po.ts",
        )
        self.assertTrue(explorer.include_entry(po, "code", explorer.MIN_ENTRY_DEGREE))

    def test_test_artifacts_dropped_when_no_e2e_repo_is_named(self):
        self.addCleanup(setattr, explorer, "E2E_REPOS", explorer.E2E_REPOS)
        explorer.E2E_REPOS = set()
        po = node("a", "SomePage", repo="my-e2e", source_file="src/test/pages/P.po.ts")
        self.assertFalse(explorer.include_entry(po, "code", 99))

    def test_degree_threshold_applies_to_plain_code(self):
        plain = node("a", "SomeClass")
        self.assertFalse(explorer.include_entry(plain, "code", explorer.MIN_ENTRY_DEGREE - 1))
        self.assertTrue(explorer.include_entry(plain, "code", explorer.MIN_ENTRY_DEGREE))


if __name__ == "__main__":
    unittest.main()
