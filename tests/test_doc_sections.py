"""The machinery three documentation gates share must be checked once, by name.

`doc_sections` extracts a section from a document so that the gates over `docs/`
and `skills/` pin a named section's content rather than the document's. Every
one of those gates asserts a *presence*, so all of them pass over `""` -- an
extractor that quietly returned nothing for every heading would leave three
green gates guarding nothing. The mutation gate cannot catch that: it mutates
`src/`, and this code is not there.

Each gate asserts its own section is present and longer than eight lines, so a
silently empty extractor fails all three against the real documents. What those
gates cannot say is *why*, and they cannot cover the cases their own documents
do not contain. Those are here, against synthetic documents whose expected
output is written out by hand:

- the body of the heading it names, and nothing past the next heading
- `""` when the heading is absent, which is the only way a caller learns a
  rename happened
- a `#` inside a fenced block is a shell comment, not a heading -- the real
  sections end in command blocks whose first line is a comment, so an extractor
  without this truncates them before the commands
- `sensitivity` reports a required element whose removal the checker cannot
  see, and concludes nothing from a section that is already broken
"""

from __future__ import annotations

import unittest

from doc_sections import (
    collapsed,
    commands,
    missing_commands,
    missing_elements,
    section,
    section_lines,
    sensitivity,
)

# One document with the shape the real ones have: a section ending in a fenced
# block whose first line is a comment, a deeper heading after it, and a sibling
# heading after that.
DOCUMENT = (
    "# Title\n"
    "\n"
    "### First\n"
    "alpha beta\n"
    "gamma\n"
    "\n"
    "```bash\n"
    "# not a heading\n"
    "cd somewhere\n"
    "```\n"
    "\n"
    "#### Second\n"
    "delta\n"
    "\n"
    "### Third\n"
    "epsilon\n"
)

# Written out rather than computed: a value derived with the code under test
# agrees with it whatever it does.
FIRST_BODY = "\nalpha beta\ngamma\n\n```bash\n# not a heading\ncd somewhere\n```\n"


class SectionTest(unittest.TestCase):
    def test_it_reads_the_named_heading_and_stops_at_the_next_one(self):
        """Breaks if the extractor runs past the section.

        An extractor that ran on would find every pinned fragment somewhere in
        the document and report green over a section that had been gutted.
        """
        self.assertEqual(section(DOCUMENT, "### First"), FIRST_BODY)
        self.assertNotIn("delta", section(DOCUMENT, "### First"))
        self.assertNotIn("epsilon", section(DOCUMENT, "### First"))

    def test_a_hash_inside_a_fenced_block_is_not_a_heading(self):
        """Breaks if fence tracking is dropped.

        The real sections end in a command block whose first line is a shell
        comment. Reading that as a heading truncates the section before the
        commands the gate is there to pin, and leaves it short enough to read as
        absent.
        """
        self.assertIn("cd somewhere", section(DOCUMENT, "### First"))

    def test_an_absent_heading_yields_empty(self):
        """Breaks if a rename stops being detectable.

        `""` is how every caller learns its heading is gone. An extractor that
        fell back to the whole document would hide the rename behind assertions
        that still passed, because every fragment is somewhere in the document.
        """
        self.assertEqual(section(DOCUMENT, "### Missing"), "")

    def test_section_lines_separates_an_absent_section_from_a_short_one(self):
        """Breaks if the length check stops distinguishing the two, which is
        what makes each gate's `> 8` bar mean anything."""
        self.assertEqual(section_lines(section(DOCUMENT, "### Missing")), 0)
        # `alpha beta`, `gamma`, the blank line, and the four lines of the block.
        self.assertEqual(section_lines(section(DOCUMENT, "### First")), 7)

    def test_commands_reads_the_fenced_lines_and_not_the_prose(self):
        """Breaks if `commands` starts reading prose, which would let a sentence
        mentioning a command stand in for the block that shows it."""
        self.assertEqual(commands(FIRST_BODY), "# not a heading\ncd somewhere")
        self.assertNotIn("alpha", commands(FIRST_BODY))
        self.assertNotIn("```", commands(FIRST_BODY))


class CollapsedTest(unittest.TestCase):
    def test_it_puts_one_space_between_words(self):
        """Breaks if a fragment stops matching across a wrapped line break,
        which is what leaves the documents free to be rewrapped."""
        self.assertEqual(collapsed("alpha\n  beta\tgamma\n"), "alpha beta gamma")

    def test_missing_elements_names_only_what_is_absent(self):
        """Breaks if the checker reports an element that is present, or misses
        one that is not. Either makes every gate built on it meaningless."""
        self.assertEqual(missing_elements("alpha\nbeta", ("alpha", "beta")), [])
        self.assertEqual(missing_elements("alpha", ("alpha", "beta")), ["beta"])

    def test_missing_elements_matches_an_element_that_wraps(self):
        """Breaks if matching goes back to the raw text. Every pinned fragment
        long enough to wrap in the document would then read as absent."""
        self.assertEqual(missing_elements("alpha\nbeta", ("alpha beta",)), [])

    def test_missing_commands_looks_only_inside_the_blocks(self):
        """Breaks if the command check starts reading prose, which is the whole
        reason it is separate: a sentence mentioning a command would then stand
        in for the block an operator copies."""
        self.assertEqual(missing_commands(FIRST_BODY, ("cd somewhere",)), [])
        self.assertEqual(missing_commands(FIRST_BODY, ("alpha",)), ["alpha"])

    def test_missing_commands_does_not_collapse_whitespace(self):
        """Breaks if commands start matching collapsed, which would accept
        `mkdir  -p` and `mkdir -p` as the same shell line."""
        self.assertEqual(
            missing_commands("```\ncd  somewhere\n```", ("cd somewhere",)),
            ["cd somewhere"],
        )


class SensitivityTest(unittest.TestCase):
    def test_it_reports_nothing_when_every_element_is_load_bearing(self):
        """The state the three gates assert: each element can be removed on its
        own, the removal is noticed, and nothing else goes with it."""
        report = sensitivity("alpha beta", ("alpha", "beta"))
        self.assertEqual(report.already_missing, [])
        self.assertEqual(report.undetected, [])

    def test_it_names_an_element_whose_removal_damages_another(self):
        """Breaks if the forge result stops being compared to the one element
        that was removed.

        `ph` is also inside `alpha`, so removing `ph` takes `alpha` with it and
        the checker reports two absences. Two is not evidence that either is
        pinned: the gate can no longer tell which element the document lost.
        """
        report = sensitivity("alpha ph", ("alpha", "ph"))
        self.assertEqual(report.already_missing, [])
        self.assertEqual(report.undetected, ["ph"])

    def test_it_concludes_nothing_from_an_already_broken_section(self):
        """Breaks if the precondition is dropped.

        Removing an element that is already absent is a no-op, so the checker
        "notices" the absence it started with and every forge passes. A section
        that has already lost an element has to fail on the precondition
        instead, which is the difference between a gate and decoration.
        """
        report = sensitivity("alpha", ("alpha", "beta"))
        self.assertEqual(report.already_missing, ["beta"])
        self.assertEqual(report.undetected, [])


if __name__ == "__main__":
    unittest.main()
