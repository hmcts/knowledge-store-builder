"""Which observer sets a merge may have staled, before the seven-minute check runs.

An entry in `tests/mutation_gate.py` names the tests that fail when it is applied.
A test that arrives later and observes the same defect makes that set
*under-describing*: the entry is unedited, uncontested and now wrong. The fast
gate cannot see it - it runs only the modules the entry names, so an unnamed
observer is by construction not run, and `caught` is printed over a set nobody
asked about (#293).

`--verify-mapping` is the check that asks, and it costs a whole-suite run per
entry: about seven minutes over four runners, which `mapping_trigger` spends
nightly and on a push to main. But a merge of `main` into a branch is where the
staleness is *created*, and that happens on the way into a pull request - so the
window between "the mapping became wrong" and "anything says so" runs to a day,
and four branches in one afternoon each needed the legs dispatched by hand to
find out which entries had gone stale.

This module closes that window with the one signal that is free: **which test ids
exist now that did not exist in the last state the mapping was verified in.** A
new observer has to be a test, and a test that did not exist cannot have been
named - so an entry naming a module that gained a test id is suspect by exactly
the mechanism #293 describes, and an entry naming only modules that gained
nothing cannot have been staled this way.

Three boundaries, and they are the whole claim it makes:

- It reads **test ids, not files.** Of the six sets the first sharded run found
  stale, five were staled by test *methods* arriving in classes and modules that
  already existed. A file-level signal flags those, but flags every module with a
  whitespace change too; the module-added-or-deleted predicate `mapping_trigger`
  uses for a push attributes none of the six, because no test module was added.
- It sees the **new-test half only.** A set also goes stale with no test touched:
  a new call site in `src/` puts a test that never reached a mutated line onto it,
  and the entry does not name it. That is the sixth of those six, and it is the
  nightly deny-list's half of the problem. A CLEAN verdict here therefore licenses
  one sentence - *no test that arrived since the base can have become an unnamed
  observer* - and not "the mapping is correct".
- It is **advisory.** Confirming a suspicion costs the seven minutes, and nothing
  here can do it cheaply, so there is no sound condition on which this could refuse
  a pull request by itself. `--refuse` exists for a maintainer who decides #293's
  option 3 is worth the trade; the default reports and names what to run.

Fail toward the expensive check, for the reason `mapping_trigger` does: a skip
that should have run looks exactly like a pass. Every uncertainty here - no base,
a base this clone does not hold, a module that will not parse - ends at
`cannot_tell` and is reported as suspicion, never as clean.

    python3 tests/observer_staleness.py                   # against the last verified main
    python3 tests/observer_staleness.py --base ORIG_HEAD   # against the branch before a merge
    python3 tests/observer_staleness.py --json            # the same, for a caller
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import mapping_trigger as trigger
import mutation_gate as gate

ROOT = Path(__file__).resolve().parent.parent

# The three answers, and the third is not a shade of the first. CLEAN means a
# question was asked and answered; CANNOT_TELL means it could not be asked, which
# is the one state that must never be rendered as the other.
CLEAN = "CLEAN"
SUSPECT = "SUSPECT"
CANNOT_TELL = "CANNOT_TELL"

# `unittest`'s own default prefix, which is what decides whether a method is
# collected and therefore whether it can observe anything. Hard-coded rather than
# read from a loader because the suite runs under the default.
TEST_PREFIX = "test"

# What `--verify-mapping` costs, named here because the remedy this prints has to
# be honest about the price of taking it.
LEG_MINUTES = 7

# Module name to the test ids that arrived in it since the base, as `Class.test`.
Arrivals = dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Suspect:
    """One entry, one module it names, and the tests that arrived in that module.

    `already_named` is what the entry does name there, and it is carried so the
    report can be read without opening the table: an entry naming eight tests in a
    module that gained one is a different-looking risk from an entry naming one.

    `same_class` is the sharp half of the signal and the reason ids are
    class-qualified. A test arriving in a class the entry already names is a test
    of the behaviour that entry describes - all five of the sets the first sharded
    run found stale *and* could be attributed to an arrival were of that shape.
    An arrival elsewhere in the module is the broader case, and it is kept rather
    than dropped because #293's sharpest instance was a wholly new class:
    class-only matching would have reported that merge clean.
    """

    entry: str
    module: str
    arrived: tuple[str, ...]
    already_named: tuple[str, ...]
    same_class: bool


@dataclass(frozen=True)
class Report:
    """The verdict, the sentence that explains it, and what to act on.

    `unattributed` is arrivals in modules no entry names. They cannot be pinned to
    an entry - a brand-new module is exactly how the sharpest instance in #293
    happened, and before that merge no entry named the module - so they widen the
    suspicion to the whole table rather than naming a row. Reported separately
    because "these three entries" and "the table" are different amounts of work.
    """

    verdict: str
    reason: str
    suspects: tuple[Suspect, ...] = ()
    unattributed: tuple[str, ...] = ()


def test_ids(source: str) -> frozenset[str]:
    """Every test a module defines, as `Class.test`.

    Class-qualified, because a class is the granularity a merge arrives at and two
    classes in one module routinely hold the same method name - `KeptEdgesTest`
    arriving next to an existing class with a `test_the_edges_are_sorted` of its own
    would be invisible to a bare method name.

    Raises `SyntaxError` for source it cannot parse, which the caller turns into
    `cannot tell`. Returning an empty set for an unparsable module would read as
    "nothing arrived here" - the silent-clean this module exists to prevent.

    Blind spot, stated because nothing in a passing run reveals it: a test method a
    class inherits from a base defined in *another* module is not visible in this
    parse. No base class in this suite provides one - they provide `setUp`
    isolation - and `test_observer_staleness` asserts that, so the blind spot
    cannot open without something failing.
    """
    return frozenset(
        f"{node.name}.{member.name}"
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef)
        for member in node.body
        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
        and member.name.startswith(TEST_PREFIX)
    )


def arrivals(before: frozenset[str] | None, after: frozenset[str]) -> tuple[str, ...]:
    """The ids `after` holds and `before` did not.

    `before` is None for a module the base does not hold, and then everything in it
    arrived: a new module is a whole set of tests no entry could have named.

    Only arrivals. A *departed* test is the opposite failure - an entry naming a
    test that no longer exists - and it is loud rather than silent: the gate reports
    `named and did not fail`, and `check_mapping` refuses outright when the whole
    module went. This looks for the quiet direction only.

    Sorted, because the report is compared and diffed and a set's iteration order
    is not stable across processes.
    """
    return tuple(sorted(after if before is None else after - before))


def modules_named(entry: gate.Mutation) -> tuple[str, ...]:
    """The test modules an entry's observers live in, deduplicated and sorted."""
    return tuple(sorted({gate.module_of(observer) for observer in entry.observers}))


def class_of(identifier: str) -> str:
    """The class part of a `Class.test` id, or of a `module.Class.test` observer."""
    parts = identifier.split(".")
    return parts[-2] if len(parts) >= 2 else ""


def suspicions(entry: gate.Mutation, arrived: Arrivals) -> tuple[Suspect, ...]:
    """The modules this entry names that gained a test it does not name.

    An arrival the entry already names is discounted, and that is what keeps the
    check from flagging its own remedy: a branch that adds a test and derives the
    set in the same change names the new test, so the entry comes back clear while
    every other entry naming that module is still reported.
    """
    found = []
    for module in modules_named(entry):
        here = tuple(o for o in sorted(entry.observers) if gate.module_of(o) == module)
        unnamed = tuple(
            arrival
            for arrival in arrived.get(module, ())
            if f"{module}.{arrival}" not in entry.observers
        )
        if unnamed:
            classes = {class_of(observer) for observer in here}
            found.append(
                Suspect(
                    entry.name,
                    module,
                    unnamed,
                    here,
                    any(class_of(arrival) in classes for arrival in unnamed),
                )
            )
    return tuple(found)


def unattributed(table: tuple[gate.Mutation, ...], arrived: Arrivals) -> tuple[str, ...]:
    """Arrivals in modules no entry names, as full `module.Class.test` ids.

    Not harmless and not attributable. `test_read_path_policy` arriving is the
    first instance in #293's table, and no entry named that module until the merge
    that brought it - so a rule that only looked at modules entries already name
    would have reported that merge clean.
    """
    covered = {module for entry in table for module in modules_named(entry)}
    return tuple(
        sorted(
            f"{module}.{arrival}"
            for module, ids in arrived.items()
            if module not in covered
            for arrival in ids
        )
    )


def cannot_tell(why: str) -> Report:
    """Every uncertainty ends here, and it reports suspicion rather than clean.

    The same rule as `mapping_trigger.cannot_tell` and for the same reason, one
    level along: there, an uncertainty that skipped would leave four legs unrun over
    a green summary; here, an uncertainty rendered as CLEAN would tell a reader that
    a table nothing could examine had been examined. Both are the failure that reads
    as a pass, so both run the expensive thing.
    """
    return Report(CANNOT_TELL, f"{why}, so no arrival could be compared and nothing is cleared.")


def closest_first(suspect: Suspect) -> tuple[int, str, str]:
    """Sort key: the same-class suspicions first, then by name.

    Explicitly tie-broken by name rather than left to the table's order, because
    the report is diffed between runs and two entries with the same tier must not
    swap places for a reason nothing in the tree explains.
    """
    return (0 if suspect.same_class else 1, suspect.entry, suspect.module)


def judge(table: tuple[gate.Mutation, ...], arrived: Arrivals | None, since: str) -> Report:
    """The verdict over a table, given what arrived since `since`."""
    if arrived is None:
        return cannot_tell(since)
    found = tuple(
        sorted(
            (suspect for entry in table for suspect in suspicions(entry, arrived)),
            key=closest_first,
        )
    )
    loose = unattributed(table, arrived)
    if not found and not loose:
        return Report(
            CLEAN,
            f"no test arrived since {since} in any module this table names, and none arrived "
            "anywhere else either, so no entry can have gained an observer it does not name. "
            "A set can still be stale because `src/` moved a test onto a mutated line without "
            "the test changing - the nightly run covers that half, and this says nothing "
            "about it.",
        )
    close = len({suspect.entry for suspect in found if suspect.same_class})
    wider = len({suspect.entry for suspect in found}) - close
    return Report(
        SUSPECT,
        f"{close} entry(s) gained a test in a class they already name, which is the shape all "
        f"five attributable instances in #293 took; {wider} more name only the module that "
        f"gained one; {len(loose)} test(s) arrived in modules no entry names at all. Compared "
        f"against {since}. Suspect is not stale - an arrival observes the entry's defect or it "
        "does not, and only a whole-suite run per entry can say which.",
        found,
        loose,
    )


def source_at(commit: str, path: str, *, run=subprocess.run) -> str | None:
    """One file as of one commit, or None when that commit does not hold it.

    None also covers a `git show` that failed for any other reason, and that
    conflation is safe in one direction only: the caller reads None as "the base
    did not have this module", which makes every test in it an arrival. That is the
    pessimistic reading. A git that is broken outright never reaches here, because
    `changes_between` has already returned None and the verdict is `cannot_tell`.
    """
    try:
        completed = run(
            ["git", "show", f"{commit}:{path}"], cwd=ROOT, capture_output=True, text=True
        )
    except OSError:
        return None
    return completed.stdout if completed.returncode == 0 else None


def changed_test_modules(base: str, head: str, *, run=subprocess.run) -> tuple[str, ...] | None:
    """The test modules that differ between two commits, or None when git could not say.

    Only the modules that differ are read, because a module identical at both ends
    cannot have gained a test - which turns a hundred `git show` calls into a
    handful. The parse and the diff both come from `mapping_trigger`, so a rename
    arrives here already split into the delete and the add it is to an observer set.
    """
    changes = trigger.changes_between(base, head, run=run)
    if changes is None:
        return None
    return tuple(sorted({change.path for change in changes if trigger.is_test_module(change.path)}))


def arrivals_between(
    base: str, head: str, modules: tuple[str, ...], *, run=subprocess.run
) -> Arrivals | None:
    """What arrived in each changed test module, or None when a module would not parse."""
    found: Arrivals = {}
    for path in modules:
        now = source_at(head, path, run=run)
        if now is None:
            # Gone at head. A module that does not exist holds no test that could
            # observe anything, and an entry still naming it fails `check_mapping`
            # rather than going quiet.
            continue
        was = source_at(base, path, run=run)
        try:
            after = test_ids(now)
            before = None if was is None else test_ids(was)
        except SyntaxError:
            return None
        arrived = arrivals(before, after)
        if arrived:
            found[Path(path).stem] = arrived
    return found


def what_arrived(base: str, head: str, *, run=subprocess.run) -> tuple[Arrivals | None, str]:
    """What arrived between two commits, and the sentence for when nothing could be read."""
    modules = changed_test_modules(base, head, run=run)
    if modules is None:
        return None, (
            f"git could not diff {base}..{head}, so this clone may not hold the base commit"
        )
    arrived = arrivals_between(base, head, modules, run=run)
    if arrived is None:
        return None, "a test module in the range could not be parsed"
    return arrived, ""


def repository(*, runner=subprocess.run) -> str:
    """The `owner/name` this checkout belongs to, or the empty string.

    From the environment inside Actions and from `gh` outside it, because the local
    run is the one that matters most here: the staleness is created by a merge on a
    developer's machine, and a check that only works in CI answers a day late.
    """
    named = os.environ.get("GITHUB_REPOSITORY", "")
    if named:
        return named
    try:
        completed = runner(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def base_for(chosen: str | None, *, runner=subprocess.run) -> tuple[str | None, str]:
    """The commit to compare against, and how to describe it.

    The default is the commit the last successful mapping legs ran against, read
    through `mapping_trigger.last_verified` rather than re-derived: that function
    already reads the matrix legs instead of a run's conclusion, because a run in
    which `mapping` was skipped is green and verified nothing. "Since the mapping
    was last known good" is the only base under which a clean answer means
    anything, and there is one definition of it.
    """
    if chosen:
        return chosen, f"the commit named on the command line ({chosen})"
    verified = trigger.last_verified(repository(runner=runner), runner=runner)
    if not verified:
        return None, "no run of this workflow has verified the mapping on main"
    return verified, f"the commit the last successful verification ran against ({verified[:12]})"


def verifying(names: Iterable[str]) -> str:
    """The `--verify-mapping` command for a set of entry names.

    `shlex.quote`, not an apostrophe of its own: seven entries in this table carry
    one in their name - `the page's edge list falls back to set order` among them -
    and a hand-quoted `--only '...'` turns the remedy this prints into a command
    that will not parse. A remedy nobody can paste is a remedy nobody runs.
    """
    only = " ".join(f"--only {shlex.quote(name)}" for name in sorted(names))
    return f"python3 tests/mutation_gate.py --verify-mapping {only}"


def remedies(report: Report) -> tuple[str, ...]:
    """The commands that settle the report, closest suspicion first.

    A verdict without one of these is not actionable: "run the seven-minute job" is
    a shrug, and the entry names are what turn it into a decision. Split by tier
    rather than joined, because the same-class entries are where the answer has
    been every time and they are the cheapest slice to run.
    """
    if report.verdict == CLEAN:
        return ()
    close = {suspect.entry for suspect in report.suspects if suspect.same_class}
    wider = {suspect.entry for suspect in report.suspects} - close
    commands = []
    if close:
        commands.append(verifying(close))
    if wider:
        commands.append(verifying(wider))
    if report.unattributed or report.verdict == CANNOT_TELL:
        commands.append(
            "python3 tests/mutation_gate.py --verify-mapping"
            f"  # the whole table, about {LEG_MINUTES} minutes over the four legs"
        )
    return tuple(commands)


def by_module(report: Report) -> tuple[tuple[str, tuple[str, ...], tuple[Suspect, ...]], ...]:
    """The suspects grouped by the module that gained the tests, closest tier first.

    Grouped because that is how the report is acted on. Nineteen entries naming
    `test_build_explorer` after one merge is one arrival and one decision, and
    printing it as nineteen lines that repeat the same six test names buries the
    two lines that are not that.
    """
    modules: dict[str, list[Suspect]] = {}
    for suspect in report.suspects:
        modules.setdefault(suspect.module, []).append(suspect)
    ordered = sorted(
        modules.items(), key=lambda pair: (closest_first(min(pair[1], key=closest_first)), pair[0])
    )
    return tuple(
        (module, suspects[0].arrived, tuple(sorted(suspects, key=closest_first)))
        for module, suspects in ordered
    )


def lines(report: Report) -> tuple[str, ...]:
    """The report as text, for a terminal and for the step summary alike."""
    written = [f"Observer staleness: {report.verdict} - {report.reason}"]
    for module, arrived, suspects in by_module(report):
        written.append(f"  {module} gained {len(arrived)} test(s): {trigger.named(arrived)}")
        close = [suspect.entry for suspect in suspects if suspect.same_class]
        wider = [suspect.entry for suspect in suspects if not suspect.same_class]
        if close:
            written.append(
                f"    {len(close)} entry(s) already name a test in a class that gained one: "
                f"{trigger.named(tuple(close), most=3)}"
            )
        if wider:
            written.append(
                f"    {len(wider)} entry(s) name only the module: "
                f"{trigger.named(tuple(wider), most=3)}"
            )
    if report.unattributed:
        written.append(
            f"  {len(report.unattributed)} test(s) arrived in modules no entry names, so no "
            f"entry can be ruled out: {trigger.named(report.unattributed)}"
        )
    written.extend(f"  $ {command}" for command in remedies(report))
    return tuple(written)


def as_dict(report: Report) -> dict[str, object]:
    """The report for a caller, with the same content the text carries."""
    return {
        "verdict": report.verdict,
        "reason": report.reason,
        "suspects": [
            {
                "entry": suspect.entry,
                "module": suspect.module,
                "arrived": list(suspect.arrived),
                "already_named": list(suspect.already_named),
                "same_class": suspect.same_class,
            }
            for suspect in report.suspects
        ],
        "unattributed": list(report.unattributed),
        "remedies": list(remedies(report)),
    }


def announce(report: Report, *, as_json: bool = False) -> None:
    """Print the report, and put it where Actions shows it and a matrix could read it."""
    print(json.dumps(as_dict(report), indent=2) if as_json else "\n".join(lines(report)))
    # `mapping_trigger`'s writer rather than a second one: two files that each
    # decide what an Actions variable is called are two files that can disagree
    # about it, and only one of them would be noticed.
    trigger._append("GITHUB_STEP_SUMMARY", "\n".join(lines(report)) + "\n")  # noqa: SLF001
    trigger._append(  # noqa: SLF001
        "GITHUB_OUTPUT", f"verdict={report.verdict}\n"
    )


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        metavar="REF",
        help="compare against REF instead of the last verified commit (`ORIG_HEAD` after a merge)",
    )
    parser.add_argument("--head", metavar="REF", default="HEAD", help="compare REF, default HEAD")
    parser.add_argument("--json", action="store_true", help="print the report for a caller")
    parser.add_argument(
        "--refuse",
        action="store_true",
        help="exit non-zero for anything but CLEAN, for a caller that blocks on it",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, run=subprocess.run, runner=subprocess.run) -> int:
    parsed = _arguments(argv)
    base, since = base_for(parsed.base, runner=runner)
    arrived, why = (None, since) if base is None else what_arrived(base, parsed.head, run=run)
    report = judge(gate.MUTATIONS, arrived, why or since)
    announce(report, as_json=parsed.json)
    return 1 if parsed.refuse and report.verdict != CLEAN else 0


if __name__ == "__main__":
    sys.exit(main())
