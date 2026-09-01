"""Decide whether the sharded mapping check needs to run, and say why in the summary.

`tests/mutation_gate.py --verify-mapping` applies every entry against the whole
suite. Sharded over four runners that is about seven minutes each, which is cheap
enough to run often and not cheap enough to run for nothing. Weekly was the
opposite problem: by the time a disagreement surfaced, the change that caused it
was a week of commits back and nobody could say which one it was (#285).

So it runs nightly, and immediately on a merge that lands the known way an
observer set goes stale - but only when something has landed that could have
invalidated one. That decision is made here, and it is asymmetric on purpose:

    A skip that should have run looks exactly like a pass.

No leg runs, the summary is green, and a mapping nobody checked keeps being
reported as checked. A run that should have skipped costs 28 runner-minutes and
tells the truth. Every uncertainty here therefore ends at `cannot_tell`, which
runs: no previous verification to compare against, a `gh api` that failed, a base
this clone does not hold, an event nothing knows how to compare. The verdict and
its reason go to the step summary every time, including when it skips, because a
decision nobody can read is a decision nobody can find wrong.

Two predicates, and the difference is the whole design:

- **A merge runs it immediately** only for what is both a known invalidator and
  attributable at that resolution: a test module added, renamed or deleted, or the
  table itself changed. That is how a correct observer set goes stale - a new test
  observes a defect an existing entry does not name - and on a merge somebody can
  still point at the commit.
- **The nightly predicate is the complete one**, and it is written as a deny-list:
  everything can invalidate a mapping unless it provably cannot. An allow-list
  fails the wrong way - a kind of file nobody thought of would read as harmless,
  silently and forever - and this one has to fail toward running.

The nightly is what covers source-only changes, and they are real invalidators
with no test touched: a new call site puts a test that never reached a mutated
line onto it, so it fails when the entry is applied and the entry does not name it;
a removed one takes a named observer off that line, so it stops failing. Both are
`--verify-mapping` disagreements produced by editing `src/` alone.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The mutation table. Changing it is changing a mapping claim, and the fast gate on
# the same push prints `caught` whether or not the claim is true.
TABLE = "tests/mutation_gate.py"

# The deny-list: paths no test in this repository reads and no table entry mutates,
# so nothing about them can move an observer set. Everything else runs the check.
#
# `.github/` is denied with one exception, and the exception is the point of the
# rule being written by hand rather than by directory: `tests.yml` pins the extras
# the suite installs, and about twenty tests skip or run depending on that pin -
# which changes what is available to observe a defect. The other files there
# configure linting, CodeQL and dependabot, none of which any test reads.
#
# Deliberately short. Docs and skills are *not* on it: entries in this table mutate
# a guide and a skill, and the tests observing them read those files, so prose is a
# mapping input here whatever it is elsewhere.
CANNOT_BE_READ = (".gitignore", ".gitattributes", "LICENSE")
THE_CI_FILE_TESTS_READ = ".github/workflows/tests.yml"

# A matrix leg reports as `mapping (1)`. A run that has these and succeeded on all
# of them verified the table; a run without them did not, however green it looks.
LEGS = "mapping ("

# One API call per run inspected, so the walk needs a bound: the answer is nearly
# always the most recent run that had legs, and an unbounded walk over a long
# history spends minutes deciding not to spend seven.
RUNS_LISTED = 60
RUNS_INSPECTED = 30


@dataclass(frozen=True)
class Change:
    """One path `git diff --name-status` reported, and what happened to it."""

    status: str
    path: str


@dataclass(frozen=True)
class Decision:
    """Whether to run the mapping check, and the sentence that explains it."""

    run: bool
    reason: str


def changes_from(text: str) -> tuple[Change, ...]:
    """Parse `git diff --name-status`.

    A rename arrives as `R100<TAB>old<TAB>new` and is split into a delete and an
    add, because that is what it is to the observer sets: the old test id is gone
    and a new one exists. Anything else keeps its first status letter, so `R100`
    and `M` are compared the same way.
    """
    parsed: list[Change] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 2 or not fields[0]:
            continue
        status = fields[0][0]
        if status in ("R", "C") and len(fields) >= 3:
            parsed.append(Change("D", fields[1]))
            parsed.append(Change("A", fields[2]))
        else:
            parsed.append(Change(status, fields[1]))
    return tuple(parsed)


def is_test_module(path: str) -> bool:
    """A file `unittest discover` would load as a test module."""
    return (
        path.startswith("tests/") and path.endswith(".py") and Path(path).name.startswith("test_")
    )


def can_be_read(path: str) -> bool:
    """True unless nothing in this repository's suite or table can read `path`."""
    if path == THE_CI_FILE_TESTS_READ:
        return True
    if path.startswith(".github/"):
        return False
    return path not in CANNOT_BE_READ


def new_or_departed_tests(changes: tuple[Change, ...]) -> tuple[str, ...]:
    """The paths that add or remove a test module, or change the table."""
    return tuple(
        sorted(
            {
                change.path
                for change in changes
                if change.path == TABLE
                or (change.status in ("A", "D") and is_test_module(change.path))
            }
        )
    )


def readable(changes: tuple[Change, ...]) -> tuple[str, ...]:
    """The paths that could change what any test observes."""
    return tuple(sorted({change.path for change in changes if can_be_read(change.path)}))


def named(paths: tuple[str, ...], most: int = 4) -> str:
    listed = ", ".join(paths[:most])
    return listed if len(paths) <= most else f"{listed} and {len(paths) - most} more"


def cannot_tell(why: str) -> Decision:
    """Every uncertainty ends here, and it runs the check.

    The one branch of this module that is never taken on a good day and never
    noticed when it is wrong, which is why it is a function with a name rather than
    a default at the end of a chain. Covered by the table entry `the mapping check
    fails toward skipping when it cannot tell`, which flips this `True` and requires
    `test_mapping_trigger` to notice.
    """
    return Decision(True, f"{why}, so nothing could be compared and it runs rather than guess.")


def decide(event: str, changes: tuple[Change, ...] | None, since: str) -> Decision:
    """Whether the mapping check needs to run, given what landed since `since`."""
    if event == "workflow_dispatch":
        return Decision(True, "it was dispatched by hand, which is the case sharding was for.")
    if changes is None:
        return cannot_tell(since)
    if event == "push":
        landed = new_or_departed_tests(changes)
        if landed:
            return Decision(
                True,
                f"a merge added, renamed or deleted a test module, or moved the table: "
                f"{named(landed)}. That is how a correct observer set goes stale, and at "
                f"this resolution the commit that did it can still be named. Compared "
                f"against {since}.",
            )
        return Decision(
            False,
            f"nothing since {since} added, renamed or deleted a test module or moved the "
            "table. An edited test or a source change can still invalidate a mapping, so "
            "this is not a claim that nothing did - the nightly run covers those, at one "
            "day's resolution rather than interrupting every merge.",
        )
    if event == "schedule":
        landed = readable(changes)
        if landed:
            return Decision(
                True,
                f"{len(landed)} path(s) this suite or table can read landed since {since}: "
                f"{named(landed)}.",
            )
        return Decision(
            False,
            f"nothing landed since {since} that any test or table entry in this repository "
            "reads, so no observer set can have moved.",
        )
    return cannot_tell(f"'{event}' is not an event this knows how to compare")


def _api(path: str, *, runner) -> object | None:
    """One `gh api` call, or None for anything that is not a JSON answer."""
    try:
        completed = runner(["gh", "api", path], cwd=ROOT, capture_output=True, text=True)
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None


def last_verified(repository: str, *, runner=subprocess.run) -> str | None:
    """The commit the most recent real verification ran against, or None.

    Read from the legs rather than from a run's own conclusion, because a run in
    which `mapping` was skipped is green and verified nothing. Treating one as a
    verification would ratchet the comparison forward from a check that never ran,
    and every night after it would skip for the same reason.

    None for every failure, which the caller turns into a run.
    """
    listing = _api(
        f"repos/{repository}/actions/workflows/tests.yml/runs?branch=main&per_page={RUNS_LISTED}",
        runner=runner,
    )
    if not isinstance(listing, dict):
        return None
    for run in list(listing.get("workflow_runs", []))[:RUNS_INSPECTED]:
        reported = _api(
            f"repos/{repository}/actions/runs/{run.get('id')}/jobs?per_page=100", runner=runner
        )
        if not isinstance(reported, dict):
            return None
        legs = [
            job for job in reported.get("jobs", []) if str(job.get("name", "")).startswith(LEGS)
        ]
        if legs and all(job.get("conclusion") == "success" for job in legs):
            return run.get("head_sha")
    return None


def changes_between(
    base: str, head: str = "HEAD", *, run=subprocess.run
) -> tuple[Change, ...] | None:
    """What landed between two commits, or None when git could not say.

    A base this clone does not hold - a shallow checkout, a force-pushed history -
    makes git exit non-zero, and that is not an empty diff. Reading it as one would
    be the silent skip this module exists to avoid.
    """
    try:
        completed = run(
            ["git", "diff", "--name-status", f"{base}..{head}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return changes_from(completed.stdout)


def base_for(event: str, *, runner=subprocess.run) -> tuple[str | None, str]:
    """The commit to compare against, and how to describe it in the summary."""
    if event == "push":
        before = os.environ.get("PUSHED_FROM", "")
        if not before or set(before) <= {"0"}:
            return None, "the push named no previous commit"
        return before, f"the commit the push moved main from ({before[:12]})"
    if event == "schedule":
        verified = last_verified(os.environ.get("GITHUB_REPOSITORY", ""), runner=runner)
        if not verified:
            return None, "no previous run of this workflow verified the mapping"
        return (
            verified,
            f"the commit the last successful verification ran against ({verified[:12]})",
        )
    return None, f"'{event}' names no commit to compare against"


def announce(decision: Decision) -> None:
    """Write the verdict where the matrix reads it and the reason where a human does."""
    sentence = (
        f"The sharded mapping check {'RUNS' if decision.run else 'is SKIPPED'}: {decision.reason}"
    )
    print(sentence)
    _append("GITHUB_STEP_SUMMARY", f"{sentence}\n")
    _append("GITHUB_OUTPUT", f"run={'true' if decision.run else 'false'}\n")


def _append(variable: str, text: str) -> None:
    """Append to the file Actions named, and do nothing when run outside it."""
    destination = os.environ.get(variable)
    if destination:
        with open(destination, "a", encoding="utf-8") as sink:
            sink.write(text)


def main(argv: list[str] | None = None, *, run=subprocess.run, runner=subprocess.run) -> int:
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    base, since = base_for(event, runner=runner)
    changes = changes_between(base, run=run) if base else None
    announce(decide(event, changes, since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
