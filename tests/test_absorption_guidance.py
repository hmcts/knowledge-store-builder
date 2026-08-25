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
be rewritten without failing this, and cannot be gutted without failing it.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "refreshing-a-store.md"

# The three outcomes. Each is a distinct decision, and dropping any one of them
# leaves an operator with no answer for that case.
REQUIRED_DECISIONS = (
    "detects it and **refuses**",
    "detects it and **reports**",
    "detects a **narrower** case",
)


def guidance(text: str) -> str:
    """The section, or "" if the heading is gone.

    Extracted rather than searched for across the whole document, so that a
    matching phrase appearing somewhere else cannot stand in for the section
    actually being present.
    """
    heading = "### Deciding whether a local check can go"
    if heading not in text:
        return ""
    after = text.split(heading, 1)[1]
    nxt = after.find("\n## ")
    return after[:nxt] if nxt != -1 else after


class AbsorptionGuidanceTest(unittest.TestCase):
    def setUp(self):
        self.text = GUIDE.read_text(encoding="utf-8")
        self.section = guidance(self.text)

    def test_the_section_is_present(self):
        """Breaks if the heading is renamed or removed, which would leave every
        assertion below passing over an empty string."""
        self.assertTrue(self.section, f"no absorption guidance found in {GUIDE.name}")
        self.assertGreater(
            len(self.section.strip().splitlines()),
            8,
            "the section is present but too short to contain the guidance",
        )

    def test_all_three_decisions_are_stated(self):
        """Breaks if a row is dropped. Each is a distinct case, and an operator
        meeting the missing one has no answer."""
        for decision in REQUIRED_DECISIONS:
            with self.subTest(decision=decision):
                self.assertIn(decision, self.section)

    def test_the_principle_is_stated(self):
        """Breaks if the table survives without the reason for it.

        A reader with three rules and no principle cannot decide a case the table
        does not list, and both real mistakes were cases the table would not have
        listed.
        """
        collapsed = " ".join(self.section.lower().split())
        self.assertIn("failure mode**, not the feature", collapsed)
        self.assertIn("not the same check", collapsed)

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

        Removes a decision from the real section text and asserts the check reports
        it. If this ever passes, the assertions above are measuring the presence of
        a document rather than its content.
        """
        forged = self.section.replace(REQUIRED_DECISIONS[0], "")
        missing = [d for d in REQUIRED_DECISIONS if d not in forged]
        self.assertEqual(
            missing,
            [REQUIRED_DECISIONS[0]],
            "removing a decision was not detected, so this gate is vacuous",
        )


if __name__ == "__main__":
    unittest.main()
