"""An identifier inside a descriptive label must be reachable (#303).

Semantic and document nodes are labelled with a phrase rather than a bare name —
a widget word, the field it is bound to, and the wording a user reads, in one
string. The separator class held no whitespace and no `=`, so the identifier in
the middle of such a label arrived in a segment with the prose either side of it
welded on, and `verify --estate` reported it absent from the graph while it sat
in the citing community's own node. Reported from a real store, where most of the
terms in the class the report tells an operator to act on turned out to be present
in node labels after all.

`name_segments`' own docstring makes the argument for fixing it: a check that
cries wolf on an entire ecosystem's naming convention gets switched off, and then
protects nothing.

**The widening is the whole risk, so half of this module points the other way.**
Substring matching would silence the reported case and the real findings with it
— against a corpus this size a substring of *something* is nearly always present
— so the tests below pin that a term must still match a **whole segment**: a
truncation of a real name stays absent, a word taken out of the middle of a
camel-case name stays absent, and an invented term stays absent. A change that
widened until everything matched would pass the first test here and fail those.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import EstateGraphIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402

# The shape the issue reports: a widget word, the identifier it is bound to, a
# constant, and the wording a user reads — one label, four kinds of string.
PHRASE_LABEL = "Checkbox deliveryWindow=NEXT_DAY ('Next day')"


class PhraseLabelSegmentTest(unittest.TestCase):
    def test_an_identifier_between_words_of_a_phrase_is_a_segment(self):
        """Breaks if the separator class loses whitespace or `=`.

        The reported defect: `deliveryWindow` arrived welded to the words either
        side of it, so nothing a summary could cite would ever match it.
        """
        self.assertIn("deliveryWindow", summaries.name_segments(PHRASE_LABEL))

    def test_a_constant_inside_a_phrase_is_offered_whole_and_in_parts(self):
        """Breaks if a word of a phrase is not itself segmented as a name.

        A word of a descriptive label is a name in its own right, so the rule that
        reaches `store` inside `@ngrx/store` has to reach `DAY` inside `NEXT_DAY`
        — and `NEXT_DAY` has to survive as a unit, because that is how the prose
        citing it spells it.
        """
        segments = summaries.name_segments(PHRASE_LABEL)
        self.assertIn("NEXT_DAY", segments)
        self.assertIn("DAY", segments)

    def test_brackets_and_quotes_separate_as_whitespace_does(self):
        """Breaks if only the two characters the issue names are added.

        The reported label wraps its user-facing wording in a parenthesis and two
        apostrophes, so a rule listing whitespace and `=` alone still leaves the
        words inside them glued to punctuation.
        """
        self.assertIn("Next", summaries.name_segments(PHRASE_LABEL))

    def test_a_camel_case_name_is_not_broken_into_its_words(self):
        """Breaks if the widening carries on into case transitions.

        Offering `delivery` and `Window` separately would corroborate a summary
        citing either against any label that merely mentions the other, which is
        the looseness the issue says is indistinguishable from substring matching.
        """
        segments = summaries.name_segments(PHRASE_LABEL)
        self.assertNotIn("delivery", segments)
        self.assertNotIn("Window", segments)

    def test_the_ecosystem_cases_the_rule_was_built_for_are_unchanged(self):
        """Breaks if the widening perturbs a bare identifier.

        A scoped package holds no phrase separator, so the wider class has to be a
        no-op on it — which is the argument for not needing a rule that first asks
        what kind of node a label came from.
        """
        self.assertEqual(summaries.name_segments("@ngrx/store"), {"ngrx", "store"})
        self.assertEqual(summaries.name_segments("a.b"), set())


class PhraseLabelEstateTest(EstateGraphIsolated):
    def assert_estate(self, labels, cited, *, absent, by_segment, why=""):
        """Build a graph of `labels`, cite `cited`, and assert both outcomes.

        One helper rather than the same four lines in each test: the tests below
        differ only in what they hand the estate and what they expect back, and
        the reason each exists lives in its docstring rather than in repeated
        setup. Both numbers are asserted every time — a term reported present
        while the by-segment counter stays at zero would mean the match came
        from somewhere this module is not describing.
        """
        self.write_graph(labels)
        found, matched = self._absent([("1", set(cited))])
        self.assertEqual(found, absent, why or "the absences are not the ones expected")
        self.assertEqual(matched, by_segment, "the by-segment count does not match the verdict")

    def test_an_identifier_in_a_phrase_label_no_longer_reads_as_absent(self):
        """Breaks if the reported false positive returns.

        The end of the reported path: a term in the citing community's own node,
        reported as something the store cannot speak about.
        """
        self.assert_estate(
            [PHRASE_LABEL],
            {"deliveryWindow"},
            absent={},
            by_segment=1,
            why="the estate holds this identifier inside a label",
        )

    def test_a_truncated_name_is_still_reported_absent(self):
        """Breaks if the widening becomes a substring match.

        A truncated identifier is the class of genuine defect this funnel exists
        to find, and it is precisely what substring matching cannot see:
        `deliveryWin` sits inside `deliveryWindow`, so a substring rule silences
        the only finding worth having.
        """
        self.assert_estate(
            [PHRASE_LABEL], {"deliveryWin"}, absent={"1": {"deliveryWin"}}, by_segment=0
        )

    def test_a_word_out_of_a_camel_case_name_is_still_reported_absent(self):
        """Breaks if the widening runs past segments into the words inside them.

        The estate names no `Window`; it names a `deliveryWindow`. Splitting on
        case as well would corroborate the first with the second, and a rule that
        loose reports almost nothing — worse than the false positives it removed,
        because the report would still look like a check.
        """
        self.assert_estate([PHRASE_LABEL], {"Window"}, absent={"1": {"Window"}}, by_segment=0)

    def test_an_invented_term_is_still_reported_absent(self):
        """Breaks if the looser match silences the finding entirely.

        The sensitivity half stated plainly: an estate full of phrase labels
        offers a great many segments, and a name it does not hold must still be
        reported against all of them.
        """
        self.assert_estate(
            [PHRASE_LABEL, "Button orderTracker ('Track your order')"],
            {"parcelDepot"},
            absent={"1": {"parcelDepot"}},
            by_segment=0,
        )

    def test_a_phrase_between_name_separators_still_corroborates(self):
        """Breaks if the wider split replaces the narrower one instead of adding.

        `Feature: My Widget` offers ` My Widget` today, so prose citing `MyWidget`
        is grounded. Splitting the phrase into its words and nothing else takes
        that away — a fix for false absences that quietly creates new ones.
        """
        self.assert_estate(["Feature: My Widget"], {"MyWidget"}, absent={}, by_segment=1)


if __name__ == "__main__":
    unittest.main()
