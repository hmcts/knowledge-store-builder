"""Each mutation-gate entry names the tests that observe it, and names the right ones.

`tests/mutation_gate.py` ran the whole suite for every entry: 1,447 tests, 8.7
seconds, 182 times, to observe a failure that one module could see. Running only
the modules an entry names takes that to about two minutes - but the mapping is
the point rather than the time. An entry that names its observer can be checked
by a later reader; a gate that claims to be sensitivity-checked cannot.

Named tests rather than named modules, and that is the load-bearing half. A
module fails for reasons an entry does not describe, so a run that concludes from
the module's verdict cannot tell an observation from a coincidence - and the
coincidence was sitting inside the gate: the guards over its own table fail for
every entry that targets its file, whatever the mutation did, so `caught` meant
"a guard noticed the file changed" for four entries whose only named module was
the one holding them (#274). The gate still runs whole modules, because a module
costs no more than one test of it; it asks afterwards whether the *named* tests
failed, and subtracts the guards it names in `TABLE_GUARDS` from every set it
derives or verifies.

The cost is a new way to be wrong, and it is the quiet kind. A wrong observer is
invisible from a passing run, because the gate prints `caught` either way:

- an entry naming nothing runs no test, and `unittest` reports an empty suite as
  a success - the worst outcome available here, a gate reporting a defect
  observed that it never looked for;
- an entry naming a module this tree does not hold loads a `_FailedTest`, which
  fails the run for the name being wrong rather than for the mutation;
- an entry naming a test that exists and does not observe it reports the entry
  as survived, which is loud - and is the direction this module pins hardest,
  because it is what proves the selection is real;
- an entry naming a guard over the table names something that is red whenever
  the file changes, which is the vacuous `caught` above.

So every check here drives the gate's own `sweep`, `verify_mapping`,
`derive_mapping` and `check_mapping` over a purpose-built tree: a source file to
mutate, a test module that reads it, one that does not, and - where the claim
needs them - a module holding a test that is red for its own reasons and one
standing in for a table guard. Real subprocesses, real discovery, the gate's real
code - the only thing constructed is the tree, because the claim is about which
tests a run reaches and which of them failed, and that is only observable from
the outcome of running them.

The last checks read the shipped table and the guard list instead, so an observer
or a guard that was renamed away is a failure in the suite rather than a `caught`
that proved nothing in a gate run somebody was waiting on.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import mutation_gate as gate

# Reads the file the mutation targets, so it fails while the mutation is applied
# and passes when it is not. The path is derived from its own location: the tree
# is built in a temporary directory whose name it cannot know.
WATCHER = """
import unittest
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "source" / "target.py"


class Watch(unittest.TestCase):
    def test_the_target_is_unmutated(self):
        self.assertIn("ORIGINAL", TARGET.read_text(encoding="utf-8"))
"""

# Names no observer of anything. An entry pointing here is the wrong-but-existing
# module case: it loads, it runs, it passes, and the mutation survives.
BYSTANDER = """
import unittest


class Bystander(unittest.TestCase):
    def test_nothing_about_the_target(self):
        self.assertEqual(1 + 1, 2)
"""

# Ends the child before it can report, the way a segfault or an OOM kill would.
# `os._exit` rather than `sys.exit`, which unittest would catch and turn into a
# failure it could report.
DIES = """
import os

os._exit(3)
"""

# Reads the target from inside `subTest`, which is how most of this suite asserts
# over a set of cases. unittest reports such a failure through a `_SubTest`, whose
# own module is unittest's rather than the test's - so a reporter that reads the
# module off the failing object attributes it to `runTest` and loses which module
# saw the defect. Deriving the shipped table produced that name 14 times.
SUBTEST_WATCHER = """
import unittest
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "source" / "target.py"


class WatchInSubTests(unittest.TestCase):
    def test_the_target_is_unmutated_in_every_case(self):
        for case in ("first", "second"):
            with self.subTest(case=case):
                self.assertIn("ORIGINAL", TARGET.read_text(encoding="utf-8"))
"""

# Two tests in one module: one that says nothing about the target, and one that is
# red whatever the target holds. The shape module granularity cannot read - the
# module fails, and only the named test can say whether anything observed the
# mutation.
NOISY = """
import unittest


class Noisy(unittest.TestCase):
    def test_nothing_about_the_target(self):
        self.assertEqual(1 + 1, 2)

    def test_red_for_its_own_reasons(self):
        self.assertEqual(1, 2, "red whether or not the target changed")
"""

# Stands in for the guards the gate names in `TABLE_GUARDS`: it reads the file the
# mutation rewrites, so it fails for every entry targeting that file, and what it
# asserts is about the table rather than about any behaviour. It observes the
# mutation in the sense that it goes red and in no sense that matters.
GUARD = """
import unittest
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "source" / "target.py"


class Guard(unittest.TestCase):
    def test_the_entry_still_names_one_site(self):
        self.assertEqual(TARGET.read_text(encoding="utf-8").count("ORIGINAL"), 1)
"""

ORIGINAL = 'VALUE = "ORIGINAL"\n'

# The tests in the fixtures above, named the way an entry names an observer:
# `module.Class.test`, which is what a run reports and what the verifier compares
# against.
WATCHES = "test_watcher.Watch.test_the_target_is_unmutated"
IGNORES = "test_bystander.Bystander.test_nothing_about_the_target"
WATCHES_IN_SUBTESTS = (
    "test_subtest_watcher.WatchInSubTests.test_the_target_is_unmutated_in_every_case"
)
QUIET_IN_A_NOISY_MODULE = "test_noisy.Noisy.test_nothing_about_the_target"
NOISE = "test_noisy.Noisy.test_red_for_its_own_reasons"
GUARDS_THE_TABLE = "test_guard.Guard.test_the_entry_still_names_one_site"


class MutationGateMappingTest(unittest.TestCase):
    def _tree(
        self, subtest_watcher: bool = False, noisy: bool = False, guard: bool = False
    ) -> Path:
        """A repository the gate can be pointed at: one source file, two test modules.

        `subtest_watcher` adds a third that observes the same mutation through
        `subTest`; `noisy` adds one holding a test that is red for its own
        reasons; `guard` adds one standing in for a table guard, and points
        `TABLE_GUARDS` at it. All three are opt-in because a module added here
        fails for every test that builds this tree, and the verifier checks the
        named set against everything that failed - so adding one unconditionally
        makes the other checks disagree about what should have been named.
        """
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "source").mkdir()
        (root / "source" / "target.py").write_text(ORIGINAL, encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_watcher.py").write_text(WATCHER, encoding="utf-8")
        (root / "tests" / "test_bystander.py").write_text(BYSTANDER, encoding="utf-8")
        if subtest_watcher:
            (root / "tests" / "test_subtest_watcher.py").write_text(
                SUBTEST_WATCHER, encoding="utf-8"
            )
        if noisy:
            (root / "tests" / "test_noisy.py").write_text(NOISY, encoding="utf-8")
        if guard:
            (root / "tests" / "test_guard.py").write_text(GUARD, encoding="utf-8")
            self.addCleanup(setattr, gate, "TABLE_GUARDS", gate.TABLE_GUARDS)
            gate.TABLE_GUARDS = (GUARDS_THE_TABLE,)

        self.addCleanup(setattr, gate, "ROOT", gate.ROOT)
        self.addCleanup(setattr, gate, "SRC", gate.SRC)
        self.addCleanup(setattr, gate, "RECOVERY_PATH", gate.RECOVERY_PATH)
        gate.ROOT = root
        gate.SRC = root / "source"
        gate.RECOVERY_PATH = root / "sidecar"
        return root

    @staticmethod
    def _entry(*observers: str) -> gate.Mutation:
        return gate.Mutation(
            "the target loses its value",
            "target.py",
            "ORIGINAL",
            "MUTATED",
            "a purpose-built target",
            observers,
        )

    def _sweep(self, mutation: gate.Mutation) -> tuple[int, str]:
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = gate.sweep((mutation,))
        return code, printed.getvalue()

    def _verify(self, mutation: gate.Mutation) -> tuple[int, str, str]:
        printed, reported = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(reported):
            code = gate.verify_mapping((mutation,))
        return code, printed.getvalue(), reported.getvalue()

    def _derive(self, mutation: gate.Mutation) -> tuple[int, list[str], str]:
        """The observers `--derive-mapping` would print for one entry, parsed.

        The JSON line rather than the progress line, because the JSON is what an
        author pastes into the table and an empty set has to be visible in it.
        """
        printed, reported = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(reported):
            code = gate.derive_mapping((mutation,))
        derived = json.loads(printed.getvalue().splitlines()[-1])
        return code, derived["observers"], reported.getvalue()

    def test_a_failure_inside_a_subtest_is_attributed_to_its_own_module(self):
        """Catches the reporter losing which module saw a subTest failure.

        Break it by deleting the `_SubTest` branch from `observer_of`: the failing
        object is then a `_SubTest`, whose own module is unittest's, so it falls
        through to `_testMethodName` and is attributed to `runTest`. The verifier
        compares what failed against what the entry names, so it reports
        `runTest failed and is not named` and refuses a mapping that is correct.

        Not cosmetic. Deriving the shipped table with that break named `runTest`
        14 times, and because the name is a single bucket it collapsed distinct
        modules into one: 6 real observers were hidden behind it, and every
        affected entry would have under-named what protects it.
        """
        self._tree(subtest_watcher=True)

        code, _, reported = self._verify(self._entry(WATCHES_IN_SUBTESTS, WATCHES))

        self.assertEqual(code, 0, f"a correct mapping was refused: {reported}")

    def test_a_module_failing_only_in_a_subtest_still_reads_as_failing(self):
        """The control for the test above, and it now shares its assurance.

        A subTest failure is reported alongside the case that contains it, so a
        module whose only failure is one could in principle be read as passing.
        This pins that it is not.

        It used to pass with the `_SubTest` branch removed, because attribution
        was wrong while the module still failed and the sweep read the module's
        verdict. The sweep now asks whether the *named* test failed, so a
        misattributed name is a name that did not fail and this reads SURVIVED as
        well. Two checks on one break rather than one, and neither is redundant:
        this drives the sweep every gate run uses and the one above drives the
        verifier that runs weekly.
        """
        self._tree(subtest_watcher=True)

        code, printed = self._sweep(self._entry(WATCHES_IN_SUBTESTS))

        self.assertEqual(code, 0, printed)
        self.assertIn("caught", printed)

    def test_an_entry_naming_no_observer_is_refused_before_anything_is_applied(self):
        """Catches the refusal being dropped from `check_mapping`. An entry with no
        observers runs no test at all, and an empty suite passes - which this gate
        prints as `caught`, a defect reported as observed by a run that looked for
        nothing. It is also where an entry lands once the table guards stop counting
        as observers: nothing behavioural was left to name (#274). Louder than the
        alternative on purpose: the entry has to be named, with the command that
        fills it in."""
        root = self._tree()
        unmappable = self._entry()

        with self.assertRaises(SystemExit) as raised:
            gate.sweep((unmappable,))

        self.assertIn("the target loses its value", str(raised.exception))
        self.assertIn("--derive-mapping", str(raised.exception))
        self.assertEqual((root / "source" / "target.py").read_text(encoding="utf-8"), ORIGINAL)
        self.assertFalse(
            (root / "sidecar").exists(), "the entry was applied despite being unmappable"
        )

    def test_an_observer_this_tree_does_not_hold_is_refused(self):
        """Catches the other half of the same hole. `unittest` turns a module it cannot
        import into a `_FailedTest`, so a renamed or misspelt observer fails the run for
        its own sake and the gate reports `caught` - about a mutation whose modules were
        never even loaded."""
        root = self._tree()
        departed = self._entry("test_departed.Gone.test_nothing_at_all")

        with self.assertRaises(SystemExit) as raised:
            gate.sweep((departed,))

        self.assertIn("test_departed", str(raised.exception))
        self.assertIn(str(root / "tests" / "test_departed.py"), str(raised.exception))

    def test_a_named_test_that_cannot_see_the_mutation_marks_it_survived(self):
        """The check that proves the selection is real, in both directions at once.
        `test_bystander` passes with the mutation applied, so the entry survives; if the
        gate were still running everything, `test_watcher` would fail in the same run
        and the entry would read as caught. Catches a `run_observers` that quietly falls
        back to the whole suite, and a wrong mapping being reported as a pass."""
        self._tree()

        code, printed = self._sweep(self._entry(IGNORES))

        self.assertEqual(code, 1, f"a mutation nothing named observed was reported: {printed}")
        self.assertIn("SURVIVED", printed)
        self.assertIn("0 of 1 mutations caught", printed)

    def test_a_named_test_that_observes_the_mutation_marks_it_caught(self):
        """The sensitivity control for the check above: a runner that failed every entry,
        or ran nothing and called it a failure, would pass that one and mean nothing.
        `test_watcher` reads the mutated file, so this is the whole gate in miniature."""
        root = self._tree()

        code, printed = self._sweep(self._entry(WATCHES))

        self.assertEqual(code, 0, f"the test that reads the mutated file did not fail: {printed}")
        self.assertIn("caught", printed)
        self.assertIn("1 of 1 mutations caught", printed)
        self.assertEqual((root / "source" / "target.py").read_text(encoding="utf-8"), ORIGINAL)

    def test_a_module_that_fails_for_a_reason_the_entry_does_not_name_is_not_caught(self):
        """Catches the catch criterion reading the module's verdict rather than the
        tests the entry names - the defect this granularity exists for.

        `test_noisy` holds a test that is red whatever the target says, and the
        entry names the one beside it that observes nothing. So the module fails
        and nothing observed the mutation. Restore `caught = not
        run_modules(mutation.observers)` and this reads as caught, which is how
        four entries targeting the gate's own file were reported as protected by
        the guard that fires whenever that file changes (#274).
        """
        self._tree(noisy=True)

        code, printed = self._sweep(self._entry(QUIET_IN_A_NOISY_MODULE))

        self.assertEqual(
            code, 1, f"a module red for its own reasons read as an observer: {printed}"
        )
        self.assertIn("SURVIVED", printed)
        self.assertIn("0 of 1 mutations caught", printed)

    def test_the_verifier_refuses_a_mapping_that_names_a_test_which_does_not_observe(self):
        """Catches the verifier going vacuous, which is the only way a wrong mapping
        becomes undetectable: a normal run cannot tell `caught` from `caught for the
        wrong reason`. Here the entry names the bystander, the whole tree is run, and
        the watcher fails without being named - so the entry's claim about what protects
        the behaviour is false in both directions."""
        self._tree()

        code, printed, reported = self._verify(self._entry(IGNORES))

        self.assertEqual(code, 1, f"a mapping naming the wrong test was accepted: {printed}")
        self.assertIn("DIFFERS", printed)
        self.assertIn(f"{WATCHES} failed and is not named", reported)
        self.assertIn(f"{IGNORES} is named and did not fail", reported)

    def test_the_verifier_disagrees_when_a_named_test_stops_failing_inside_a_module_that_does(self):
        """The property module granularity could not have, and the one the issue that
        prompted this asked for: deleting the test that observes a behaviour has to
        make the mapping disagree.

        The entry names the watcher, which fails, and a test in `test_noisy` that
        does not - the shape a renamed or deleted observer leaves behind, because
        the module it lived in still fails for something else. Compared as
        modules, `{test_watcher, test_noisy}` is named and `{test_watcher,
        test_noisy}` failed, and the verifier agrees with a mapping that describes
        protection nothing provides (#274).
        """
        self._tree(noisy=True)

        code, printed, reported = self._verify(self._entry(WATCHES, QUIET_IN_A_NOISY_MODULE))

        self.assertEqual(code, 1, f"a named test that did not fail was accepted: {printed}")
        self.assertIn("DIFFERS", printed)
        self.assertIn(f"{NOISE} failed and is not named", reported)
        self.assertIn(f"{QUIET_IN_A_NOISY_MODULE} is named and did not fail", reported)

    def test_an_entry_disagreeing_in_both_directions_is_counted_once(self):
        """Catches the summary tallying complaints rather than the entries it names.

        One entry raises a message in each direction, so subtracting the message
        list from the entry count answers a different question than the sentence
        asks. Break it by restoring `len(mutations) - len(wrong)`: a population
        of one entry then reports `-1 of 1`, a count below zero.
        """
        self._tree()

        _, printed, _ = self._verify(self._entry(IGNORES))

        self.assertIn("0 of 1 entries name what observes them", printed)

    def test_the_verifier_accepts_the_mapping_that_matches_what_failed(self):
        """The sensitivity control for the verifier: one that refused every entry would
        pass the check above and could not distinguish a right mapping from a wrong one."""
        self._tree()

        code, printed, reported = self._verify(self._entry(WATCHES))

        self.assertEqual(code, 0, f"a mapping that matches the failures was refused: {reported}")
        self.assertIn("agrees", printed)
        self.assertIn("1 of 1 entries name what observes them", printed)

    def test_a_table_guard_is_left_out_of_a_derived_observer_set(self):
        """Catches the exclusion being dropped from `derive_mapping`. `test_guard`
        stands in for a guard over the table: it goes red because the file changed,
        which is not an observation of anything, and it went into the table as one
        for nine entries (#274). The watcher in the same run is the sensitivity
        half - an exclusion that dropped everything would leave a table of empty
        tuples and pass a check that only looked for the guard's absence."""
        self._tree(guard=True)

        code, observers, reported = self._derive(self._entry(WATCHES))

        self.assertEqual(code, 0, f"an entry a test observes was refused: {reported}")
        self.assertEqual(observers, [WATCHES], "the guard was derived as an observer")

    def test_an_entry_only_a_table_guard_observes_derives_as_observed_by_nothing(self):
        """Catches an entry with no behavioural protection reading as protected, which
        is the finding the issue behind this reported: four entries whose only named
        module was the one holding the guard. With the watcher deleted the guard is
        the only thing that fails, so the derived set has to be empty and the run has
        to refuse rather than print a tuple somebody would paste in."""
        root = self._tree(guard=True)
        (root / "tests" / "test_watcher.py").unlink()

        code, observers, reported = self._derive(self._entry())

        self.assertEqual(code, 1, "an entry only a table guard observed was derived as protected")
        self.assertEqual(observers, [], f"the guard was derived as an observer: {observers}")
        self.assertIn("UNOBSERVED", reported)
        self.assertIn("the target loses its value", reported)

    def test_an_entry_naming_a_table_guard_is_refused_before_anything_is_applied(self):
        """Catches the refusal missing from `check_mapping`, which is what stops a
        derived-and-then-hand-edited table putting a guard back. A guard is red
        whenever its file changes, so an entry naming one is caught on every run and
        protected on none - and the refusal has to leave the tree alone, because an
        entry rejected half-applied is a defect left in the working copy."""
        root = self._tree(guard=True)

        with self.assertRaises(SystemExit) as raised:
            gate.sweep((self._entry(GUARDS_THE_TABLE),))

        self.assertIn("table guard", str(raised.exception))
        self.assertIn(GUARDS_THE_TABLE, str(raised.exception))
        self.assertIn("--derive-mapping", str(raised.exception))
        self.assertEqual((root / "source" / "target.py").read_text(encoding="utf-8"), ORIGINAL)
        self.assertFalse((root / "sidecar").exists(), "a refused entry was applied anyway")

    def test_a_suite_run_that_reported_nothing_is_refused(self):
        """Catches a run whose child never reported being read as a pass or a fail. Both
        are conclusions about a suite that did not run: as a pass it makes every entry
        survive, and as a fail it makes every entry caught - the second silently."""
        root = self._tree()
        (root / "tests" / "test_dies.py").write_text(DIES, encoding="utf-8")

        with self.assertRaises(SystemExit) as raised:
            # The class and method are invented: the module exits at import, so it
            # holds no test, and only the module part of an observer selects what
            # is run.
            gate.run_observers(["test_dies.NeverLoads.test_nothing_at_all"])

        self.assertIn("reported nothing", str(raised.exception))
        self.assertIn("exit 3", str(raised.exception))

    def test_every_shipped_entry_names_tests_in_modules_this_repository_holds(self):
        """Runs the gate's own precondition over the shipped table, so an observer that
        was renamed away fails here rather than in the middle of a gate run somebody is
        waiting on - where it would read as `caught`. Reads the table and the names of
        files, both of which mean the same thing while a mutation is applied, so unlike
        its `find` counterpart this one never has to skip."""
        gate.check_mapping(gate.MUTATIONS)

        self.assertGreater(len(gate.MUTATIONS), 0, "the table is empty, so this checked nothing")

    def test_every_table_guard_names_a_test_this_repository_holds(self):
        """Catches the exclusion going vacuous, which is how it would fail: silently.
        `TABLE_GUARDS` subtracts names from every set the gate derives or verifies, so
        a guard that has been renamed leaves the constant excluding a name nothing
        produces - and the guard becomes an observer again, which is the defect it was
        written to remove. Resolved through an import rather than matched against the
        file's text, because a test can be inherited from a class another module
        defines."""
        for guard in gate.TABLE_GUARDS:
            with self.subTest(guard=guard):
                parts = guard.split(".")
                self.assertEqual(len(parts), 3, f"{guard} is not `module.Class.test`")
                holder = getattr(importlib.import_module(parts[0]), parts[1], None)
                self.assertTrue(
                    callable(getattr(holder, parts[2], None)),
                    f"{guard} names no test this repository holds, so the exclusion it "
                    "is in matches nothing and the guard counts as an observer again",
                )

        self.assertGreater(len(gate.TABLE_GUARDS), 0, "no guard is named, so this checked nothing")

    def test_no_shipped_entry_names_the_same_test_twice(self):
        """Catches a mapping pasted in twice over. A duplicate is a claim counted twice
        and it is the shape a hand-edited table grows."""
        for mutation in gate.MUTATIONS:
            with self.subTest(mutation=mutation.name):
                self.assertEqual(
                    len(set(mutation.observers)),
                    len(mutation.observers),
                    f"{mutation.name} names {mutation.observers}",
                )


if __name__ == "__main__":
    unittest.main()
