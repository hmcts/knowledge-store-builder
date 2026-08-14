"""Tests for knowledgestore/build_explorer.py - the explorer's embedded index."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path


from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
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


class NodeKindTest(SettingsIsolated):
    def test_gherkin_and_ticket_kinds(self):
        self.assertEqual(explorer.node_kind(node("a", "F", kind="gherkin_feature")), "feature")
        self.assertEqual(explorer.node_kind(node("a", "S", kind="gherkin_scenario")), "scenario")
        self.assertEqual(explorer.node_kind(node("a", "T", kind="jira_ticket")), "ticket")

    def test_code_and_concept(self):
        self.assertEqual(explorer.node_kind(node("a", "X")), "code")
        concept = node("a", "X", file_type="concept")
        self.assertEqual(explorer.node_kind(concept), "concept")


class NoiseTest(SettingsIsolated):
    def test_minified_symbols_are_noise(self):
        for label in ("Zt()", "e", "$m()", "abc"):
            self.assertTrue(explorer.is_noise(node("a", label), "code"), label)

    def test_real_symbols_and_business_nodes_are_kept(self):
        self.assertFalse(explorer.is_noise(node("a", "AddressPipe"), "code"))
        self.assertFalse(explorer.is_noise(node("a", "e"), "feature"))


class BuildIndexTest(SettingsIsolated):
    def setUp(self):
        self._min_degree = config.MIN_ENTRY_DEGREE
        config.configure(MIN_ENTRY_DEGREE=0)

    def tearDown(self):
        config.configure(MIN_ENTRY_DEGREE=self._min_degree)

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


class DeploymentEntriesTest(SettingsIsolated):
    """Deployment evidence has to survive the degree gate and reach the entry."""

    def test_a_deployment_node_is_indexed_however_isolated(self):
        deployment = node("d", "progression-service (prd)", file_type="concept")
        deployment["metadata"] = {"kind": "deployment"}
        # two edges only - below MIN_ENTRY_DEGREE, and it must still be included
        self.assertTrue(explorer.include_entry(deployment, "concept", 2))

    def test_an_environment_node_is_indexed_however_isolated(self):
        environment = node("e", "prd", file_type="concept")
        environment["metadata"] = {"kind": "environment"}
        self.assertTrue(explorer.include_entry(environment, "concept", 1))

    def test_ordinary_concepts_still_face_the_degree_gate(self):
        self.assertFalse(explorer.include_entry(node("c", "SomeHelper"), "concept", 2))

    def test_the_config_summary_is_capped_and_sorted_before_capping(self):
        config_map = {f"k{i}": str(i) for i in range(50)}
        summary = explorer.deployment_summary({"kind": "deployment", "config": config_map})
        self.assertEqual(len(summary.split(" ")), config.DEPLOY_PAGE_KEYS)
        # sorted, then capped: an insertion-ordered cap would start k0 k1 k2, and
        # would churn the committed page whenever the values file was re-ordered
        self.assertTrue(summary.startswith("k0=0 k1=1 k10=10"), summary)

    def test_a_non_deployment_node_contributes_no_summary(self):
        self.assertEqual(explorer.deployment_summary({"kind": "package"}), "")
        self.assertEqual(explorer.deployment_summary({}), "")

    def test_the_summary_travels_on_the_entry_so_the_page_can_search_it(self):
        deployment = node("d", "pay-service (prd)", file_type="concept", source_file=None)
        deployment["metadata"] = {
            "kind": "deployment",
            "service": "pay-service",
            "environment": "prd",
            "config": {"replicas": "4", "resources.limits.cpu": "2"},
        }
        graph = {
            "nodes": [
                deployment,  # no edges at all, so the exemption is what indexes it
                node("n1", "AddressPipe"),
                node("n2", "AddressForm"),
                node("n3", "AddressStore"),
                node("n4", "AddressApi"),
            ],
            "links": [
                {"source": "n1", "target": "n2"},
                {"source": "n1", "target": "n3"},
                {"source": "n1", "target": "n4"},
            ],
        }
        entries, _ = explorer.build_index(graph, {}, {})
        by_label = {entry[0]: entry for entry in entries}
        self.assertEqual(by_label["pay-service (prd)"][9], "replicas=4 resources.limits.cpu=2")
        # every other entry pays one JSON string for the field and nothing more
        self.assertEqual(by_label["AddressPipe"][9], "")


class NodeTicketsTest(SettingsIsolated):
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


class EntryConnectionsTest(SettingsIsolated):
    def test_orders_by_degree_dedupes_labels_and_caps(self):
        nodes = {f"n{i}": {"label": f"L{i}"} for i in range(8)}
        nodes["dup"] = {"label": "L1"}
        adjacency = {"root": set(list(nodes))}
        degree = {nid: i for i, nid in enumerate(sorted(nodes))}
        got = explorer.entry_connections("root", adjacency, degree, nodes)
        self.assertLessEqual(len(got), explorer.MAX_CONNECTIONS)
        self.assertEqual(len(got), len(set(got)))  # deduped


class KeptEdgesTest(SettingsIsolated):
    def test_emits_each_kept_edge_once_and_skips_dropped_nodes(self):
        kept = [("a", {}, "code"), ("b", {}, "code")]
        adjacency = {"a": {"b", "dropped"}, "b": {"a"}}
        index_of = {"a": 0, "b": 1}
        self.assertEqual(explorer.kept_edges(kept, adjacency, index_of), [0, 1])


class LatestSyncedTest(SettingsIsolated):
    """Test timezone-aware chronological comparison of committed dates."""

    def test_returns_empty_string_when_no_entries(self):
        self.assertEqual(explorer.latest_synced({}), "")

    def test_returns_empty_string_when_no_committed_dates(self):
        recorded = {"repo": {"sha": "abc123"}}  # no committed key
        self.assertEqual(explorer.latest_synced(recorded), "")

    def test_extracts_date_from_single_entry(self):
        recorded = {
            "repo-a": {
                "sha": "a" * 40,
                "committed": "2026-07-30T09:14:02+01:00",
            }
        }
        self.assertEqual(explorer.latest_synced(recorded), "2026-07-30")

    def test_handles_different_timezones_chronologically(self):
        """Lexicographically larger string can be chronologically earlier."""
        # 2026-07-30T01:00:00+05:00 is lexicographically larger
        # but 2026-07-29T23:30:00-05:00 is chronologically later
        recorded = {
            "repo-a": {
                "sha": "a" * 40,
                "committed": "2026-07-30T01:00:00+05:00",  # 2026-07-29 20:00:00 UTC
            },
            "repo-b": {
                "sha": "b" * 40,
                "committed": "2026-07-29T23:30:00-05:00",  # 2026-07-30 04:30:00 UTC
            },
        }
        # repo-b is chronologically later, so should return its date
        self.assertEqual(explorer.latest_synced(recorded), "2026-07-29")

    def test_skips_invalid_timestamps(self):
        recorded = {
            "repo-a": {"sha": "a" * 40, "committed": "invalid-date"},
            "repo-b": {"sha": "b" * 40, "committed": "2026-07-30T09:14:02+01:00"},
        }
        # Should skip the invalid one and use the valid one
        self.assertEqual(explorer.latest_synced(recorded), "2026-07-30")

    def test_handles_multiple_valid_entries(self):
        recorded = {
            "repo-a": {
                "sha": "a" * 40,
                "committed": "2026-07-28T10:00:00+00:00",
            },
            "repo-b": {
                "sha": "b" * 40,
                "committed": "2026-07-30T09:14:02+01:00",
            },
            "repo-c": {
                "sha": "c" * 40,
                "committed": "2026-07-29T15:00:00+00:00",
            },
        }
        # repo-b should be latest
        self.assertEqual(explorer.latest_synced(recorded), "2026-07-30")


class BuildPageSmokeTest(SettingsIsolated):
    """main() inlines app.js and all data blocks into one page."""

    def test_main_produces_page_with_all_blocks(self):
        self.addCleanup(setattr, explorer, "MIN_ENTRY_DEGREE", config.MIN_ENTRY_DEGREE)
        config.configure(MIN_ENTRY_DEGREE=0)
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config.configure(GRAPH_PATH=root / "graph.json")
            config.configure(LABELS_PATH=root / "labels.json")
            config.configure(INTENT_INDEX_PATH=root / "missing-intent.json.gz")
            config.configure(TICKET_TITLES_PATH=root / "missing-titles.json.gz")
            config.configure(SUMMARIES_PATH=root / "missing-summaries.json")
            config.configure(SYNONYMS_PATH=root / "missing-syn.json.gz")
            config.configure(TICKET_DESCRIPTIONS_PATH=root / "missing-desc.json.gz")
            config.configure(TOPICS_BRIEFS_PATH=root / "missing-topics.json")
            config.configure(DEEPDIVES_PATH=root / "missing-dives.json")
            config.configure(EXPLORER_PATH=root / "explorer.html")
            config.configure(PROVENANCE_PATH=root / "provenance.json")
            with open(config.PROVENANCE_PATH, "w") as pf:
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
            config.GRAPH_PATH.write_text(
                _json.dumps({"nodes": [node("n1", "AddressPipe")], "links": []}), encoding="utf-8"
            )
            config.LABELS_PATH.write_text("{}", encoding="utf-8")
            code = explorer.main()
            html = config.EXPLORER_PATH.read_text(encoding="utf-8")
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
            "dives",
        ):
            self.assertIn(f'<script id="{block}"', html)
        self.assertIn("function runAsk", html)  # app.js inlined
        self.assertIn("sources synced to 2026-07-30", html)


class IncludeEntryPolicyTest(SettingsIsolated):
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
        self.assertTrue(explorer.include_entry(po, "code", config.MIN_ENTRY_DEGREE))

    def test_test_artifacts_dropped_when_no_e2e_repo_is_named(self):
        self.addCleanup(setattr, explorer, "E2E_REPOS", explorer.E2E_REPOS)
        explorer.E2E_REPOS = set()
        po = node("a", "SomePage", repo="my-e2e", source_file="src/test/pages/P.po.ts")
        self.assertFalse(explorer.include_entry(po, "code", 99))

    def test_package_declarations_are_indexed_without_connections(self):
        """A manifest-declared package is a search target however isolated it is.

        graphify's manifest ingest deliberately stops short of inventing a stub
        node for an external dependency, and prunes the dangling `depends_on`
        edge, so a first-party package node's degree collapses to whatever links
        to it inside the corpus - routinely zero. Degree-gating those nodes
        de-indexes real, named things: measured on one estate, that change took
        `depends_on` edges from 8,404 to 9 and dropped 21,133 labelled nodes below
        the bar, including the one a graded retrieval question was asserting on.
        Nothing needs to link to a package for the package to be the answer.
        """
        pkg = node("r::pkg_thing_backend", "thing_backend", source_file="backend/pyproject.toml")
        pkg["type"] = "package"
        self.assertTrue(explorer.include_entry(pkg, "code", 0))

    def test_package_nodes_still_need_a_label(self):
        """The label gate stays ahead of the package exemption.

        Java package-hierarchy nodes are also typed as packages by some
        extractors and carry no label; exempting them from the degree bar would
        put unnameable entries into the index.
        """
        unnamed = {"id": "r::pkg_uk_gov", "repo": "r", "file_type": "concept", "type": "package"}
        self.assertFalse(explorer.include_entry(unnamed, "concept", 0))

    def test_degree_threshold_applies_to_plain_code(self):
        plain = node("a", "SomeClass")
        self.assertFalse(explorer.include_entry(plain, "code", config.MIN_ENTRY_DEGREE - 1))
        self.assertTrue(explorer.include_entry(plain, "code", config.MIN_ENTRY_DEGREE))


class RepoAttributeGuardTest(unittest.TestCase):
    """A graph without `repo` must not ship a page that silently lost its tickets.

    The join in `node_tickets` is keyed on the attribute, so its absence does not
    raise - it matches nothing, and the page reads as an estate whose files no
    ticket ever touched. That is the store's most dangerous output: a confident
    negative. Reported from a store that carried the value as `repository` on all
    70,655 of its nodes and `repo` on none.
    """

    GRAPH = {"links": [], "nodes": [{"id": "a", "label": "a.py", "source_file": "a.py"}]}

    def _build(self, nodes) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            explorer.build_index({**self.GRAPH, "nodes": nodes}, {}, {})
        return err.getvalue()

    def test_a_graph_with_no_repo_attribute_is_reported(self):
        text = self._build(self.GRAPH["nodes"])
        self.assertIn("`repo`", text)
        self.assertIn(
            "no ticket evidence",
            text,
            "naming the attribute is not enough - say what the page will ship",
        )

    def test_a_graph_that_carries_it_is_not_nagged_about(self):
        nodes = [{**self.GRAPH["nodes"][0], "repo": "repo-a"}]
        self.assertEqual(self._build(nodes), "")

    def test_a_partially_stamped_graph_is_not_reported(self):
        """Some nodes legitimately lack it; only a total absence is the defect."""
        nodes = [{**self.GRAPH["nodes"][0], "repo": "repo-a"}, {"id": "b", "label": "b"}]
        self.assertEqual(self._build(nodes), "")


class JoinCardinalityTest(unittest.TestCase):
    """A join that matches nothing must not pass as a sparse estate.

    Shape, schema and freshness checks all pass on a dead join: the graph is
    valid, the index is valid, every count is healthy. Only the cardinality of
    the join says otherwise, and nothing measured it - on one store the
    file-to-ticket join produced ZERO matches across 70,655 nodes and 108
    repositories of mined tickets, with the build green and a 12-check
    regression suite passing.

    The cause is that the two documented build routes disagree about
    `source_file`: the index is keyed on repo-relative paths, and the
    single-root route emits `repositories/<repo>/<path>`.
    """

    INDEX = {"repo-a": {f"f{i}.py": {"tickets": {"T-1": 1}} for i in range(6)}}

    def _graph(self, prefix: str) -> dict:
        nodes = [
            {
                "id": f"n{i}",
                "label": f"ServiceComponent{i}",
                "repo": "repo-a",
                "source_file": f"{prefix}f{i}.py",
            }
            for i in range(6)
        ]
        links = [
            {"source": f"n{i}", "target": f"n{j}"} for i in range(6) for j in range(6) if i != j
        ]
        return {"nodes": nodes, "links": links}

    def _build(self, prefix: str):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            entries, _ = explorer.build_index(self._graph(prefix), {}, self.INDEX)
        return entries, err.getvalue()

    def test_a_dead_join_is_reported(self):
        entries, text = self._build("repositories/repo-a/")
        self.assertEqual(sum(1 for e in entries if e[7]), 0)
        self.assertIn("matched nothing", text)
        self.assertIn(
            "repositories/",
            text,
            "naming the likely cause is what makes this actionable rather than alarming",
        )
        self.assertIn(
            "different spaces",
            text,
            "the evidence shape - both sides populated, intersection empty - names the "
            "whole class; the prefix names only this instance",
        )

    def test_the_same_graph_joined_correctly_is_silent(self):
        """Identical node count and identical entries - only the join differs."""
        entries, text = self._build("")
        self.assertEqual(sum(1 for e in entries if e[7]), 6)
        self.assertEqual(text, "")

    def test_a_working_join_reports_its_rate(self):
        """A partial join is the quieter failure: one estate fixed the AST half
        and left the semantic half skipping every record, and 5,692 of 72,370
        reads as a working join on a sparse estate. The rate is a measurement,
        not a verdict - no threshold is asserted."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            explorer.build_index(self._graph(""), {}, self.INDEX)
        self.assertIn("6 of 6", out.getvalue())
        self.assertIn("100.0%", out.getvalue())

    def test_a_dead_join_reports_no_rate(self):
        """Zero goes to stderr as a warning, not to stdout as a statistic."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            explorer.build_index(self._graph("repositories/repo-a/"), {}, self.INDEX)
        self.assertNotIn("carry ticket evidence", out.getvalue())

    def _layered(self, semantic_prefix: str, semantic_repo: str = "repo-a") -> tuple[str, str]:
        """An AST layer that joins and a semantic layer that may not."""
        nodes = [
            {
                "id": f"a{i}",
                "label": f"AstComponent{i}",
                "repo": "repo-a",
                "source_file": f"f{i}.py",
                "_origin": "ast",
            }
            for i in range(6)
        ]
        nodes += [
            {
                "id": f"s{i}",
                "label": f"SemComponent{i}",
                "repo": semantic_repo,
                "source_file": f"{semantic_prefix}f{i}.py",
            }
            for i in range(6)
        ]
        ids = [n["id"] for n in nodes]
        graph = {
            "nodes": nodes,
            "links": [{"source": x, "target": y} for x in ids for y in ids if x != y],
        }
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            explorer.build_index(graph, {}, self.INDEX)
        return out.getvalue(), err.getvalue()

    def test_one_layer_keyed_differently_is_reported_though_the_whole_is_not_zero(self):
        """The half-dead case a composite count structurally cannot show.

        One estate converted its AST layer and left the semantic layer skipping
        every record: 5,692 of 72,370 is never zero and reads as a working join
        on a sparse estate. Per layer it was 0 of 46,602.
        """
        out, err = self._layered("/absolute/gone/")
        self.assertIn("semantic layer", err)
        self.assertIn("0 of 6", err)
        self.assertIn("50.0%", out, "the composite stays non-zero, which is the whole point")

    def test_layers_that_all_join_are_not_reported(self):
        out, err = self._layered("")
        self.assertEqual(err, "")
        self.assertIn("12 of 12", out)

    def test_a_layer_whose_repository_is_not_mined_is_not_reported(self):
        """Sparsity, not a key mismatch - and the false positive this check
        produced on the maintainer's own estate before the restriction was
        added: 2,115 `meta-arch` nodes joining zero because their repository is
        not in the index at all."""
        out, err = self._layered("", semantic_repo="unmined-repo")
        self.assertEqual(err, "", "a layer with nothing to join against must not be accused")

    def test_an_estate_with_no_intent_index_is_not_reported(self):
        """Nothing to join is not a broken join."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            explorer.build_index(self._graph(""), {}, {})
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()


class TrackerEvidenceTest(SettingsIsolated):
    """Real ticket titles and descriptions reach the page; comments do not.

    Before this, `fetch-tickets` wrote an artefact nothing read: one estate held
    12,298 fetched tickets - 6,569 with real titles and 5,839 with descriptions -
    and the page still showed mined commit subjects. The stage delivered a file,
    not an answer.

    Comments are deliberately excluded. The same estate's 38,231 comments hold
    10.1 M characters, and the page is the artefact people download and open
    offline; an agent reads the committed artefact directly for those.
    """

    def test_a_tracker_summary_replaces_a_mined_commit_subject(self):
        merged = explorer.merge_ticket_evidence(
            mined={"CCT-1": {"d": ["fix: CCT-1 tweak the thing"]}},
            tracker={"CCT-1": {"summary": "Postcode lookup rejects valid BFPO addresses"}},
        )
        self.assertEqual(merged["CCT-1"]["t"], "Postcode lookup rejects valid BFPO addresses")
        # the mined evidence stays: it is what the commits actually said
        self.assertEqual(merged["CCT-1"]["d"], ["fix: CCT-1 tweak the thing"])

    def test_mined_evidence_survives_where_no_tracker_data_exists(self):
        merged = explorer.merge_ticket_evidence(
            mined={"DD-9": {"d": ["chore: DD-9 bump"]}}, tracker={}
        )
        self.assertEqual(merged["DD-9"]["d"], ["chore: DD-9 bump"])
        self.assertNotIn("t", merged["DD-9"])

    def test_an_absent_ticket_contributes_nothing(self):
        """A 404 is cached as absent; it is not a title."""
        merged = explorer.merge_ticket_evidence(
            mined={"WIP-1": {"d": ["wip"]}},
            tracker={"WIP-1": {"absent": True, "checked": "2026-08-06"}},
        )
        self.assertNotIn("t", merged["WIP-1"])

    def test_the_description_is_capped_for_the_page(self):
        config.configure(TICKET_DETAIL_CHARS=40)
        merged = explorer.merge_ticket_evidence(
            mined={}, tracker={"CCT-2": {"summary": "s", "description": "word " * 100}}
        )
        self.assertLessEqual(len(merged["CCT-2"]["x"]), 41)

    def test_comments_are_carried_so_they_can_be_searched(self):
        """Deliberately reversed: this test previously asserted the opposite.

        Comments were held out of the page when the plan was to distribute the file
        outside the repository, where 10.8 M characters of narrative would travel
        with it. That distribution was dropped and the requirement became that every
        layer of evidence is queryable. Comments are the layer that answers why a
        change was made, and a page holding them without searching them would be the
        worst of both.

        The cost is measured: the page goes from 51 MB to about 61 MB, 3.3 MB of that
        gzipped. Comments arrive already bounded by KSB_TRACKER_COMMENT_CHARS, so
        there is no second cap here - one policy, applied at fetch.
        """
        merged = explorer.merge_ticket_evidence(
            mined={}, tracker={"CCT-3": {"summary": "s", "comments": ["first note", "second"]}}
        )
        self.assertEqual(merged["CCT-3"]["c"], ["first note", "second"])

    def test_a_ticket_with_no_comments_carries_no_comment_field(self):
        """An empty list would read as evidence found, matching the stage's rule."""
        merged = explorer.merge_ticket_evidence(
            mined={}, tracker={"CCT-4": {"summary": "s", "comments": []}}
        )
        self.assertNotIn("c", merged["CCT-4"])

    def test_a_ticket_known_only_to_the_tracker_still_appears(self):
        """Evidence can exist without a commit mentioning the id."""
        merged = explorer.merge_ticket_evidence(
            mined={}, tracker={"GPE-7": {"summary": "Welsh translation missing on the plea page"}}
        )
        self.assertEqual(merged["GPE-7"]["t"], "Welsh translation missing on the plea page")
