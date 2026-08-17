"""Grounding checks for authored summaries.

Every mechanical gate in the authoring pipeline measures shape: ids present,
lengths in range, merge accepted. A batch of confidently fabricated summaries
passes all of them. These checks measure grounding instead — whether the prose
cites anything the evidence does not contain.

The hard part is not detection, it is false positives. Ordinary prose is full of
capitalised words ("Welsh", "Angular", "Service Manual") that are not claims
about code. The rule used here is structural rather than a blocklist: a token is
treated as an identifier only if it has an internal case change, an underscore,
two or more hyphens, a file extension, or a ticket shape. Prose capitalisation
does not survive that test, so the checker stays quiet on English.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import config  # noqa: E402


class IdentifierExtractionTest(SettingsIsolated):
    """What counts as a claim about code, and what is just English."""

    def test_camel_case_is_an_identifier(self):
        self.assertIn("CaseAggregate", summaries.prose_identifiers("Dominated by CaseAggregate."))

    def test_snake_case_and_repo_names_are_identifiers(self):
        found = summaries.prose_identifiers("svc-context-progression holds case_status")
        self.assertIn("svc-context-progression", found)
        self.assertIn("case_status", found)

    def test_file_names_and_ticket_ids_are_identifiers(self):
        found = summaries.prose_identifiers("pom.xml changed under CCT-1234")
        self.assertIn("pom.xml", found)
        self.assertIn("CCT-1234", found)

    def test_english_compound_adjectives_are_not_identifiers(self):
        # "the police-to-courtroom mapping" is prose; flagging it is noise that
        # trains readers to ignore the report
        for phrase in ("police-to-courtroom", "end-to-end", "point-in-time", "out-of-hours"):
            self.assertEqual(
                summaries.prose_identifiers(f"Handles {phrase} routing."),
                set(),
                phrase,
            )

    def test_hyphenated_code_names_are_still_identifiers(self):
        found = summaries.prose_identifiers("Defined in svc-context-progression and svc-case-api")
        self.assertIn("svc-context-progression", found)

    def test_ordinary_capitalised_prose_is_not_an_identifier(self):
        # the false-positive case that would make the checker useless
        text = "Welsh language handling in the Service Manual, an Angular UI using JSON."
        self.assertEqual(summaries.prose_identifiers(text), set())

    def test_sentence_start_and_acronyms_are_not_identifiers(self):
        text = "This service records data. API responses use HTTP and RAML schemas."
        self.assertEqual(summaries.prose_identifiers(text), set())


class VerifyTest(SettingsIsolated):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        self._old_root = config.ROOT
        config.configure(root=str(self.root))

    def tearDown(self):
        config.configure(root=str(self._old_root))
        self._tmp.cleanup()

    def write(self, digests, prose):
        config.SUMMARIES_INPUT_PATH.write_text(json.dumps(digests), encoding="utf-8")
        config.SUMMARIES_PATH.write_text(json.dumps(prose), encoding="utf-8")

    def digest(self, cid, label, repo, nodes):
        return {
            "id": cid,
            "label": label,
            "size": 40,
            "repositories": [repo],
            "top_nodes": [{"label": n, "source_file": f"src/{n}.java"} for n in nodes],
            "business_features": [],
            "tickets": [],
        }

    def run_verify(self, **kwargs):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = summaries.verify(**kwargs)
        return code, buffer.getvalue()

    # --- the core check ------------------------------------------------------

    def test_a_summary_citing_something_absent_from_its_digest_is_reported(self):
        # the failure this whole check exists for: plausible, well-formed,
        # correctly-lengthed prose that names code the evidence does not contain
        self.write(
            [self.digest("1", "CaseAggregate", "svc-context-progression", ["CaseAggregate"])],
            {"1": "Case progression logic in svc-context-progression, built on HearingAggregate."},
        )
        code, output = self.run_verify()
        self.assertEqual(code, 0, "reporting is not failing, unless --strict")
        self.assertIn("HearingAggregate", output)
        self.assertIn("1", output)

    def test_a_summary_citing_only_evidence_is_not_reported(self):
        self.write(
            [self.digest("1", "CaseAggregate", "svc-context-progression", ["CaseAggregate"])],
            {"1": "Case progression logic in svc-context-progression, around CaseAggregate."},
        )
        code, output = self.run_verify()
        self.assertEqual(code, 0)
        self.assertNotIn("[unsupported]", output)

    def test_identifiers_from_source_paths_count_as_evidence(self):
        # a digest cites src/CaseAggregate.java; prose may name the file
        self.write(
            [self.digest("1", "CaseAggregate", "svc-context-progression", ["CaseAggregate"])],
            {"1": "Progression logic in svc-context-progression, see CaseAggregate.java."},
        )
        _, output = self.run_verify()
        self.assertNotIn("CaseAggregate.java", output)

    def test_prose_with_no_identifiers_at_all_is_quiet(self):
        self.write(
            [self.digest("1", "Utilities", "svc-a", ["Helper"])],
            {"1": "Shared date and string utilities used across the service."},
        )
        _, output = self.run_verify()
        self.assertNotIn("[unsupported]", output)

    def test_top_nodes_written_as_strings_are_read_as_evidence(self):
        # real digests write a node as "Label (source/file.ext)" in one string.
        # Every fixture here used the dict form, so a full passing suite still
        # crashed on a real store - this pins the shape that actually ships.
        config.SUMMARIES_INPUT_PATH.write_text(
            json.dumps(
                [
                    {
                        "id": "1",
                        "label": "CaseAggregate",
                        "size": 40,
                        "repositories": ["svc-a"],
                        "top_nodes": ["CaseAggregate (src/main/java/CaseAggregate.java)"],
                        "business_features": [],
                        "tickets": [],
                    }
                ]
            ),
            encoding="utf-8",
        )
        config.SUMMARIES_PATH.write_text(
            json.dumps({"1": "Logic in svc-a around CaseAggregate."}), encoding="utf-8"
        )
        code, output = self.run_verify()
        self.assertEqual(code, 0)
        self.assertNotIn("[unsupported]", output)

    def test_a_test_class_in_evidence_grounds_a_claim_about_the_class(self):
        # a digest showing HearingResultHelperTest is evidence the helper exists;
        # describing the class rather than its test is interpretation
        self.write(
            [self.digest("1", "HearingResultHelperTest", "svc-a", ["HearingResultHelperTest"])],
            {"1": "Helper logic in svc-a around HearingResultHelper."},
        )
        _, output = self.run_verify()
        self.assertNotIn("[unsupported]", output)

    def test_method_decoration_in_evidence_grounds_the_bare_name(self):
        # graph node labels are written ".saveDecision()"; prose says saveDecision
        self.write(
            [self.digest("1", "CaseService", "svc-a", [".saveDecision()"])],
            {"1": "The svc-a case service exposes saveDecision for SJP decisions."},
        )
        _, output = self.run_verify()
        self.assertNotIn("[unsupported]", output)

    def test_kebab_and_camel_spellings_of_one_concept_agree(self):
        # this estate names a schema in kebab-case and its class in CamelCase
        self.write(
            [self.digest("1", "ResultPromptWordSynonym", "svc-a", ["ResultPromptWordSynonym"])],
            {"1": "Handles the result-prompt-word-synonym reference data in svc-a."},
        )
        _, output = self.run_verify()
        self.assertNotIn("[unsupported]", output)

    def test_a_longer_name_is_still_not_the_same_identifier(self):
        # normalisation must not collapse Foo into FooProcessor, or the check
        # stops distinguishing a real class from an invented neighbour
        self.write(
            [self.digest("1", "CaseDecisionProcessor", "svc-a", ["CaseDecisionProcessor"])],
            {"1": "Logic in svc-a around ApplicationDecisionProcessor."},
        )
        _, output = self.run_verify()
        self.assertIn("ApplicationDecisionProcessor", output)

    def test_an_event_named_inside_a_dotted_schema_filename_is_grounded(self):
        # Schema and event contracts are filed as dotted names while prose cites
        # the event itself. Without this, a correct summary of a schema cluster
        # reports as unsupported, and on a schema-heavy estate that was 90% of
        # everything the check flagged -- enough noise to make people dismiss it.
        digest = self.digest("1", "referencedata-event/listener", "svc-a", [])
        digest["top_nodes"] = [
            {
                "label": "properties",
                "source_file": "referencedata-event/src/schema/referencedata.event.case-marker-added.json",
            }
        ]
        self.write([digest], {"1": "Schema content in svc-a defining the case-marker-added event."})
        _, output = self.run_verify()
        self.assertNotIn("[unsupported]", output)

    def test_a_dotted_component_does_not_ground_an_invented_neighbour(self):
        # splitting on dots must not become substring matching: an event the
        # evidence does not hold is still unsupported
        digest = self.digest("1", "referencedata-event/listener", "svc-a", [])
        digest["top_nodes"] = [
            {
                "label": "properties",
                "source_file": "referencedata-event/src/schema/referencedata.event.case-marker-added.json",
            }
        ]
        self.write(
            [digest], {"1": "Schema content in svc-a defining the case-marker-removed event."}
        )
        _, output = self.run_verify()
        self.assertIn("case-marker-removed", output)

    # --- speculation ---------------------------------------------------------

    def test_speculation_words_are_reported(self):
        self.write(
            [self.digest("1", "Helper", "svc-a", ["Helper"])],
            {"1": "This probably handles retries and appears to log failures."},
        )
        _, output = self.run_verify()
        self.assertIn("probably", output)
        self.assertIn("appears to", output)

    def test_factual_prose_raises_no_speculation_finding(self):
        self.write(
            [self.digest("1", "Helper", "svc-a", ["Helper"])],
            {"1": "Retry handling and failure logging for the service."},
        )
        _, output = self.run_verify()
        self.assertNotIn("[speculation]", output)

    # --- sampling and strict -------------------------------------------------

    def test_sampling_reports_its_own_rate_so_it_cannot_be_read_as_full_coverage(self):
        digests = [self.digest(str(i), "Helper", "svc-a", ["Helper"]) for i in range(50)]
        prose = {str(i): "Helper utilities in svc-a." for i in range(50)}
        self.write(digests, prose)
        _, output = self.run_verify(sample=10)
        self.assertIn("10", output)
        self.assertIn("50", output, "the denominator must appear, not just the sample size")

    def test_strict_fails_when_something_is_unsupported(self):
        self.write(
            [self.digest("1", "CaseAggregate", "svc-a", ["CaseAggregate"])],
            {"1": "Logic in svc-a around InventedClassName."},
        )
        code, _ = self.run_verify(strict=True)
        self.assertEqual(code, 1)

    def test_strict_passes_when_everything_is_grounded(self):
        self.write(
            [self.digest("1", "CaseAggregate", "svc-a", ["CaseAggregate"])],
            {"1": "Logic in svc-a around CaseAggregate."},
        )
        code, _ = self.run_verify(strict=True)
        self.assertEqual(code, 0)

    # --- guards --------------------------------------------------------------

    def test_refuses_when_there_is_nothing_to_verify(self):
        # a mis-specified path must not read as a clean bill of health
        self.write([], {})
        code, _ = self.run_verify()
        self.assertEqual(code, 1)

    def test_a_summary_with_no_matching_digest_is_reported(self):
        # cannot be grounded against evidence that is not there
        self.write([self.digest("1", "Helper", "svc-a", ["Helper"])], {"99": "Orphan summary."})
        _, output = self.run_verify()
        self.assertIn("99", output)


class VerifyCliTest(VerifyTest):
    def test_verify_is_reachable_as_a_sub_command(self):
        self.write(
            [self.digest("1", "CaseAggregate", "svc-a", ["CaseAggregate"])],
            {"1": "Logic in svc-a around CaseAggregate."},
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = summaries.main(["verify"])
        self.assertEqual(code, 0)

    def test_strict_flag_reaches_the_check(self):
        self.write(
            [self.digest("1", "CaseAggregate", "svc-a", ["CaseAggregate"])],
            {"1": "Logic in svc-a around InventedClassName."},
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = summaries.main(["verify", "--strict"])
        self.assertEqual(code, 1)


class ProvenanceSplitTest(SettingsIsolated):
    """verify reports grounding split by carried-vs-authored when a remap
    report exists - remap preserves coverage while degrading grounding
    (measured: 9% flagged authored, 37% carried), so retention improvements
    must never be read without this line beside them."""

    def test_split_line_reports_both_groups(self):
        from contextlib import redirect_stdout
        from io import StringIO

        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=str(tmp))
            (Path(tmp) / "knowledge" / "summaries").mkdir(parents=True)
            config.SUMMARIES_INPUT_PATH.write_text(
                json.dumps(
                    [
                        {
                            "id": 1,
                            "label": "HearingStore",
                            "top_nodes": [{"label": "HearingStore"}],
                        },
                        {"id": 2, "label": "ResultsFlow", "top_nodes": [{"label": "ResultsFlow"}]},
                    ]
                ),
                encoding="utf-8",
            )
            config.SUMMARIES_PATH.write_text(
                json.dumps(
                    {
                        "1": "Covers HearingStore and nothing else worth naming here.",
                        "2": "Claims a FabricatedWidget the digest does not contain.",
                    }
                ),
                encoding="utf-8",
            )
            config.REMAP_REPORT_PATH.write_text(
                json.dumps({"carried": {"2": {"from": "9", "share": 0.9}}, "displaced": {}}),
                encoding="utf-8",
            )
            out = StringIO()
            with redirect_stdout(out):
                summaries.verify()
        text = out.getvalue()
        self.assertIn("grounding by provenance", text)
        self.assertIn("carried 100% (1 of 1)", text)
        self.assertIn("authored 0% (0 of 1)", text)


class VerifyNamesWhatItMeasuresTest(SettingsIsolated):
    """A digest is a 12-node sample; "unsupported" claimed more than that.

    `verify` reported 260 of 482 summaries with "unsupported citations", which
    reads as half the store making unbacked claims. Measured on that estate, 239
    of 244 distinct flagged terms (98%) existed in the corpus - the digest simply
    had not sampled them. A gate that is 98% false positives gets ignored, and
    then stops catching the 2% that matter.
    """

    def _store(self, digest_nodes: list, prose: str, graph_labels: list | None = None) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "knowledge" / "summaries").mkdir(parents=True)
        (root / "graphify-out").mkdir(parents=True)
        config.configure(root=str(root))
        # json.dump rather than knowledgestore.io, because this module already
        # imports the stdlib `io` and shadowing it would be worse than verbose.
        config.SUMMARIES_INPUT_PATH.write_text(
            json.dumps([{"id": "1", "top_nodes": [{"label": n} for n in digest_nodes]}]),
            encoding="utf-8",
        )
        config.SUMMARIES_PATH.write_text(json.dumps({"1": prose}), encoding="utf-8")
        config.GRAPH_PATH.write_text(
            json.dumps({"nodes": [{"id": n, "label": n} for n in (graph_labels or [])]}),
            encoding="utf-8",
        )

    def _run(self, **kwargs) -> str:
        captured = io.StringIO()
        with redirect_stdout(captured), redirect_stderr(io.StringIO()):
            summaries.verify(**kwargs)
        return captured.getvalue()

    def test_the_digest_finding_is_not_called_unsupported(self):
        """It measures digest corroboration, and saying "unsupported" invites
        someone to act on 98% noise."""
        self._store(["AlphaService"], "Covers AlphaService and BetaWidget.")
        text = self._run()
        self.assertIn("not in digest", text)
        self.assertNotIn("[unsupported]", text)

    def test_the_digest_run_says_the_finding_is_usually_real_content(self):
        self._store(["AlphaService"], "Covers AlphaService and BetaWidget.")
        self.assertIn("did not sample", self._run())

    def test_a_term_in_the_graph_is_not_reported_as_absent(self):
        """The point of the wider pass: the digest missed it, the estate has it."""
        self._store(["AlphaService"], "Covers AlphaService and BetaWidget.", ["BetaWidget"])
        text = self._run(estate=True)
        self.assertNotIn("not in graph", text)
        self.assertIn("exists somewhere in the graph", text)

    def test_a_term_in_neither_is_reported_as_a_candidate_not_a_fabrication(self):
        """Wording matters here: the graph is narrower than the corpus, so this
        is a shortlist to read. On a real estate the one summary it isolated
        cited two terms that existed in the corpus."""
        self._store(["AlphaService"], "Covers AlphaService and GammaThing.", ["BetaWidget"])
        text = self._run(estate=True)
        self.assertIn("not in graph", text)
        self.assertIn("NARROWER one than the corpus", text)
        self.assertNotIn("fabrication", text.replace("proven fabrications", ""))

    def test_strict_without_estate_keeps_its_previous_meaning(self):
        """An existing CI invocation must not silently change what it fails on."""
        self._store(["AlphaService"], "Covers AlphaService and GammaThing.", ["GammaThing"])
        self.assertEqual(summaries.verify(strict=True), 1)

    def test_strict_with_estate_fails_only_on_what_the_graph_lacks(self):
        self._store(["AlphaService"], "Covers AlphaService and GammaThing.", ["GammaThing"])
        self.assertEqual(summaries.verify(strict=True, estate=True), 0)


if __name__ == "__main__":
    unittest.main()
