"""Tests for knowledgestore/extract_gherkin.py - the Gherkin business-intent layer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


from knowledgestore import config  # noqa: E402
from knowledgestore import extract_gherkin as gherkin  # noqa: E402

FEATURE = """@regression @DD-123
Feature: Amend defendant address

  Background: signed in
    Given user signs in to common platform

  Scenario: Amend address for a person defendant
    When user updates defendant details "name", "dob", "address"
    Then the address is saved

  Scenario Outline: Amend for <type>
    When user enters court location and selects option for working under delegated powers
"""

JAVA = """package com.stepdefinitions;
public class DefendantDetailsStepDefinitions {
    @When("user updates defendant details {string}, {string}, {string}")
    public void update() {}
    @Then("the address is saved")
    public void saved() {}
}
"""


class NormaliseStepTest(unittest.TestCase):
    def test_cucumber_expressions_and_quotes_align(self):
        annotation = gherkin.normalise_step(
            "user updates defendant details {string}, {string}, {string}"
        )
        step = gherkin.normalise_step('user updates defendant details "name", "dob", "address"')
        self.assertEqual(annotation, step)

    def test_outline_params_and_numbers_align(self):
        self.assertEqual(
            gherkin.normalise_step("waits <seconds> seconds"),
            gherkin.normalise_step("waits 30 seconds"),
        )


class ParseFeatureTest(unittest.TestCase):
    def test_parses_name_scenarios_tags_and_tickets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "features" / "CPS" / "DD-999-amend-address.feature"
            path.parent.mkdir(parents=True)
            path.write_text(FEATURE, encoding="utf-8")
            feature = gherkin.parse_feature(path, root)

        self.assertEqual(feature["name"], "Amend defendant address")
        self.assertEqual(len(feature["scenarios"]), 2)
        self.assertIn("DD-123", feature["tickets"])  # from tag
        self.assertIn("DD-999", feature["tickets"])  # from filename
        self.assertIn("regression", feature["tags"])

    def test_file_without_feature_header_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "broken.feature"
            path.write_text("Scenario: floating\n", encoding="utf-8")
            self.assertIsNone(gherkin.parse_feature(path, root))


class FeatureAreaTest(unittest.TestCase):
    def test_groups_by_first_directory_under_features(self):
        self.assertEqual(gherkin.feature_area("src/test/resources/features/CPS/PET.feature"), "CPS")
        self.assertEqual(gherkin.feature_area("elsewhere/x.feature"), "(root)")


class StepDefinitionsTest(unittest.TestCase):
    def test_maps_normalised_patterns_to_class_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            java = repo / "src" / "test" / "java" / "com" / "stepdefinitions" / "D.java"
            java.parent.mkdir(parents=True)
            java.write_text(JAVA, encoding="utf-8")
            patterns = gherkin.parse_step_definitions(repo)

        key = gherkin.normalise_step("user updates defendant details {string}, {string}, {string}")
        self.assertIn(key, patterns)
        self.assertEqual(patterns[key][0], "DefendantDetailsStepDefinitions")


class GraphEnricherTest(unittest.TestCase):
    def _graph(self):
        return {
            "nodes": [
                {
                    "id": "r::stepdef",
                    "label": "D",
                    "repo": "repo-a",
                    "community": 1,
                    "source_file": "src/test/java/com/stepdefinitions/D.java",
                },
            ],
            "links": [],
        }

    def _feature(self):
        return {
            "rel": "src/test/resources/features/CPS/amend.feature",
            "name": "Amend defendant address",
            "scenarios": ["Amend address for a person defendant"],
            "steps": {gherkin.normalise_step("the address is saved")},
            "tags": ["regression"],
            "tickets": ["DD-123"],
        }

    def test_adds_feature_scenario_and_ticket_nodes_with_edges(self):
        graph = self._graph()
        enricher = gherkin.GraphEnricher(graph, {})
        enricher.add_feature(self._feature(), "repo-a", {}, {})

        kinds = [(n.get("metadata") or {}).get("kind") for n in graph["nodes"]]
        self.assertIn("feature", kinds)
        self.assertIn("scenario", kinds)
        self.assertIn("ticket", kinds)
        relations = {e["relation"] for e in graph["links"]}
        self.assertEqual(relations, {"contains", "references"})

    def test_duplicate_feature_is_skipped(self):
        graph = self._graph()
        enricher = gherkin.GraphEnricher(graph, {})
        enricher.add_feature(self._feature(), "repo-a", {}, {})
        nodes_after_first = len(graph["nodes"])
        enricher.add_feature(self._feature(), "repo-a", {}, {})
        self.assertEqual(len(graph["nodes"]), nodes_after_first)
        self.assertEqual(enricher.stats["duplicate"], 1)


class NormIdTest(unittest.TestCase):
    def test_lowercases_and_squashes_non_alphanumerics(self):
        self.assertEqual(gherkin.norm_id("CPS/PET File.feature"), "cps_pet_file_feature")
        self.assertEqual(gherkin.norm_id("__x--y__"), "x_y")


class StepdefClassNodesTest(unittest.TestCase):
    def test_maps_java_class_nodes_by_source_file(self):
        graph = {
            "nodes": [
                {
                    "id": "a",
                    "label": "D",
                    "repo": "repo-a",
                    "source_file": "src/test/java/com/stepdefinitions/D.java",
                },
                {
                    "id": "b",
                    "label": "NotTheStem",
                    "repo": "repo-a",
                    "source_file": "src/test/java/com/stepdefinitions/E.java",
                },
                {
                    "id": "c",
                    "label": "F",
                    "repo": "other-repo",
                    "source_file": "src/test/java/com/stepdefinitions/F.java",
                },
            ]
        }
        mapping = gherkin.stepdef_class_nodes(graph, "repo-a")
        self.assertEqual(mapping, {"src/test/java/com/stepdefinitions/D.java": "a"})


class EnrichRepositoryTest(unittest.TestCase):
    def test_walks_features_and_links_step_definitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo-a"
            feature = repo / "src" / "test" / "resources" / "features" / "CPS" / "a.feature"
            feature.parent.mkdir(parents=True)
            feature.write_text(FEATURE, encoding="utf-8")
            java = repo / "src" / "test" / "java" / "com" / "stepdefinitions" / "D.java"
            java.parent.mkdir(parents=True)
            java.write_text(JAVA, encoding="utf-8")

            graph = {
                "nodes": [
                    {
                        "id": "r::stepdef",
                        "label": "D",
                        "repo": "repo-a",
                        "community": 1,
                        "source_file": "src/test/java/com/stepdefinitions/D.java",
                    },
                ],
                "links": [],
            }
            enricher = gherkin.GraphEnricher(graph, {})
            gherkin.enrich_repository(repo, enricher)

        self.assertEqual(enricher.stats["features"], 1)
        self.assertGreaterEqual(enricher.stats["scenarios"], 1)
        self.assertGreaterEqual(enricher.stats["stepdef_edges"], 1)
        stepdef_edges = [e for e in graph["links"] if e["target"] == "r::stepdef"]
        self.assertTrue(stepdef_edges)


class WriteOutputsTest(unittest.TestCase):
    def test_writes_graph_labels_and_recompressed_gz(self):
        import gzip as _gzip
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            gherkin.GRAPH_PATH = Path(tmp) / "graph.json"
            gherkin.LABELS_PATH = Path(tmp) / ".graphify_labels.json"
            graph = {"nodes": [{"id": "n"}], "links": []}
            gherkin.write_outputs(graph, {"1": "Area"})
            written = _json.loads(gherkin.GRAPH_PATH.read_text(encoding="utf-8"))
            zipped = _json.loads(
                _gzip.open(Path(tmp) / "graph.json.gz", "rt", encoding="utf-8").read()
            )
        self.assertEqual(written, graph)
        self.assertEqual(zipped, graph)


class GraphReportNoteTest(unittest.TestCase):
    """The audit report must not silently disagree with the graph beside it.

    `graphify` writes GRAPH_REPORT.md from its own pass, then this stage adds the
    Gherkin layer and the report is never regenerated. Anyone reconciling the two
    finds a discrepancy with no stated cause, and the business-intent layer — the
    part that makes business-language questions answerable — is invisible in the
    audit that is supposed to describe the graph.
    """

    def _report(self, tmp: Path) -> Path:
        report = tmp / "GRAPH_REPORT.md"
        report.write_text("# Graph Report\n\n- 100 nodes · 200 edges\n", encoding="utf-8")
        gherkin.REPORT_PATH = report
        return report

    def test_the_report_records_what_gherkin_added_after_it_was_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self._report(Path(tmp))
            gherkin.note_gherkin_layer({"features": 12, "scenarios": 30}, nodes=142, edges=260)
            text = report.read_text(encoding="utf-8")
        self.assertIn("100 nodes", text, "graphify's own report is preserved")
        self.assertIn("142", text, "the graph's actual node count is stated")
        self.assertIn("12", text, "what this stage added is stated")

    def test_running_twice_does_not_stack_notes(self):
        # stages are idempotent; a note that appends on every run turns the
        # report into a changelog and makes the newest figure hard to find
        with tempfile.TemporaryDirectory() as tmp:
            report = self._report(Path(tmp))
            gherkin.note_gherkin_layer({"features": 12}, nodes=142, edges=260)
            gherkin.note_gherkin_layer({"features": 13}, nodes=143, edges=261)
            text = report.read_text(encoding="utf-8")
        self.assertEqual(text.count(gherkin.REPORT_NOTE_MARKER), 1)
        self.assertIn("143", text, "the note reflects the latest run")
        self.assertNotIn("142", text, "the superseded figure is gone")

    def test_a_missing_report_is_not_an_error(self):
        # graphify may not have run, or the store may not keep the report
        with tempfile.TemporaryDirectory() as tmp:
            gherkin.REPORT_PATH = Path(tmp) / "absent.md"
            gherkin.note_gherkin_layer({"features": 1}, nodes=2, edges=3)
            self.assertFalse((Path(tmp) / "absent.md").exists())


class ConfiguredLanguagesTest(unittest.TestCase):
    """An added step-definition language must take effect after import.

    The module used to copy config.STEP_DEFINITION_LANGUAGES to a module-level
    name at import time and read the copy. This module is imported before a
    caller can configure anything, so config.configure() was accepted - it
    raises KeyError only for an unknown setting - and then silently ignored: the
    stage searched the three default languages, matched none of the estate's
    step definitions, and reported success.
    """

    def setUp(self):
        self.original = config.STEP_DEFINITION_LANGUAGES

    def tearDown(self):
        config.configure(STEP_DEFINITION_LANGUAGES=self.original)

    def test_a_language_added_after_import_is_searched(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "steps").mkdir()
            (repo / "steps" / "address_steps.go").write_text(
                'Given("user amends the defendant address", func() {})\n',
                encoding="utf-8",
            )

            # the three default languages cannot see a .go file
            self.assertEqual(gherkin.parse_step_definitions(repo), {})

            config.configure(
                STEP_DEFINITION_LANGUAGES={
                    "go": {
                        "glob": "**/*.go",
                        "annotation": r"\b(?:Given|When|Then)\s*\(\s*\"(.*?)\"\s*,",
                        "symbol": None,
                    }
                }
            )
            found = gherkin.parse_step_definitions(repo)

        self.assertIn(
            "user amends the defendant address",
            found,
            "configure() after import was accepted and then ignored",
        )
        self.assertEqual(found["user amends the defendant address"][1], "steps/address_steps.go")


if __name__ == "__main__":
    unittest.main()
