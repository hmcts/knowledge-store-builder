"""A red nightly opens an issue, and a green one closes it.

The scheduled run is the only place two kinds of staleness in the mutation table
surface - a test that a `src/` change put onto a mutated line, and anything the
pull-request checks cannot read - and nothing told anyone when it went red. Four
episodes in one month sat red for one to five nights each: the defect was found
when somebody happened to open the Actions tab, by which time the change that
caused it was days of commits back.

So a scheduled run that ends red opens one issue, or comments on the one already
open, naming the jobs that failed, the entries the mapping legs disagreed about,
and the command that re-derives each. A scheduled run that ends green closes it.

It never re-derives anything itself. A named test that stopped failing can be a
test changed on purpose, and it can be a test that lost what it was checking;
only a reader can tell which, and an automatic re-derive would turn the second
into a green table over protection that is gone.

The decision and the wording live here rather than in the workflow for the reason
`mapping_trigger` gives: a branch written in shell is a branch nothing can test.
The tracker is reached through `gh`, behind the `runner` seam, so the suite drives
this module against a tracker it can inspect.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

# Exact, and searched for exactly: a looser match would comment on whatever issue
# happened to mention the nightly.
TITLE = "Nightly tests are red"

# What `toJSON(needs)` reports for a job that did its work. `skipped` is a pass
# here because the workflow already decides when a skip is a failure:
# `mapping-summary` reports a mapping leg that should have run and did not.
PASSING = frozenset({"success", "skipped"})

# The line `mutation_gate.py --verify-mapping` writes for each disagreement. Read
# from the leg's log, which carries an Actions timestamp before it and may carry
# colour codes around it.
WRONG_MAPPING = re.compile(r"WRONG MAPPING: (.+?)\s*$")
COLOUR = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class Disagreement:
    entry: str
    detail: str


def failed_jobs(results: dict[str, dict]) -> tuple[str, ...]:
    """The jobs that did not pass, sorted, from the workflow's `needs` context."""
    return tuple(sorted(job for job, need in results.items() if need.get("result") not in PASSING))


def disagreements(log: str) -> tuple[Disagreement, ...]:
    """Each `WRONG MAPPING` line in a leg's log, once, in the order the gate wrote them."""
    found: dict[tuple[str, str], Disagreement] = {}
    for line in COLOUR.sub("", log).splitlines():
        match = WRONG_MAPPING.search(line)
        if not match:
            continue
        entry, _, detail = match.group(1).partition(": ")
        found.setdefault((entry, detail), Disagreement(entry, detail))
    return tuple(found.values())


def report(failed: tuple[str, ...], found: tuple[Disagreement, ...], run_url: str, sha: str) -> str:
    """The issue body, or the comment on an issue already open."""
    lines = [
        f"The scheduled run at `{sha[:12]}` ended red: {run_url}",
        "",
        "Jobs that did not pass: " + ", ".join(f"`{job}`" for job in failed),
    ]
    if found:
        lines += ["", "Entries whose named observers disagree with the suite:", ""]
        lines += [f"- **{each.entry}**: {each.detail}" for each in found]
        lines += [
            "",
            "Re-derive each set, and read it before pasting it in. A test that is named",
            "and did not fail can have changed on purpose, or can have lost what it was",
            "checking; only the second is a regression, and re-deriving hides it.",
            "",
            "```bash",
            *(
                f"python3 tests/mutation_gate.py --derive-mapping --only '{entry}'"
                for entry in dict.fromkeys(each.entry for each in found)
            ),
            "```",
        ]
    elif any(job.startswith("mapping") for job in failed):
        lines += [
            "",
            "A mapping job failed without a `WRONG MAPPING` line this could read; see the",
            "run's logs.",
        ]
    return "\n".join(lines)


def _gh(arguments: list[str], *, runner) -> str:
    """One `gh` call. A failure raises: an alarm that cannot reach the tracker says so."""
    completed = runner(["gh", *arguments], capture_output=True, text=True)
    if completed.returncode != 0:
        raise SystemExit(f"gh {' '.join(arguments[:2])} failed: {completed.stderr.strip()}")
    return completed.stdout


def open_alarm(repository: str, *, runner) -> int | None:
    """The number of the open issue with exactly this title, or None."""
    listed = json.loads(
        _gh(
            [
                "issue",
                "list",
                "--repo",
                repository,
                "--state",
                "open",
                "--search",
                f'in:title "{TITLE}"',
                "--json",
                "number,title",
            ],
            runner=runner,
        )
    )
    numbers = sorted(issue["number"] for issue in listed if issue.get("title") == TITLE)
    return numbers[0] if numbers else None


def leg_logs(repository: str, run_id: str, *, runner) -> str:
    """The logs of this run's failed mapping legs, joined."""
    jobs = json.loads(
        _gh(["api", f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"], runner=runner)
    ).get("jobs", [])
    failing = sorted(
        (
            job
            for job in jobs
            if job.get("conclusion") == "failure" and job.get("name", "").startswith("mapping (")
        ),
        key=lambda job: job["name"],
    )
    return "\n".join(
        _gh(["api", f"repos/{repository}/actions/jobs/{job['id']}/logs"], runner=runner)
        for job in failing
    )


def main(argv: list[str] | None = None, *, runner=subprocess.run) -> int:
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    sha = os.environ.get("GITHUB_SHA", "")
    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repository}/actions/runs/{run_id}"
    failed = failed_jobs(json.loads(os.environ.get("RESULTS", "{}")))
    existing = open_alarm(repository, runner=runner)

    if not failed:
        if existing is None:
            print("The nightly is green and no alarm is open.")
            return 0
        _gh(
            [
                "issue",
                "close",
                str(existing),
                "--repo",
                repository,
                "--comment",
                f"The scheduled run at `{sha[:12]}` is green again: {run_url}",
            ],
            runner=runner,
        )
        print(f"The nightly is green again; closed #{existing}.")
        return 0

    body = report(failed, disagreements(leg_logs(repository, run_id, runner=runner)), run_url, sha)
    if existing is None:
        _gh(
            ["issue", "create", "--repo", repository, "--title", TITLE, "--body", body],
            runner=runner,
        )
        print("The nightly is red; opened an issue.")
    else:
        _gh(
            ["issue", "comment", str(existing), "--repo", repository, "--body", body], runner=runner
        )
        print(f"The nightly is still red; commented on #{existing}.")
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
