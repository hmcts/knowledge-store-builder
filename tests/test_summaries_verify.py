"""Grounding checks for authored summaries.

Every mechanical gate in the authoring pipeline measures shape: ids present,
lengths in range, merge accepted. A batch of confidently fabricated summaries
passes all of them. These checks measure grounding instead — whether the prose
cites anything the evidence does not contain.

The hard part is not detection, it is false positives. Ordinary prose is full of
capitalised words ("Welsh", "Angular", "Common Platform") that are not claims
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
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import config  # noqa: E402


class IdentifierExtractionTest(unittest.TestCase):
    """What counts as a claim about code, and what is just English."""

    def test_camel_case_is_an_identifier(self):
        self.assertIn("CaseAggregate", summaries.prose_identifiers("Dominated by CaseAggregate."))

    def test_snake_case_and_repo_names_are_identifiers(self):
        found = summaries.prose_identifiers("cpp-context-progression holds speak_welsh")
        self.assertIn("cpp-context-progression", found)
        self.assertIn("speak_welsh", found)

    def test_file_names_and_ticket_ids_are_identifiers(self):
        found = summaries.prose_identifiers("pom.xml changed under CCT-1234")
        self.assertIn("pom.xml", found)
        self.assertIn("CCT-1234", found)

    def test_ordinary_capitalised_prose_is_not_an_identifier(self):
        # the false-positive case that would make the checker useless
        text = "Welsh language handling in the Common Platform, an Angular UI using JSON."
        self.assertEqual(summaries.prose_identifiers(text), set())

    def test_sentence_start_and_acronyms_are_not_identifiers(self):
        text = "This service records data. API responses use HTTP and RAML schemas."
        self.assertEqual(summaries.prose_identifiers(text), set())


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        self._old_root = config.ROOT
        config.configure(root=str(self.root))
        summaries.INPUT_PATH = config.SUMMARIES_INPUT_PATH
        summaries.OUTPUT_PATH = config.SUMMARIES_PATH

    def tearDown(self):
        config.configure(root=str(self._old_root))
        summaries.INPUT_PATH = config.SUMMARIES_INPUT_PATH
        summaries.OUTPUT_PATH = config.SUMMARIES_PATH
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
            [self.digest("1", "CaseAggregate", "cpp-context-progression", ["CaseAggregate"])],
            {"1": "Case progression logic in cpp-context-progression, built on HearingAggregate."},
        )
        code, output = self.run_verify()
        self.assertEqual(code, 0, "reporting is not failing, unless --strict")
        self.assertIn("HearingAggregate", output)
        self.assertIn("1", output)

    def test_a_summary_citing_only_evidence_is_not_reported(self):
        self.write(
            [self.digest("1", "CaseAggregate", "cpp-context-progression", ["CaseAggregate"])],
            {"1": "Case progression logic in cpp-context-progression, around CaseAggregate."},
        )
        code, output = self.run_verify()
        self.assertEqual(code, 0)
        self.assertNotIn("[unsupported]", output)

    def test_identifiers_from_source_paths_count_as_evidence(self):
        # a digest cites src/CaseAggregate.java; prose may name the file
        self.write(
            [self.digest("1", "CaseAggregate", "cpp-context-progression", ["CaseAggregate"])],
            {"1": "Progression logic in cpp-context-progression, see CaseAggregate.java."},
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


if __name__ == "__main__":
    unittest.main()
