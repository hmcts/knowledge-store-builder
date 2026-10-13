"""A red nightly opens one issue, a second red night comments on it, a green night closes it.

Driven through `nightly_alarm.main` against a tracker built here that answers the
`gh` calls the module makes: issues it holds, comments on them, and the jobs and
logs of one run. Everything downstream of the `runner` seam is the module's real
code, and every assertion reads the tracker afterwards - which issues are open,
what their bodies and comments say - rather than which calls were made.

The leg log is the format the Actions jobs-log API returns, copied from a real
failing night: a timestamp, the gate's indent, then the line. The unit-test
output that shares the log is there too, because its `SURVIVED:` lines are what a
looser reader would have picked up.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import nightly_alarm as alarm

REPOSITORY = "example-org/example-repo"
RUN = "4242"

LEG_LOG = (
    "2026-10-12T03:50:23.6383114Z test_a_named_test_that_cannot_see_the_mutation_marks_it_survived ... ok\n"
    "2026-10-12T03:50:23.7000000Z   SURVIVED: the target loses its value - not caught by "
    "test_bystander.Bystander.test_nothing_about_the_target - a purpose-built target\n"
    "2026-10-12T05:58:41.5417088Z   WRONG MAPPING: the summaries metadata block read as a community: "
    "test_ticket_titles_and_summaries.SummariesMergeTest.test_unknown_id_and_bad_length_are_rejected "
    "is named and did not fail\n"
    "2026-10-12T05:58:41.5421381Z 62 of 63 entries name what observes them.\n"
)

RED = {
    "tests": {"result": "success"},
    "mapping-decision": {"result": "success"},
    "mapping": {"result": "failure"},
    "mapping-summary": {"result": "failure"},
}
GREEN = {name: {"result": "success"} for name in RED} | {
    "observer-staleness": {"result": "skipped"}
}


class Tracker:
    """The part of GitHub `nightly_alarm` reaches, as state the checks can read."""

    def __init__(self, issues: dict[int, dict] | None = None, legs: dict[str, str] | None = None):
        self.issues = issues or {}
        self.legs = legs if legs is not None else {"mapping (2)": LEG_LOG}

    def __call__(self, command, capture_output=True, text=True):
        assert command[0] == "gh", command
        out = self._answer(command[1:])
        return subprocess.CompletedProcess(command, 0, out, "")

    def _answer(self, arguments: list[str]) -> str:
        verb = arguments[:2]
        if verb == ["issue", "list"]:
            return json.dumps(
                [{"number": n, "title": i["title"]} for n, i in self.issues.items() if i["open"]]
            )
        if verb == ["issue", "create"]:
            number = max(self.issues, default=0) + 1
            title = arguments[arguments.index("--title") + 1]
            body = arguments[arguments.index("--body") + 1]
            self.issues[number] = {"title": title, "body": body, "comments": [], "open": True}
            return f"https://github.com/{REPOSITORY}/issues/{number}\n"
        if verb == ["issue", "comment"]:
            self.issues[int(arguments[2])]["comments"].append(
                arguments[arguments.index("--body") + 1]
            )
            return ""
        if verb == ["issue", "close"]:
            issue = self.issues[int(arguments[2])]
            issue["comments"].append(arguments[arguments.index("--comment") + 1])
            issue["open"] = False
            return ""
        if arguments[0] == "api" and arguments[1].endswith("/jobs?per_page=100"):
            jobs = [
                {"id": 900 + i, "name": name, "conclusion": "failure"}
                for i, name in enumerate(self.legs)
            ]
            jobs.append({"id": 1, "name": "mapping (1)", "conclusion": "success"})
            return json.dumps({"jobs": jobs})
        if arguments[0] == "api" and arguments[1].endswith("/logs"):
            job = int(arguments[1].split("/")[-2])
            return list(self.legs.values())[job - 900]
        raise AssertionError(f"unexpected gh call: {arguments}")


class NightlyAlarmTest(unittest.TestCase):
    def _run(self, results: dict, tracker: Tracker) -> int:
        environment = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_RUN_ID": RUN,
            "GITHUB_SHA": "e557478aa1b2c3d4e5f6",
            "GITHUB_SERVER_URL": "https://github.com",
            "RESULTS": json.dumps(results),
        }
        with mock.patch.dict(os.environ, environment), mock.patch("builtins.print"):
            return alarm.main(runner=tracker)

    def _open(self, tracker: Tracker) -> list[dict]:
        return [issue for issue in tracker.issues.values() if issue["open"]]

    def test_a_red_night_opens_one_issue_naming_the_entry_and_its_remedy(self):
        """The feature. Catches the alarm not firing, firing without the entry, or
        sending the reader to the wrong command."""
        tracker = Tracker()

        code = self._run(RED, tracker)

        self.assertEqual(code, 0)
        [issue] = self._open(tracker)
        self.assertEqual(issue["title"], "Nightly tests are red")
        self.assertIn("the summaries metadata block read as a community", issue["body"])
        self.assertIn(
            "test_unknown_id_and_bad_length_are_rejected is named and did not fail", issue["body"]
        )
        self.assertIn(
            "python3 tests/mutation_gate.py --derive-mapping --only "
            "'the summaries metadata block read as a community'",
            issue["body"],
        )
        self.assertIn(f"https://github.com/{REPOSITORY}/actions/runs/{RUN}", issue["body"])
        self.assertIn("`mapping`", issue["body"])

    def test_the_unit_tests_own_survivor_lines_are_not_reported(self):
        """Catches a reader that takes `SURVIVED:` lines too. The suite's fixtures print
        one every night, for a purpose-built target, and an alarm quoting them would
        send the reader after an entry that does not exist."""
        tracker = Tracker()

        self._run(RED, tracker)

        [issue] = self._open(tracker)
        self.assertNotIn("the target loses its value", issue["body"])

    def test_a_second_red_night_comments_rather_than_opening_another(self):
        """Catches a duplicate issue per night - thirteen of them in the month this
        was written - which is the noise that gets an alarm muted."""
        tracker = Tracker(
            {7: {"title": "Nightly tests are red", "body": "first", "comments": [], "open": True}}
        )

        self._run(RED, tracker)

        self.assertEqual([7], [n for n, i in tracker.issues.items() if i["open"]])
        self.assertEqual(len(tracker.issues[7]["comments"]), 1)
        self.assertIn(
            "the summaries metadata block read as a community", tracker.issues[7]["comments"][0]
        )

    def test_an_issue_that_only_mentions_the_title_is_left_alone(self):
        """Catches a loose title match: the search is a substring search, so an issue
        about the nightly would otherwise collect the alarm's comments."""
        tracker = Tracker(
            {
                3: {
                    "title": "Why Nightly tests are red so often",
                    "body": "",
                    "comments": [],
                    "open": True,
                }
            }
        )

        self._run(RED, tracker)

        self.assertEqual(tracker.issues[3]["comments"], [])
        self.assertEqual(len(self._open(tracker)), 2)

    def test_a_green_night_closes_the_open_alarm(self):
        """Catches an alarm that only ever opens: a tracker full of red issues for
        nights long since fixed reads as a tracker nobody watches."""
        tracker = Tracker(
            {7: {"title": "Nightly tests are red", "body": "first", "comments": [], "open": True}}
        )

        self._run(GREEN, tracker)

        self.assertEqual(self._open(tracker), [])
        self.assertIn("green again", tracker.issues[7]["comments"][-1])

    def test_a_green_night_with_no_alarm_touches_nothing(self):
        """The control for the one above: green must not open anything."""
        tracker = Tracker()

        self._run(GREEN, tracker)

        self.assertEqual(tracker.issues, {})

    def test_a_skipped_job_is_not_a_red_night(self):
        """Catches `skipped` counted as failure. `observer-staleness` is skipped on
        every scheduled run by design, so that reading would open an issue nightly."""
        self.assertEqual(alarm.failed_jobs(GREEN), ())

    def test_a_red_job_outside_the_mapping_still_opens_the_issue(self):
        """Catches an alarm scoped to the mapping legs: the plain suite failing on a
        schedule is a red nightly too, with no `WRONG MAPPING` line to read."""
        tracker = Tracker(legs={})
        results = RED | {
            "tests": {"result": "failure"},
            "mapping": {"result": "success"},
            "mapping-summary": {"result": "success"},
        }

        self._run(results, tracker)

        [issue] = self._open(tracker)
        self.assertIn("`tests`", issue["body"])
        self.assertNotIn("--derive-mapping", issue["body"])

    def test_a_tracker_that_refuses_fails_the_job(self):
        """Catches the alarm failing silently: a revoked `issues: write` would leave
        every red night unreported behind a green alarm step."""

        def refusing(command, capture_output=True, text=True):
            return subprocess.CompletedProcess(
                command, 1, "", "HTTP 403: Resource not accessible by integration"
            )

        with self.assertRaises(SystemExit) as raised:
            self._run(RED, refusing)  # type: ignore[arg-type]

        self.assertIn("403", str(raised.exception))


try:  # PyYAML is the `deploy` extra, not a runtime dependency of this library
    import yaml

    HAS_YAML = True
except ImportError:  # pragma: no cover - the default-install CI job takes this path
    HAS_YAML = False

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "tests.yml"


@unittest.skipUnless(HAS_YAML, "needs the `deploy` extra (PyYAML)")
class TheAlarmJobIsWiredTest(unittest.TestCase):
    """The module is only as good as what the workflow hands it and when it runs."""

    def setUp(self):
        self.jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
        self.job = self.jobs["nightly-alarm"]

    def test_it_waits_on_every_other_job(self):
        """Catches a job added to the workflow and left out of `needs`: its failure
        would never reach `RESULTS`, and a night red only there would read green."""
        self.assertEqual(sorted(self.job["needs"]), sorted(set(self.jobs) - {"nightly-alarm"}))

    def test_it_runs_on_the_schedule_whatever_the_other_jobs_did(self):
        """Catches a dropped `always()`, which skips this job on exactly the nights it
        exists for, and a dropped event condition, which would close or open the
        alarm from a pull request."""
        condition = self.job["if"].replace(" ", "")
        self.assertIn("always()", condition)
        self.assertIn("github.event_name=='schedule'", condition)

    def test_it_can_read_the_legs_and_write_the_issue(self):
        """Catches a permission dropped in a tidy-up. Without them every call fails;
        the module fails loudly then, but only on a red night."""
        self.assertEqual(self.job["permissions"].get("issues"), "write")
        self.assertEqual(self.job["permissions"].get("actions"), "read")

    def test_it_hands_the_module_every_job_result(self):
        """Catches the step passing one job's result rather than all of them."""
        [step] = [s for s in self.job["steps"] if "nightly_alarm.py" in str(s.get("run", ""))]
        self.assertEqual(step["env"]["RESULTS"].replace(" ", ""), "${{toJSON(needs)}}")


if __name__ == "__main__":
    unittest.main()
