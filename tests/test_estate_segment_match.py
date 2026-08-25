"""A cited term must match a name segment, not only a whole label (#179).

`NgRx` was reported `[not in graph]` while the estate held `@ngrx/store`,
`@ngrx/effects` and six more scoped packages across 228 labels. A whole-label
match cannot match a scoped package name, and scoped names are the norm in JS/TS —
so the check cried wolf on an entire ecosystem's naming convention. A check that
does that gets switched off, and then it protects nothing.

**This deliberately loosens a check whose job is not lying**, so it trades false
positives for false negatives, which fail in the reassuring direction. Two things
keep that trade honest, and both are asserted here: the segment length floor, and
the count of terms matched only by a segment, reported on every run.

The count exists because the issue was explicit that a looser match had to be
measured against a real estate both ways. A one-off measurement on one estate
would not have travelled; a number printed wherever the check runs does.
"""

from __future__ import annotations

import contextlib
import io as io_module
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import config  # noqa: E402


class NameSegmentTest(unittest.TestCase):
    def test_a_scoped_package_yields_its_scope_and_name(self):
        """Breaks if the reported case stops working. `@ngrx/store` has to offer
        `ngrx`, because `NgRx` is what prose cites."""
        self.assertEqual(summaries.name_segments("@ngrx/store"), {"ngrx", "store"})

    def test_a_java_package_and_a_module_address_split_too(self):
        """Breaks if the rule is special-cased to one ecosystem.

        The issue predicted the same failure arriving from Java packages and
        Terraform module addresses; a segment rule covers those without a second
        special case, and that is the reason it was chosen over a scoped-package
        rule.
        """
        self.assertIn("example", summaries.name_segments("uk.gov.example.thing"))
        self.assertIn("vault", summaries.name_segments("module.key-vault"))

    def test_segments_below_the_floor_are_not_offered(self):
        """Breaks if the floor is removed, which is what stops this becoming a
        substring match in effect.

        Segments are short and common — `api`, `ui`, `db` — so without a floor the
        looser rule turns "the estate contains a segment spelled like your term"
        into "your term is corroborated".
        """
        self.assertEqual(summaries.name_segments("a.b"), set())
        self.assertNotIn("uk", summaries.name_segments("uk.gov.example"))


class AbsentFromEstateTest(SettingsIsolated):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "graphify-out").mkdir(parents=True)
        self._old_root = config.ROOT
        config.configure(root=str(self.root))

    def tearDown(self):
        config.configure(root=str(self._old_root))
        self._tmp.cleanup()
        super().tearDown()

    def write_graph(self, labels):
        config.GRAPH_PATH.write_text(
            json.dumps({"nodes": [{"id": f"n{i}", "label": v} for i, v in enumerate(labels)]}),
            encoding="utf-8",
        )

    def _absent(self, unsupported):
        err = io_module.StringIO()
        with contextlib.redirect_stderr(err):
            return summaries.absent_from_estate(unsupported)

    def test_a_scoped_package_no_longer_reads_as_absent(self):
        """Breaks if the reported false positive returns.

        This is the finding a reader takes as evidence that a summary cites
        something the estate does not contain, when the estate contains it eight
        times over.
        """
        self.write_graph(["@ngrx/store", "@ngrx/effects"])

        absent, by_segment = self._absent([("1", {"NgRx"})])

        self.assertEqual(absent, {}, "NgRx is present in the estate under a scoped name")
        self.assertEqual(by_segment, 1)

    def test_a_genuinely_absent_term_is_still_reported(self):
        """Breaks if the looser match silences the finding entirely.

        A rule that stops this check firing by matching everything is worse than
        the false positive it was meant to fix — that is the failure the issue
        warned about, and this is the test that would catch it.
        """
        self.write_graph(["@ngrx/store"])

        absent, by_segment = self._absent([("1", {"Kafka"})])

        self.assertEqual(absent, {"1": {"Kafka"}})
        self.assertEqual(by_segment, 0)

    def test_a_whole_label_match_is_not_counted_as_a_segment_match(self):
        """Breaks if the measurement overstates what the change bought.

        A counter that credits the looser rule for matches the strict rule already
        made would report success in the reassuring direction — the same shape as a
        counter this repository already had to correct for overstating.
        """
        self.write_graph(["Kafka"])

        absent, by_segment = self._absent([("1", {"Kafka"})])

        self.assertEqual(absent, {})
        self.assertEqual(by_segment, 0, "this matched a whole label, not a segment")

    def test_a_short_term_is_not_matched_against_a_segment(self):
        """Breaks if the floor is bypassed on the matching side.

        The floor has to hold in both places: a two-character cited term must not
        be corroborated by a two-character segment, or the floor only limits what
        the estate offers and not what a summary can claim.
        """
        self.write_graph(["uk.gov.example"])

        absent, _by_segment = self._absent([("1", {"uk"})])

        self.assertEqual(absent, {"1": {"uk"}})

    def test_the_finding_label_is_unchanged(self):
        """Breaks if the fix widens the label as well as the match.

        `[not in graph]` is deliberately narrower than `[not in estate]`, because
        the graph is narrower than the corpus. The issue says to fix the match
        without widening the claim.
        """
        source = Path(summaries.__file__).read_text(encoding="utf-8")
        self.assertIn("[not in graph]", source)
        self.assertNotIn("[not in estate]", source)


if __name__ == "__main__":
    unittest.main()
