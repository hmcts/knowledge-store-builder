"""The answer gate: its refusals, and one real run against a built page.

The refusals matter as much as the pass. Every one of them is a case where the
stage could otherwise report success having asserted nothing - no question set, no
page, no Node - and a gate that passes vacuously is worse than no gate, because it
is read as evidence.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import check_answers, cli, config

FIXTURE_PAGE = Path(__file__).resolve().parent.parent / ".fixture-store/graphify-out/explorer.html"
FIXTURE_QUESTIONS = Path(__file__).resolve().parent / "explorer/fixtures/questions.txt"


class TheRunnerShips(unittest.TestCase):
    def test_the_runner_is_where_the_stage_looks_for_it(self):
        """It is read through `importlib.resources`, so a wheel that omitted it would
        fail only for a consumer, never here - unless this asserts the resolved path.
        """
        self.assertTrue(check_answers.runner_path().is_file(), check_answers.runner_path())

    def test_the_runner_sits_beside_the_scorer_it_drives(self):
        """Different directories would let the two come from different versions, and the
        whole point of this design is that the assertion drives the shipped scorer."""
        runner = check_answers.runner_path()
        self.assertTrue((runner.parent / "app.js").is_file())

    def test_the_stage_is_registered_and_parses_its_own_arguments(self):
        self.assertIn("check-answers", cli.STAGES)
        self.assertIn("check-answers", cli.SELF_PARSING)


class ItRefusesRatherThanPassVacuously(SettingsIsolated):
    def setUp(self) -> None:
        self.tmp = self.enterContext(__import__("tempfile").TemporaryDirectory())
        config.configure(root=self.tmp)

    def test_no_question_set_is_a_refusal(self):
        code = check_answers.main(["--candidate", str(FIXTURE_PAGE)])
        self.assertEqual(code, 2)

    def test_no_page_is_a_refusal(self):
        questions = Path(self.tmp) / "q.txt"
        questions.write_text("anything | graph\n")
        code = check_answers.main(["--questions", str(questions)])
        self.assertEqual(code, 2)

    def test_a_named_candidate_that_does_not_exist_is_a_refusal(self):
        questions = Path(self.tmp) / "q.txt"
        questions.write_text("anything | graph\n")
        code = check_answers.main(
            ["--candidate", str(Path(self.tmp) / "nope.html"), "--questions", str(questions)]
        )
        self.assertEqual(code, 2)

    def test_without_node_it_refuses_instead_of_reporting_success(self):
        original = shutil.which
        shutil.which = lambda name: None if name == "node" else original(name)
        try:
            code = check_answers.main(
                ["--candidate", str(FIXTURE_PAGE), "--questions", str(FIXTURE_QUESTIONS)]
            )
        finally:
            shutil.which = original
        self.assertEqual(code, 2)


class OneRealRun(unittest.TestCase):
    """A fixture page, driven all the way through Node.

    Node is required rather than skipped. This suite has been green over a fifth of
    itself never running, because an absent optional dependency turned twenty tests
    into silent skips - so the one test that proves this stage works end to end
    fails loudly if the toolchain is missing instead of quietly not running.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Build the fixture page if it is not there.

        Not a precondition on the caller. The unit suite runs before the explorer
        steps in CI, so requiring the page to exist first made this fail for a
        reason nothing in the log explained - and a developer running the suite on
        a fresh checkout would hit exactly the same thing.
        """
        if FIXTURE_PAGE.is_file():
            return
        builder = Path(__file__).resolve().parent / "explorer" / "fixture.py"
        subprocess.run([sys.executable, str(builder)], capture_output=True, text=True, check=True)

    def test_the_fixture_store_answers_its_declared_questions(self):
        self.assertTrue(
            FIXTURE_PAGE.is_file(),
            f"fixture page still absent after building it: {FIXTURE_PAGE}",
        )
        self.assertIsNotNone(shutil.which("node"), "Node is required by check-answers")
        completed = subprocess.run(
            [
                "node",
                str(check_answers.runner_path()),
                "--page",
                str(FIXTURE_PAGE),
                "--questions",
                str(FIXTURE_QUESTIONS),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        # Named quantities, so a report that measured nothing cannot read as a pass.
        self.assertIn("5 of 5 questions answered as declared", completed.stdout)
        for mode in ("brief 1/1", "dive 1/1", "graph 1/1", "ticket 1/1", "abstain 1/1"):
            self.assertIn(mode, completed.stdout)


if __name__ == "__main__":
    unittest.main()
