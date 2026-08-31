"""When the sharded mapping check needs to run, and the fail-safe that decides it.

Weekly was too long: by the time a mapping disagreement surfaced, the change that
caused it was a week of commits back and nobody could say which one it was. So it
runs nightly, and on a merge that lands the known way an observer set goes stale -
but only when something has landed that could have invalidated one, because
running four seven-minute legs over a day on which nothing landed teaches nobody
anything.

That makes the decision the new thing that can be silently wrong, and it is wrong
in only one direction that matters. **A skip that should have run looks exactly
like a pass**: no leg runs, the summary is green, and the mapping claim nobody
checked keeps being reported as checked. A run that should have skipped costs 28
runner-minutes and tells the truth. So every uncertainty in `mapping_trigger` ends
at `cannot_tell`, which runs - no previous verification to compare against, an API
that failed, a base this clone does not hold, an event nothing knows how to
compare - and this module pins each of those paths, because that is the branch
nobody exercises by accident.

The predicates are two, and the difference is deliberate. A merge runs the check
immediately only for the mechanism that is both known and attributable - a test
module added, renamed or deleted, or the table itself changed - because that is
what makes a correct observer set stale, and at merge resolution somebody can name
the commit. The nightly predicate is the complete one, and it is written as a
deny-list: everything can invalidate a mapping unless it provably cannot, so a
path nobody thought about runs the check rather than skipping it.

The checks here drive the real `decide`, the real `last_verified` over stubbed API
responses, and the real `changes_between` over a real git repository with real
commits - the boundary is stubbed, never the reasoning.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mapping_trigger as trigger

TABLE = "tests/mutation_gate.py"


def changes(*lines: str) -> tuple[trigger.Change, ...]:
    """What `git diff --name-status` printed, parsed by the real parser."""
    return trigger.changes_from("\n".join(lines) + "\n" if lines else "")


class WhatWarrantsARunTest(unittest.TestCase):
    """The two predicates, driven through the real `decide`."""

    def test_a_merge_that_adds_a_test_module_runs_immediately(self):
        """Catches the merge rule missing the mechanism it exists for. A new test
        module is how a correct observer set becomes stale: it can hold a test that
        observes a defect an existing entry does not name, and `--verify-mapping`
        reports that as the entry under-describing what protects it. Both of the
        disagreements this branch found were of exactly that shape."""
        decision = trigger.decide("push", changes("A\ttests/test_new_thing.py"), "the last push")

        self.assertTrue(decision.run, decision.reason)
        self.assertIn("tests/test_new_thing.py", decision.reason)

    def test_a_merge_that_renames_or_deletes_a_test_module_runs_immediately(self):
        """The other direction, and it is the louder one: an entry naming a test that
        no longer exists is `named and did not fail`, and if the whole module went,
        `check_mapping` refuses the next gate run outright. A rename is a delete and
        an add, which is why the parser splits it into both."""
        for line in ("D\ttests/test_gone.py", "R100\ttests/test_old.py\ttests/test_new.py"):
            with self.subTest(line=line):
                decision = trigger.decide("push", changes(line), "the last push")

                self.assertTrue(decision.run, decision.reason)

    def test_a_merge_that_changes_the_table_runs_immediately(self):
        """Catches the table falling out of the merge rule. An entry added, or an
        entry's `observers` edited, is a mapping claim nothing has checked - and the
        fast gate on that same push prints `caught` for it either way."""
        decision = trigger.decide("push", changes(f"M\t{TABLE}"), "the last push")

        self.assertTrue(decision.run, decision.reason)
        self.assertIn(TABLE, decision.reason)

    def test_a_merge_of_source_or_an_edited_test_waits_for_the_night(self):
        """The half that makes the merge rule worth having a nightly behind it.

        Both of these can invalidate a mapping - an edited test module can gain a
        test method, and a source change can move which existing tests reach a
        mutated line - so neither is safe to ignore. They are not run immediately
        because nearly every merge carries one, and 28 runner-minutes per merge buys
        a few hours over waiting for the night. The nightly predicate below is the
        one that has to be complete; this one is the sharp subset that earns
        interrupting for.
        """
        for line in ("M\tsrc/knowledgestore/io.py", "M\ttests/test_merge_layers.py"):
            with self.subTest(line=line):
                decision = trigger.decide("push", changes(line), "the last push")

                self.assertFalse(decision.run, decision.reason)
                self.assertIn("nightly", decision.reason)

    def test_the_nightly_runs_when_only_source_changed(self):
        """The argument this design turns on, so it is pinned rather than asserted in
        a comment. A source change alone can invalidate an observer set in both
        directions and touch no test: a new call site puts a test that never reached
        the mutated line onto it, and it fails when the entry is applied - `failed
        and is not named`; a removed one takes a named observer off that line, and it
        stops failing - `named and did not fail`. The nightly is what covers it."""
        decision = trigger.decide("schedule", changes("M\tsrc/knowledgestore/io.py"), "last night")

        self.assertTrue(decision.run, decision.reason)
        self.assertIn("src/knowledgestore/io.py", decision.reason)

    def test_the_nightly_runs_for_a_document_the_table_mutates(self):
        """Catches a predicate written for code. Entries in this table target
        markdown - a guide and a skill - and the tests observing them read those
        files, so prose is a mapping input here whatever it is elsewhere."""
        for line in ("M\tdocs/building-a-knowledge-store.md", "M\tskills/x/SKILL.md"):
            with self.subTest(line=line):
                self.assertTrue(trigger.decide("schedule", changes(line), "last night").run)

    def test_the_nightly_skips_when_nothing_it_can_read_landed(self):
        """The only skip that is safe, and the reason has to say so. Nothing landed
        that any test or table entry in this repository reads, so no observer set can
        have moved."""
        landed = changes(
            "M\t.github/workflows/lint.yml", "M\t.gitignore", "M\t.github/dependabot.yml"
        )

        decision = trigger.decide("schedule", landed, "the last verification")

        self.assertFalse(decision.run, decision.reason)
        self.assertIn("the last verification", decision.reason)

    def test_the_nightly_skips_when_nothing_landed_at_all(self):
        """The common case on a quiet day, and the one this policy exists to skip."""
        decision = trigger.decide("schedule", changes(), "the last verification")

        self.assertFalse(decision.run, decision.reason)

    def test_a_path_nobody_thought_about_is_read_as_one_that_matters(self):
        """Catches the predicate being written as an allow-list, which fails the wrong
        way: a kind of file that did not exist when the list was written would be read
        as unable to invalidate anything, silently, forever. A deny-list makes the
        unknown case run."""
        decision = trigger.decide("schedule", changes("A\tsomething/nobody/predicted.toml"), "then")

        self.assertTrue(decision.run, decision.reason)

    def test_the_workflow_pinning_the_extras_is_not_denied(self):
        """Catches `.github` being denied wholesale. This one file pins the extras the
        suite runs with, and 20 tests skip or run depending on it - which changes what
        can observe a defect."""
        decision = trigger.decide("schedule", changes("M\t.github/workflows/tests.yml"), "then")

        self.assertTrue(decision.run, decision.reason)

    def test_a_dispatch_runs_whatever_changed(self):
        """Somebody asking for it by hand is the case the sharding was for. It does not
        consult the predicates at all, so a dispatch cannot be talked out of running."""
        decision = trigger.decide("workflow_dispatch", changes(), "nothing")

        self.assertTrue(decision.run, decision.reason)


class FailTowardRunningTest(unittest.TestCase):
    """Every uncertainty runs the check. Named cases, because this is the branch that
    is never taken by accident and never noticed when it is wrong."""

    def test_nothing_to_compare_against_runs_the_check(self):
        for event in ("push", "schedule", "workflow_dispatch", "issue_comment"):
            with self.subTest(event=event):
                decision = trigger.decide(event, None, "no base could be established")

                self.assertTrue(decision.run, decision.reason)

    def test_an_event_nothing_knows_how_to_compare_runs_the_check(self):
        decision = trigger.decide("repository_dispatch", changes("M\tREADME.md"), "then")

        self.assertTrue(decision.run, decision.reason)


class LastVerifiedTest(unittest.TestCase):
    """The commit the last real verification ran against, over a stubbed API."""

    def _api(self, replies: dict[str, object]):
        """A `gh api` that answers from `replies`, and 1 for anything unasked."""
        calls: list[str] = []

        def runner(command, **_kwargs) -> subprocess.CompletedProcess[str]:
            path = command[-1]
            calls.append(path)
            for fragment, body in replies.items():
                if fragment in path:
                    return subprocess.CompletedProcess(command, 0, json.dumps(body), "")
            return subprocess.CompletedProcess(command, 1, "", "not found")

        return runner, calls

    @staticmethod
    def _runs(*ids: int) -> dict[str, object]:
        return {"workflow_runs": [{"id": i, "head_sha": f"sha{i}"} for i in ids]}

    @staticmethod
    def _legs(*conclusions: str) -> dict[str, object]:
        return {
            "jobs": [
                {"name": f"mapping ({n})", "conclusion": conclusion}
                for n, conclusion in enumerate(conclusions, start=1)
            ]
            + [{"name": "tests", "conclusion": "success"}]
        }

    def test_the_head_sha_of_the_newest_run_whose_legs_all_passed(self):
        """What the nightly compares against. The run has to have actually verified
        something, so the answer comes from the legs rather than from the run's own
        conclusion."""
        runner, _ = self._api(
            {
                "workflows/tests.yml/runs": self._runs(9, 8),
                "runs/9/jobs": self._legs("success", "success", "success", "success"),
            }
        )

        self.assertEqual(trigger.last_verified("owner/repo", runner=runner), "sha9")

    def test_a_run_whose_mapping_was_skipped_is_not_a_verification(self):
        """The defect this whole policy invites, and the one that would compound. A
        skipped run is green, so a comparison against its commit would treat
        everything before it as verified - and the skip would ratchet forward every
        night from a check that never ran."""
        runner, _ = self._api(
            {
                "workflows/tests.yml/runs": self._runs(9, 8),
                "runs/9/jobs": {"jobs": [{"name": "tests", "conclusion": "success"}]},
                "runs/8/jobs": self._legs("success", "success", "success", "success"),
            }
        )

        self.assertEqual(trigger.last_verified("owner/repo", runner=runner), "sha8")

    def test_a_run_with_one_failed_leg_is_not_a_verification(self):
        """Three quarters of the table verified is not the table verified, and the
        entries in the failed quarter are exactly the ones in question."""
        runner, _ = self._api(
            {
                "workflows/tests.yml/runs": self._runs(9, 8),
                "runs/9/jobs": self._legs("success", "failure", "success", "success"),
                "runs/8/jobs": self._legs("success", "success", "success", "success"),
            }
        )

        self.assertEqual(trigger.last_verified("owner/repo", runner=runner), "sha8")

    def test_an_api_that_fails_answers_nothing_rather_than_a_wrong_commit(self):
        """Catches an error swallowed into a plausible answer. `None` is what makes
        the caller run; a stale-but-real SHA would make it skip."""
        for replies in ({}, {"workflows/tests.yml/runs": self._runs(9)}):
            with self.subTest(replies=sorted(replies)):
                runner, _ = self._api(replies)

                self.assertIsNone(trigger.last_verified("owner/repo", runner=runner))

    def test_unparseable_json_answers_nothing(self):
        def runner(command, **_kwargs) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, "<html>rate limited</html>", "")

        self.assertIsNone(trigger.last_verified("owner/repo", runner=runner))

    def test_the_walk_is_bounded(self):
        """Catches a walk over every run this workflow has ever had: one API call per
        run, and the answer is nearly always in the first few. Unbounded, a repository
        with a long history spends minutes deciding not to spend seven."""
        runner, calls = self._api({"workflows/tests.yml/runs": self._runs(*range(1, 200))})

        self.assertIsNone(trigger.last_verified("owner/repo", runner=runner))
        self.assertLess(len(calls), 40, "the walk asked the API once per run in the history")


class ChangesFromARealRepositoryTest(unittest.TestCase):
    """`changes_between`, against real commits made by real git."""

    def _repository(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self._git(root, "init", "-b", "main")
        self._git(root, "config", "user.email", "gate@example.invalid")
        self._git(root, "config", "user.name", "gate")
        (root / "tests").mkdir()
        (root / "tests" / "test_first.py").write_text("first\n", encoding="utf-8")
        self._git(root, "add", "tests/test_first.py")
        self._git(root, "commit", "-m", "first")

        self.addCleanup(setattr, trigger, "ROOT", trigger.ROOT)
        trigger.ROOT = root
        return root

    @staticmethod
    def _git(root: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments], cwd=root, capture_output=True, text=True, check=True
        )
        return completed.stdout.strip()

    def test_what_landed_between_two_real_commits(self):
        """The parse and the command together, on output git actually produced: a
        hand-written fixture of `--name-status` proves nothing about the flags this
        passes."""
        root = self._repository()
        base = self._git(root, "rev-parse", "HEAD")
        (root / "tests" / "test_second.py").write_text("second\n", encoding="utf-8")
        (root / "tests" / "test_first.py").write_text("first, edited\n", encoding="utf-8")
        self._git(root, "add", "-A")
        self._git(root, "commit", "-m", "second")

        landed = trigger.changes_between(base)

        self.assertEqual(
            sorted((change.status, change.path) for change in landed or ()),
            [("A", "tests/test_second.py"), ("M", "tests/test_first.py")],
        )

    def test_a_base_this_clone_does_not_hold_answers_nothing(self):
        """The shallow-checkout and force-push case, and the reason it is not an
        exception: git exits non-zero and the caller runs the check. A shallow clone
        is the shape that produces it in CI, and it would otherwise be read as
        nothing having changed."""
        self._repository()

        self.assertIsNone(trigger.changes_between("0" * 40))


class TheDecisionIsAnnouncedTest(unittest.TestCase):
    """`main`, driven end to end over the files Actions actually reads."""

    def _actions(self, **environment: str) -> tuple[Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        output, summary = root / "output", root / "summary"
        output.touch()
        summary.touch()
        patched = mock.patch.dict(
            os.environ,
            {
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(summary),
                "GITHUB_REPOSITORY": "owner/repo",
                **environment,
            },
        )
        patched.start()
        self.addCleanup(patched.stop)
        return output, summary

    @staticmethod
    def _git_saying(text: str, code: int = 0):
        def run(command, **_kwargs) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, code, text, "")

        return run

    def test_a_run_is_written_as_an_output_and_explained_in_the_summary(self):
        """Catches the decision being reached and not carried: the matrix reads the
        output, and a step that decides correctly and writes nothing skips."""
        output, summary = self._actions(GITHUB_EVENT_NAME="push", PUSHED_FROM="a" * 40)

        code = trigger.main(run=self._git_saying("A\ttests/test_new.py\n"))

        self.assertEqual(code, 0)
        self.assertIn("run=true", output.read_text(encoding="utf-8"))
        self.assertIn("tests/test_new.py", summary.read_text(encoding="utf-8"))

    def test_a_skip_says_which_branch_was_taken_and_why(self):
        """A skip that does not explain itself is indistinguishable from a run nobody
        looked at. The summary has to name the comparison as well as the verdict."""
        output, summary = self._actions(GITHUB_EVENT_NAME="push", PUSHED_FROM="a" * 40)

        trigger.main(run=self._git_saying("M\tREADME.md\n"))

        self.assertIn("run=false", output.read_text(encoding="utf-8"))
        said = summary.read_text(encoding="utf-8")
        self.assertIn("skip", said.lower())
        self.assertIn("a" * 12, said, "the summary did not say what it compared against")

    def test_a_push_that_names_no_previous_commit_runs_anyway(self):
        """The fail-safe end to end. `github.event.before` is all zeros for the first
        push to a branch and after some force pushes, and a range starting there is
        not a range - so there is nothing to compare and the check runs."""
        output, summary = self._actions(GITHUB_EVENT_NAME="push", PUSHED_FROM="0" * 40)

        trigger.main(run=self._git_saying("", code=128))

        self.assertIn("run=true", output.read_text(encoding="utf-8"))
        self.assertIn("named no previous commit", summary.read_text(encoding="utf-8"))

    def test_a_nightly_with_no_previous_verification_runs_anyway(self):
        """The case on the first night after this lands, and after every failed run
        until one passes. Nothing to compare against means everything is unverified,
        which is the opposite of nothing to do."""
        output, summary = self._actions(GITHUB_EVENT_NAME="schedule")

        def api(command, **_kwargs) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "no runs")

        trigger.main(run=self._git_saying(""), runner=api)

        self.assertIn("run=true", output.read_text(encoding="utf-8"))
        self.assertIn("no previous run", summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
