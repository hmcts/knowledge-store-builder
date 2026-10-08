"""`summaries verify` must show, on every run, that its comparison still discriminates.

A flagged rate on its own cannot say whether the check behind it works: if a field
rename or a digest shape change made the comparison vacuous, the rate would fall
and read as an improvement. So `verify` moves every summary onto the next community
id in memory, runs the same comparison, and reports what that produced beside the
real figure (#388).

Each test is a real store on disk and a real `verify` run; assertions land on the
printed report and the exit code. The break each test catches is named on it.
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


class RotationSelfCheckTest(SettingsIsolated):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        (self.root / "graphify-out").mkdir(parents=True)
        config.configure(root=str(self.root))

    def store(self, communities: dict[str, tuple[str, list[str]]]) -> None:
        """Community id -> (prose, the labels its digest holds)."""
        config.SUMMARIES_INPUT_PATH.write_text(
            json.dumps(
                [
                    {"id": int(cid), "top_nodes": [{"label": n} for n in labels]}
                    for cid, (_, labels) in communities.items()
                ]
            ),
            encoding="utf-8",
        )
        config.SUMMARIES_PATH.write_text(
            json.dumps({cid: prose for cid, (prose, _) in communities.items()}),
            encoding="utf-8",
        )

    def run_verify(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = summaries.verify()
        return code, out.getvalue(), err.getvalue()

    def test_a_working_check_reports_what_the_rotation_produced_beside_the_real_figure(self):
        """Breaks if the rotation is not run, is not reported, or fails to wrap.

        Three communities, each citing two identifiers only its own digest holds.
        As they stand none is flagged (0 of 3). Rotated 1->2, 2->3 and 3->1, every
        one cites identifiers its new digest lacks (3 of 3). If the last did not
        wrap onto the first the figure would read 2 of 3, so the wrap is held too.
        """
        self.store(
            {
                "1": ("AlphaService calls AlphaRepo.", ["AlphaService", "AlphaRepo"]),
                "2": ("BetaService calls BetaRepo.", ["BetaService", "BetaRepo"]),
                "3": ("GammaService calls GammaRepo.", ["GammaService", "GammaRepo"]),
            }
        )

        code, out, err = self.run_verify()

        self.assertIn(
            "Self-check: Of 3 summaries that cite an identifier, 0 (0%) are flagged as they "
            "stand and 3 (100%) when each is moved onto the next community's digest.",
            out,
        )
        self.assertNotIn("SELF-CHECK", err)
        self.assertEqual(code, 0)

    def test_a_check_that_stopped_discriminating_says_so_loudly_and_keeps_the_exit_code(self):
        """Breaks if a vacuous comparison reads as healthy, or if the failure
        changes the exit code.

        Every digest holds every identifier, which is what a comparison that
        matches everything looks like: nothing is flagged as it stands or rotated.
        The decision is that `verify` reports and never fails the run, so the exit
        code is the one a clean run returns.
        """
        shared = ["AlphaService", "AlphaRepo", "BetaService", "BetaRepo"]
        self.store(
            {
                "1": ("AlphaService calls AlphaRepo.", shared),
                "2": ("BetaService calls BetaRepo.", shared),
            }
        )

        code, out, err = self.run_verify()

        self.assertIn(
            "SELF-CHECK FAILED: Of 2 summaries that cite an identifier, 0 (0%) are flagged as "
            "they stand and 0 (0%) when each is moved onto the next community's digest.",
            err,
        )
        self.assertNotIn("Self-check:", out)
        self.assertEqual(code, 0)

    def test_fewer_than_two_communities_is_inconclusive_not_a_collapse(self):
        """Breaks if a single community, whose rotation moves nothing, is reported
        as either a pass or a failure of the check."""
        self.store({"1": ("AlphaService calls AlphaRepo.", ["AlphaService", "AlphaRepo"])})

        code, out, err = self.run_verify()

        self.assertIn(
            "SELF-CHECK INCONCLUSIVE: 1 community with a digest: a rotation needs at least two, "
            "and with fewer it moves nothing, so this run cannot show that the flagged rate "
            "above discriminates.",
            err,
        )
        self.assertNotIn("Self-check:", out)
        self.assertNotIn("SELF-CHECK FAILED", err)
        self.assertEqual(code, 0)

    def test_nothing_grounded_to_begin_with_is_inconclusive(self):
        """Breaks if a store whose prose is wrong on every community is reported as
        a discriminating check: with nothing grounded, a rotation has nothing to take
        away and a pass would be earned by no evidence."""
        self.store(
            {
                "1": ("AlphaService calls AlphaRepo.", ["BetaService", "BetaRepo"]),
                "2": ("BetaService calls BetaRepo.", ["AlphaService", "AlphaRepo"]),
            }
        )

        _, out, err = self.run_verify()

        self.assertIn(
            "SELF-CHECK INCONCLUSIVE: no summary that cites an identifier is grounded", err
        )
        self.assertNotIn("Self-check:", out)


if __name__ == "__main__":
    unittest.main()
