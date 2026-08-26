"""An author must be told which dashed terms the grounding check looks at.

`prose_identifiers` treats a dashed token as a claim about code when it has
three or more segments and a lowercase initial, and nothing an author could read
said so (#248). Measured on a real store, authors then rewrote correct English
defensively - most of it two-segment compounds the rule never looks at - and the
prose got worse for a rule that had not fired.

The check is deliberately unchanged. Every structural and lexical refinement was
measured against the store and rejected: segment count cannot separate English
from invention because both are exactly three segments, and dictionary
membership cannot either, because the estate's own identifiers are built from
ordinary English words. What remains is a documentation defect, so the fix is
prose - and prose has no gate unless one is written.

Two copies, because an author meets the rule twice and either alone leaves them
misinformed: the authoring brief in the build skill, read before writing, and the
report itself, read after. The report is where the temptation to assume the check
is wrong arises, so its copy is asserted on **stdout from a real run** rather
than in the source: a caveat in a string nothing reaches is the vacuity this
repository keeps finding.

The third property is the one that closes #248 rather than restating it: the
documented rule is pinned **against the code**. Every exempt example either copy
cites is run through `prose_identifiers` and must come back unflagged, and every
compound they cite as flagged either way must come back flagged. Documentation
that drifts from the check is the defect, so a gate that only reads the prose
would reproduce it.

The extractor and the forge are `tests/doc_sections`, shared with the other gates
over prose; what stays here is this rule's heading, its fragments and its
assertions.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from doc_sections import (
    Copy,
    body_of,
    missing_elements,
    section_after_rename,
    section_lines,
    sensitivity,
)
from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import config  # noqa: E402

# Each fragment is the shortest one that cannot survive its element being
# dropped, so both copies stay free to be rewritten in their own register while
# the rule they state does not.
REQUIRED = (
    # the shape, whose absence is the whole defect. Either half alone is a
    # different rule: the segment count without the case rule flags `JDBC-backed`
    "three or more segments",
    "lowercase initial",
    # worked exempt examples, so the boundary is concrete rather than described
    "same-named",
    "JDBC-backed",
    "end-to-end",
    "point-in-time",
    # that a three-segment lowercase compound is flagged either way, and why -
    # without the reason this reads as a defect to be fixed rather than a limit
    "identifier or ordinary English",
    "built from ordinary English words",
    # the operative instruction, and the point of the change: the original brief
    # lacked it, and that is what produced the defensive rewriting
    "rephrase it",
    "do not assume the check is wrong",
)

# Invented, and in the skill only: the report lists the terms it actually
# flagged, so naming a made-up pair beside them would be noise. Two tokens of
# one shape, one reading as an identifier and one as English, are what makes
# "flagged either way" concrete.
SKILL_ONLY = ("widget-record-created", "no-reason-supplied")

SKILL = Copy(
    "skills/knowledge-store-build/SKILL.md",
    "## Writing community summaries",
    REQUIRED + SKILL_ONLY,
)

# The terms the copies cite as exempt, which the check must genuinely not flag,
# and the terms they cite as flagged whichever they are, which it must flag.
# Written out rather than derived from REQUIRED so the assertion cannot be
# satisfied by an empty selection.
EXEMPT_EXAMPLES = ("same-named", "JDBC-backed", "end-to-end", "point-in-time")
FLAGGED_EXAMPLES = SKILL_ONLY


def caveat_line(output: str) -> str:
    """The one printed line carrying the rule, or "" if the report has lost it.

    Selected by the shape fragment rather than by an opening phrase, so the
    wording stays free to change. One line rather than the whole report for the
    reason `doc_sections.section` extracts rather than searches: a report long
    enough to state findings states most short fragments somewhere, so a gate
    reading all of it would pass over a caveat that had been gutted.
    """
    carrying = [line for line in output.splitlines() if "three or more segments" in line]
    return carrying[0] if len(carrying) == 1 else ""


class DashedRuleMatchesTheCodeTest(SettingsIsolated):
    """The documented boundary, run through the check it describes."""

    def test_every_exempt_example_is_genuinely_unflagged(self):
        """Breaks if a copy cites a term the check does in fact flag.

        This is the failure that created #248 in reverse: an author acting on an
        example that is wrong avoids a construction that was never at risk.
        """
        for token in EXEMPT_EXAMPLES:
            with self.subTest(token=token):
                self.assertEqual(
                    summaries.prose_identifiers(f"Handles {token} routing."),
                    set(),
                    f"{token} is documented as exempt and the check flags it",
                )

    def test_every_compound_documented_as_flagged_is_flagged(self):
        """Breaks if the pair illustrating "either way" stops illustrating it.

        Both are three lowercase segments and neither carries a joiner, so the
        check cannot tell the identifier-shaped one from the English one - which
        is the sentence's claim, and is only true while both are flagged.
        """
        for token in FLAGGED_EXAMPLES:
            with self.subTest(token=token):
                self.assertIn(
                    token,
                    summaries.prose_identifiers(f"Handles {token} routing."),
                    f"{token} is documented as flagged and the check ignores it",
                )


class DashedRuleInTheSkillTest(SettingsIsolated):
    """The copy an author reads before writing."""

    def setUp(self):
        self.section = body_of(SKILL)

    def test_the_section_is_present(self):
        """Breaks if the heading is renamed or removed, which would leave every
        assertion below passing over an empty string."""
        self.assertGreater(
            section_lines(self.section),
            8,
            f"{SKILL.path} has no {SKILL.heading!r} section, or one too short to brief an author",
        )

    def test_the_brief_states_the_rule_and_the_instruction(self):
        """Breaks if the brief loses the shape, the examples, the reason a
        three-segment compound is flagged either way, or the instruction to
        rephrase - the last being what the original brief lacked."""
        absent = missing_elements(self.section, SKILL.required)
        self.assertEqual(
            absent,
            [],
            f"{SKILL.path}'s {SKILL.heading!r} section no longer states: {absent}",
        )

    def test_this_gate_notices_a_dropped_element(self):
        """The sensitivity check, in the same run.

        Forges the real section with one required element removed and asserts the
        checker reports exactly that element. If this ever passes trivially, the
        assertion above is measuring the presence of a section rather than the
        rule it has to carry.
        """
        report = sensitivity(self.section, SKILL.required)
        self.assertEqual(
            report.already_missing,
            [],
            f"precondition: the brief has already lost {report.already_missing}, so the "
            "forges below remove nothing and conclude nothing",
        )
        self.assertEqual(
            report.undetected,
            [],
            f"the brief states {report.undetected} only incidentally: removing it is not "
            "reported as that element going missing, so it could leave the document "
            "unnoticed",
        )

    def test_this_gate_notices_a_removed_heading(self):
        """The other way the gate could go vacuous: the extractor finding nothing
        after a rename, and every content assertion passing over ""."""
        self.assertEqual(
            section_after_rename(SKILL),
            "",
            f"renaming {SKILL.heading!r} away in {SKILL.path} still yielded a section, so "
            "the assertions above may be reading a different one",
        )


class DashedRuleInTheReportTest(SettingsIsolated):
    """The copy an author meets after writing, asserted on a real run's stdout.

    Present-in-the-source is not the property worth having here. The rule matters
    at the moment a term is listed as a finding, so what is checked is that
    `verify` prints it then - in both the digest pass and the estate pass, since
    the estate pass is the one an operator reaches for when deciding whether a
    finding is real.
    """

    def _store(self, prose: str, graph_labels: list[str] | None = None) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "knowledge" / "summaries").mkdir(parents=True)
        (root / "graphify-out").mkdir(parents=True)
        config.configure(root=str(root))
        config.SUMMARIES_INPUT_PATH.write_text(
            json.dumps([{"id": "1", "top_nodes": [{"label": "AlphaService"}]}]),
            encoding="utf-8",
        )
        config.SUMMARIES_PATH.write_text(json.dumps({"1": prose}), encoding="utf-8")
        config.GRAPH_PATH.write_text(
            json.dumps({"nodes": [{"id": n, "label": n} for n in (graph_labels or [])]}),
            encoding="utf-8",
        )

    def _run(self, **kwargs) -> str:
        captured = io.StringIO()
        with redirect_stdout(captured):
            summaries.verify(**kwargs)
        return captured.getvalue()

    def test_the_digest_pass_prints_the_rule_and_the_instruction(self):
        """Breaks if the caveat stops reaching stdout - the call site removed, or
        the print moved behind a condition a plain run does not meet."""
        self._store("Covers AlphaService and the widget-record-created event.")
        printed = caveat_line(self._run())
        absent = missing_elements(printed, REQUIRED)
        self.assertEqual(
            absent,
            [],
            f"the printed report no longer states: {absent}",
        )

    def test_the_estate_pass_prints_it_too(self):
        """Breaks if the caveat is printed only when `absent` is None.

        The digest-sampling caveat it sits beside is in that branch, so a one-line
        move puts this one there as well - and silences it for exactly the run an
        operator makes when deciding whether a flagged term is a real finding.
        """
        self._store("Covers AlphaService and the widget-record-created event.")
        printed = caveat_line(self._run(estate=True))
        absent = missing_elements(printed, REQUIRED)
        self.assertEqual(absent, [], f"the estate pass no longer states: {absent}")

    def test_it_is_not_printed_when_nothing_dashed_was_flagged(self):
        """Breaks if the caveat becomes unconditional.

        A paragraph about dashed terms printed under findings that contain none is
        noise in the block the findings are in, and this report's history is that
        noise gets it ignored: the digest finding was renamed for the same reason.
        """
        self._store("Covers AlphaService and BetaWidget.")
        output = self._run()
        self.assertIn("BetaWidget", output, "precondition: something must be flagged")
        self.assertEqual(
            caveat_line(output),
            "",
            "the dashed-term rule printed under findings that hold no dashed term",
        )

    def test_this_gate_notices_a_dropped_element(self):
        """The sensitivity check, in the same run, over the line that was printed
        rather than over the source string that produced it."""
        self._store("Covers AlphaService and the widget-record-created event.")
        report = sensitivity(caveat_line(self._run()), REQUIRED)
        self.assertEqual(
            report.already_missing,
            [],
            f"precondition: the report has already lost {report.already_missing}",
        )
        self.assertEqual(
            report.undetected,
            [],
            f"the report states {report.undetected} only incidentally: removing it is not "
            "reported as that element going missing, so this gate is vacuous for it",
        )


if __name__ == "__main__":
    unittest.main()
