"""The one verdict over the mapping legs, and the two green states it has to separate.

`mapping-summary` reported plain `success` over four legs that never ran (#331). The
skip was correct and `mapping-decision` said so honestly in its own summary; the
summary job then produced a green check indistinguishable from the one a real
verification produces. Nothing in a checks list separated "every entry in the table
was applied against a whole-suite run and agreed" from "nothing looked at the
table", and a reader - or a chain deriving its check list from the workflow - takes
the green one for the first.

So the module under test has three states rather than two, and the checks here are
mostly about the middle one. Two of them are the fix, and they fail in opposite
directions:

- the skipped state must not read as verified - the defect;
- the verified state must not carry the disclaimer - the over-correction, which is
  as useless as the defect and looks identical to a fix. "Always say we did not
  check" distinguishes nothing either.

Both of those compare verdict words, so a third check holds the words pairwise
non-substring: `"VERIFIED" in text` is true of `"NOT VERIFIED"`, and a rewording
that reintroduced that would leave the two checks above passing over a summary that
had stopped distinguishing anything. That is the wrong-quantity shape this
repository keeps finding - correct code answering a neighbouring question.

The provenance is the rest. A skipped verdict names the commit the table was last
actually verified at and when, read through `mapping_trigger.last_verification`,
which reads the matrix legs rather than a run's conclusion - so the checks here
include the one that matters most for it: a run in which `mapping` was skipped is
green and verified nothing, and a provenance line that named such a run as the last
verification would be the bug reporting itself as the fix.

The boundary stubbed is `gh`, and only that: every check drives the real `state`,
the real `report` and the real `last_verification`.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mapping_summary as summary
import mapping_trigger as trigger


def api(replies: dict[str, object]):
    """A `gh api` that answers the paths named and 404s everything else.

    The IO boundary and nothing above it: the walk, the leg filter and the verdict
    are all real code in every check below.
    """

    def runner(command, **_kwargs) -> subprocess.CompletedProcess[str]:
        path = command[-1]
        for fragment, body in replies.items():
            if fragment in path:
                return subprocess.CompletedProcess(command, 0, json.dumps(body), "")
        return subprocess.CompletedProcess(command, 1, "", "not found")

    return runner


def runs(*described: tuple[int, str, str]) -> dict[str, object]:
    """A runs listing, newest first, each run with the commit and date it reports.

    Real-shaped commit ids, because the verdict abbreviates one to twelve
    characters and a fixture of `000...0007` would let a check pass against the
    abbreviation of any run in the listing.
    """
    return {
        "workflow_runs": [
            {"id": i, "head_sha": sha, "created_at": created} for i, sha, created in described
        ]
    }


def legs(*conclusions: str) -> dict[str, object]:
    """A run's jobs: the four matrix legs, plus the `tests` job every run has."""
    return {
        "jobs": [
            {"name": f"mapping ({n})", "conclusion": conclusion}
            for n, conclusion in enumerate(conclusions, start=1)
        ]
        + [{"name": "tests", "conclusion": "success"}]
    }


ALL_PASSED = ("success", "success", "success", "success")

# Two runs, newest first, as `(id, commit, date)`. Which of the two is the answer
# is the whole question in this module, so they differ in both halves.
NEWEST = (9, "4f21c0ab9d7e5613aa02bb44cc9910ee77fd3321", "2026-03-09T00:00:00Z")
OLDER = (8, "b7e30d1c8a4926f5510dd77ee3320bb99acc4412", "2026-03-08T00:00:00Z")
A_VERIFICATION = trigger.Verification("0123456789abcdef0123", "2026-01-02T03:04:05Z")


def rendered(condition: str, verification: trigger.Verification | None = None) -> str:
    """The verdict a reader sees, for a state, through the real renderer."""
    return "\n".join(summary.report(condition, "", "", verification))


class TheTwoGreenStatesAreNotTheSameStateTest(unittest.TestCase):
    """The defect, its over-correction, and the check that keeps both honest."""

    def test_legs_that_never_ran_do_not_read_as_verified(self):
        """The defect in #331. A decision of `false` with the legs skipped is a
        correct skip and still a pass, but it verified no entry - so it must not
        produce the verdict that says the table is checked as of this commit.

        Fails if `state` folds the skipped pair into `VERIFIED`, and fails if the
        skipped verdict is written with the verified verdict's words.
        """
        condition = summary.state("false", "skipped")

        self.assertEqual(condition, summary.NOT_LOOKED_AT)
        self.assertNotIn(summary.VERIFIED, rendered(condition, A_VERIFICATION))

    def test_legs_that_verified_the_table_say_so_without_a_disclaimer(self):
        """The over-correction guard, and it is the half that looks like a fix while
        being none. A summary that says "these legs did not run" whatever happened
        distinguishes the two states no better than one that always says success -
        and the run that did apply every entry against a whole-suite run has earned
        the verdict that says so.

        Fails if the disclaimer is unconditional, or if the verified verdict is
        rewritten to hedge.
        """
        condition = summary.state("true", "success")
        text = rendered(condition)

        self.assertEqual(condition, summary.VERIFIED)
        self.assertIn(summary.VERIFIED, text)
        self.assertNotIn(summary.NOT_LOOKED_AT, text)
        self.assertNotIn("did not run", text)

    def test_the_three_verdicts_cannot_be_confused(self):
        """What stops the two checks above going vacuous. They separate the states by
        their words, so a rewording that made one word a substring of another -
        `NOT VERIFIED` for the skipped state is the obvious one, and it is what the
        first draft of this said - would leave both passing over a summary that had
        stopped distinguishing anything.

        Fails on any such rewording, which is the only way this module's two
        assertions can become tautologies.
        """
        words = (summary.VERIFIED, summary.NOT_LOOKED_AT, summary.FAILED)

        self.assertEqual(len(set(words)), len(words), "two states share a word")
        for word in words:
            for other in words:
                if word is not other:
                    self.assertNotIn(word, other, f"{word!r} is readable inside {other!r}")


class TheSkippedVerdictCarriesItsProvenanceTest(unittest.TestCase):
    """What a run that read nothing says instead, so it is not merely green."""

    def test_it_names_the_commit_and_date_the_table_was_last_verified_at(self):
        """Without this the skipped state is a distinguishable verdict with nothing
        actionable in it: a reader knows this run checked nothing and has no way to
        tell whether the table was verified last night or three weeks ago, which is
        the question they are actually asking.

        Fails if the provenance is dropped from the verdict, or if either half of it
        is - the commit without the date names something unlocatable in time.
        """
        text = rendered(summary.NOT_LOOKED_AT, A_VERIFICATION)

        self.assertIn("0123456789ab", text)
        self.assertIn("2026-01-02T03:04:05Z", text)

    def test_it_says_it_cannot_tell_rather_than_printing_an_empty_date(self):
        """An unanswerable question rendered as a clean answer is the failure the
        whole trigger policy is built around, and a provenance line quietly missing
        its date is that failure wearing this fix's clothes: "the table was last
        verified at , by the run of " reads as a formatting bug rather than as the
        table being unverified.

        Fails if the absent case is rendered by the same template as the present one.
        """
        text = rendered(summary.NOT_LOOKED_AT, None)

        self.assertIn("cannot say when the table was last verified", text)
        self.assertNotIn("last verified at ,", text)

    def test_a_run_whose_legs_were_skipped_is_not_the_last_verification(self):
        """The one that matters most, driven through the real `last_verification`. A
        run in which `mapping` was skipped is green and verified nothing, so a lookup
        reading a run's own conclusion would name a run of exactly this kind as the
        last verification - the defect reporting itself as the fix, and it would
        ratchet forward from a check that never ran.

        Fails if the walk stops reading the legs.
        """
        found = trigger.last_verification(
            "owner/repo",
            runner=api(
                {
                    "workflows/tests.yml/runs": runs(NEWEST, OLDER),
                    "runs/9/jobs": {"jobs": [{"name": "tests", "conclusion": "success"}]},
                    "runs/8/jobs": legs(*ALL_PASSED),
                }
            ),
        )

        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.head_sha, found.ran_at), (OLDER[1], OLDER[2]))

    def test_the_date_and_the_commit_come_off_the_same_run(self):
        """Catches the two halves being read from different places, which is a
        provenance line that reads precisely and is wrong. The newest run is not a
        verification here, so both halves have to be the older one's.

        Fails if `ran_at` is taken from the listing's first entry, or from the
        wrong field of the right entry.
        """
        found = trigger.last_verification(
            "owner/repo",
            runner=api(
                {
                    "workflows/tests.yml/runs": runs(NEWEST, OLDER),
                    "runs/9/jobs": legs("success", "failure", "success", "success"),
                    "runs/8/jobs": legs(*ALL_PASSED),
                }
            ),
        )

        assert found is not None
        self.assertEqual((found.head_sha, found.ran_at), (OLDER[1], OLDER[2]))
        self.assertNotIn(NEWEST[2], rendered(summary.NOT_LOOKED_AT, found))

    def test_a_run_the_api_gave_no_date_for_says_so(self):
        """Catches an absent date rendering as an empty one. The commit is still
        worth naming, so this is not a `None` case - it is the one place a sentence
        has to be written for a field the API did not fill in.

        The commit is the other half, and it goes the other way: a run with no
        commit to name is no answer at all, so it is `None` rather than a
        `Verification` carrying an empty sha that the verdict would abbreviate to
        nothing. `last_verified` returned that empty string before this change and
        both its callers happened to treat it as absent, which is why the guard
        needs an observer of its own rather than their luck.
        """
        found = trigger.last_verification(
            "owner/repo",
            runner=api(
                {
                    "workflows/tests.yml/runs": {"workflow_runs": [{"id": 9, "head_sha": "abc"}]},
                    "runs/9/jobs": legs(*ALL_PASSED),
                }
            ),
        )

        assert found is not None
        self.assertEqual(found.ran_at, trigger.UNRECORDED)
        self.assertIn(trigger.UNRECORDED, rendered(summary.NOT_LOOKED_AT, found))

        nameless = api(
            {
                "workflows/tests.yml/runs": {"workflow_runs": [{"id": 9, "head_sha": ""}]},
                "runs/9/jobs": legs(*ALL_PASSED),
            }
        )

        self.assertIsNone(trigger.last_verification("owner/repo", runner=nameless))
        self.assertIsNone(trigger.last_verified("owner/repo", runner=nameless))

    def test_the_verified_verdict_does_not_offer_an_older_run_as_its_evidence(self):
        """Catches the provenance line escaping the state it belongs to. In the
        verified state the run that verified the table is *this* run, so naming the
        commit of the previous one would be a green check citing evidence from
        somewhere else - the same substitution as the defect, one run along.

        Fails if `report` renders the provenance for every state.
        """
        old = trigger.Verification("f" * 40, "2019-01-01T00:00:00Z")

        text = "\n".join(summary.report(summary.VERIFIED, "true", "success", old))

        self.assertNotIn("f" * 12, text)
        self.assertNotIn("2019-01-01T00:00:00Z", text)


class AFailedLegStillFailsTheSummaryTest(unittest.TestCase):
    """The half that already worked, pinned so the fix cannot have cost it."""

    def test_the_exit_code_for_every_decision_and_result_pair(self):
        """Catches the skipped case being made distinguishable by making every case
        pass. Only two pairs are a pass: the legs ran and agreed, or the decision
        said skip and they skipped. Everything else is a leg that did not pass or the
        wiring failing silently - a decision of `true` with the legs skipped is the
        matrix never having started, which verifies nothing and used to report green.

        Drives the real `main` with the values the runner supplies, so the mapping
        from those strings to an exit code is the shipped one.
        """
        for decision, result, expected in (
            ("true", "success", 0),
            ("false", "skipped", 0),
            ("true", "failure", 1),
            ("true", "cancelled", 1),
            ("true", "skipped", 1),
            ("false", "success", 1),
            ("", "skipped", 1),
        ):
            with self.subTest(decision=decision, result=result):
                with mock.patch.dict(
                    os.environ,
                    {"DECISION": decision, "RESULT": result, "GITHUB_REPOSITORY": "owner/repo"},
                    clear=True,
                ):
                    self.assertEqual(summary.main(runner=api({})), expected)


class TheVerdictReachesTheRunTest(unittest.TestCase):
    """End to end, through the file Actions renders."""

    def test_the_skipped_verdict_is_written_where_a_reader_of_the_run_sees_it(self):
        """Catches a verdict that is only ever printed to a step's log. The whole
        complaint in #331 is about what the run tells someone reading it, and the step
        summary is where that is read - a distinguishable sentence in stdout and a
        bare green check is the same defect with more words.

        Fails if `announce` stops writing the summary file, and fails if the
        provenance does not survive the trip.
        """
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        sink = Path(temporary.name) / "summary.md"
        sink.touch()

        with mock.patch.dict(
            os.environ,
            {
                "DECISION": "false",
                "RESULT": "skipped",
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_STEP_SUMMARY": str(sink),
            },
            clear=True,
        ):
            code = summary.main(
                runner=api(
                    {
                        "workflows/tests.yml/runs": runs(OLDER),
                        "runs/8/jobs": legs(*ALL_PASSED),
                    }
                )
            )

        written = sink.read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        self.assertIn(summary.NOT_LOOKED_AT, written)
        self.assertIn(OLDER[1][:12], written)
        self.assertIn(OLDER[2], written)


if __name__ == "__main__":
    unittest.main()
