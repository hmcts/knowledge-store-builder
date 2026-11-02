"""The required `tests` context's verdict over the unit job and the mutation gate.

Each check names the way the context could report green over a gate that did not
look: a failed shard, a shard that was cancelled or never started on a pull request,
and the unit job failing alone. The other half is the pass that must stay a pass -
a gate legitimately skipped on a schedule - because a verdict that fails there blocks
the nightly run for nothing. The module is driven through its `main`, with the
environment the workflow hands it.
"""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest import mock

import tests_verdict as verdict


def run(unit: str, gate: str, event: str) -> tuple[int, str]:
    environment = {"UNIT": unit, "GATE": gate, "EVENT": event}
    out = io.StringIO()
    with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(out):
        code = verdict.main()
    return code, out.getvalue()


class TheTestsContextTest(unittest.TestCase):
    def test_everything_green_on_a_pull_request_passes(self):
        """Catches the verdict failing the ordinary case, which would block every merge."""
        for event in ("pull_request", "push"):
            with self.subTest(event=event):
                self.assertEqual(run("success", "success", event)[0], 0)

    def test_a_failed_shard_fails_the_context(self):
        """Catches the gate blocking nothing: a shard that found a surviving mutation
        reports `failure` for the matrix, and the context must not read green."""
        code, text = run("success", "failure", "pull_request")
        self.assertEqual(code, 1)
        self.assertIn("mutation gate reported 'failure'", text)

    def test_a_cancelled_or_skipped_gate_fails_on_a_pull_request(self):
        """Catches a gate that never ran reading as satisfied. `skipped` is what a job
        that `needs` a failed one reports, and a cancelled matrix reports the same
        absence of a verdict."""
        for event in ("pull_request", "push"):
            for gate in ("cancelled", "skipped", ""):
                with self.subTest(event=event, gate=gate):
                    self.assertEqual(run("success", gate, event)[0], 1)

    def test_a_gate_skipped_where_it_is_not_wanted_passes(self):
        """Catches the verdict blocking the nightly and dispatched runs, where the
        mapping legs own the check and the gate job is correctly skipped."""
        for event in ("schedule", "workflow_dispatch"):
            with self.subTest(event=event):
                self.assertEqual(run("success", "skipped", event)[0], 0)

    def test_a_failed_unit_job_fails_the_context_even_with_the_gate_green(self):
        """Catches the verdict looking only at the gate, now that the unit tests live
        in a job of their own."""
        for event in ("pull_request", "schedule"):
            for unit in ("failure", "cancelled", "skipped", ""):
                with self.subTest(event=event, unit=unit):
                    self.assertEqual(run(unit, "success", event)[0], 1)

    def test_an_unrecognised_event_wants_the_gate(self):
        """Catches an event this module has never heard of, or an empty one, excusing a
        skipped gate: the asymmetry runs towards a red check somebody reads."""
        for event in ("", "merge_group", "workflow_run"):
            with self.subTest(event=event):
                self.assertEqual(run("success", "skipped", event)[0], 1)

    def test_a_failure_says_which_job_it_was(self):
        """Catches an unreadable red: both problems are named when both are present."""
        code, text = run("failure", "cancelled", "pull_request")
        self.assertEqual(code, 1)
        self.assertIn("unit job reported 'failure'", text)
        self.assertIn("mutation gate reported 'cancelled'", text)


if __name__ == "__main__":
    unittest.main()
