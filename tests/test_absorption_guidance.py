"""An operator must be told how to decide whether a local check can be deleted.

When a release absorbs something a store built for itself, the obvious move is to
delete the local copy — and it is wrong about half the time. Nothing in the docs
said so, which cost two real mistakes:

- A store was told two local checks were now redundant. Only one was; the other
  detected a condition the library still does not detect at all (#193).
- A store's refusing check was replaced in guidance by a library check that
  *reports*. Their build reads one graph file while six other scripts read the
  other, so a line on stderr in a run that exits 0 was invisible to them.

Both were caught by the operator verifying rather than accepting. This gate exists
because the guidance that prevents a third instance is prose, and prose has no
gate unless one is written — `CLAUDE.md` puts it as: a correction ships the check
that makes it durable.

It pins the decision content rather than the wording around it, so the section can
be rewritten without failing this, and cannot be gutted without failing it. The
extractor and the forge are `tests/doc_sections`, shared with the other gates over
prose; what stays here is this section's heading, its fragments and its assertions.
"""

from __future__ import annotations

import unittest

from doc_sections import Copy, body_of, collapsed, missing_elements, section_lines, sensitivity

# The three outcomes. Each is a distinct decision, and dropping any one of them
# leaves an operator with no answer for that case.
REQUIRED_DECISIONS = (
    "detects it and **refuses**",
    "detects it and **reports**",
    "detects a **narrower** case",
)

GUIDE = Copy(
    "docs/refreshing-a-store.md",
    "### Deciding whether a local check can go",
    REQUIRED_DECISIONS,
)


class AbsorptionGuidanceTest(unittest.TestCase):
    def setUp(self):
        self.section = body_of(GUIDE)

    def test_the_section_is_present(self):
        """Breaks if the heading is renamed or removed, which would leave every
        assertion below passing over an empty string."""
        self.assertGreater(
            section_lines(self.section),
            8,
            f"{GUIDE.path} has no {GUIDE.heading!r} section, or one too short to "
            "contain the guidance",
        )

    def test_all_three_decisions_are_stated(self):
        """Breaks if a row is dropped. Each is a distinct case, and an operator
        meeting the missing one has no answer."""
        absent = missing_elements(self.section, REQUIRED_DECISIONS)
        self.assertEqual(absent, [], f"the decision table no longer states: {absent}")

    def test_the_principle_is_stated(self):
        """Breaks if the table survives without the reason for it.

        A reader with three rules and no principle cannot decide a case the table
        does not list, and both real mistakes were cases the table would not have
        listed.
        """
        principle = collapsed(self.section.lower())
        self.assertIn("failure mode**, not the feature", principle)
        self.assertIn("not the same check", principle)

    def test_it_says_why_a_reporting_check_is_not_a_gate(self):
        """Breaks if the specific trap goes unstated.

        This is the one that actually happened: swapping a refusing check for a
        reporting one costs nothing on the day and everything on the day it
        matters, because a line on stderr in a run that exits 0 is indistinguishable
        from no line at all.
        """
        self.assertIn("exits 0", self.section)

    def test_it_says_to_verify_against_behaviour_not_a_summary(self):
        """Breaks if the instruction softens to "check the release notes".

        The mistake was made by whoever maintains the library, in a message to the
        operator — so the guidance has to say that a maintainer's summary is not
        the thing to verify against.
        """
        self.assertIn("summary of it", self.section)

    def test_this_gate_notices_a_dropped_decision(self):
        """The sensitivity check, in the same run.

        Removes each decision from the real section in turn and asserts the check
        reports that decision and only that decision. If this ever passes
        trivially, the assertions above are measuring the presence of a document
        rather than the three outcomes it has to distinguish.
        """
        report = sensitivity(self.section, REQUIRED_DECISIONS)
        self.assertEqual(
            report.already_missing,
            [],
            f"precondition: the section has already lost {report.already_missing}",
        )
        self.assertEqual(
            report.undetected,
            [],
            f"removing {report.undetected} from the decision table was not detected, "
            "so this gate is vacuous for those rows",
        )


if __name__ == "__main__":
    unittest.main()
