"""Each mutation-gate entry runs only the test modules it names, and names the right ones.

`tests/mutation_gate.py` ran the whole suite for every entry: 1,447 tests, 8.7
seconds, 182 times, to observe a failure that one module could see. Running only
the named modules takes that to about two minutes - but the mapping is the point
rather than the time. An entry that names its observer can be checked by a later
reader; a gate that claims to be sensitivity-checked cannot.

The cost is a new way to be wrong, and it is the quiet kind. A wrong observer is
invisible from a passing run, because the gate prints `caught` either way:

- an entry naming nothing runs no test, and `unittest` reports an empty suite as
  a success - the worst outcome available here, a gate reporting a defect
  observed that it never looked for;
- an entry naming a module this tree does not hold loads a `_FailedTest`, which
  fails the run for the name being wrong rather than for the mutation;
- an entry naming a module that exists and does not observe it reports the entry
  as survived, which is loud - and is the direction this module pins hardest,
  because it is what proves the selection is real.

So every check here drives the gate's own `sweep`, `verify_mapping` and
`check_mapping` over a purpose-built tree: a source file to mutate, a test module
that reads it, and one that does not. Real subprocesses, real discovery, the
gate's real code - the only thing constructed is the tree, because the claim is
about which modules a run reaches, and that is only observable from the outcome
of running them.

The last check reads the shipped table instead, so an observer that was renamed
away is a failure in the suite rather than a `caught` that proved nothing in a
gate run somebody was waiting on.
"""

from __future__ import annotations

import contextlib
import io
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
        self.assertEqual(2, 1 + 1)
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

ORIGINAL = 'VALUE = "ORIGINAL"\n'


class MutationGateMappingTest(unittest.TestCase):
    def _tree(self, subtest_watcher: bool = False) -> Path:
        """A repository the gate can be pointed at: one source file, two test modules.

        `subtest_watcher` adds a third that observes the same mutation through
        `subTest`. It is opt-in because a module added here observes the mutation
        for every test that builds this tree, and the verifier checks the named
        set against everything that failed - so adding it unconditionally makes
        the mapping tests disagree about what should have been named.
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

    def test_a_failure_inside_a_subtest_is_attributed_to_its_own_module(self):
        """Catches the reporter losing which module saw a subTest failure.

        Break it by deleting the `_SubTest` branch from `module_of`: the failing
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

        code, _, reported = self._verify(self._entry("test_subtest_watcher", "test_watcher"))

        self.assertEqual(0, code, f"a correct mapping was refused: {reported}")

    def test_a_module_failing_only_in_a_subtest_still_reads_as_failing(self):
        """The control for the test above, and it does not carry its assurance.

        A subTest failure is reported alongside the case that contains it, so a
        module whose only failure is one could in principle be read as passing.
        This pins that it is not. It passes with the `_SubTest` branch removed -
        attribution is wrong then, but the run still fails - so it cannot detect
        the defect above and is not a second guard against it.
        """
        self._tree(subtest_watcher=True)

        code, printed = self._sweep(self._entry("test_subtest_watcher"))

        self.assertEqual(0, code, printed)
        self.assertIn("caught", printed)

    def test_an_entry_naming_no_observer_is_refused_before_anything_is_applied(self):
        """Catches the refusal being dropped from `check_mapping`. An entry with no
        observers runs no test at all, and an empty suite passes - which this gate
        prints as `caught`, a defect reported as observed by a run that looked for
        nothing. Louder than the alternative on purpose: the entry has to be named,
        with the command that fills it in."""
        root = self._tree()

        with self.assertRaises(SystemExit) as raised:
            gate.sweep((self._entry(),))

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

        with self.assertRaises(SystemExit) as raised:
            gate.sweep((self._entry("test_departed"),))

        self.assertIn("test_departed", str(raised.exception))
        self.assertIn(str(root / "tests" / "test_departed.py"), str(raised.exception))

    def test_a_named_module_that_cannot_see_the_mutation_marks_it_survived(self):
        """The check that proves the selection is real, in both directions at once.
        `test_bystander` passes with the mutation applied, so the entry survives; if the
        gate were still running everything, `test_watcher` would fail in the same run
        and the entry would read as caught. Catches a `run_modules` that quietly falls
        back to the whole suite, and a wrong mapping being reported as a pass."""
        self._tree()

        code, printed = self._sweep(self._entry("test_bystander"))

        self.assertEqual(code, 1, f"a mutation nothing named observed was reported: {printed}")
        self.assertIn("SURVIVED", printed)
        self.assertIn("0 of 1 mutations caught", printed)

    def test_a_named_module_that_observes_the_mutation_marks_it_caught(self):
        """The sensitivity control for the check above: a runner that failed every entry,
        or ran nothing and called it a failure, would pass that one and mean nothing.
        `test_watcher` reads the mutated file, so this is the whole gate in miniature."""
        root = self._tree()

        code, printed = self._sweep(self._entry("test_watcher"))

        self.assertEqual(code, 0, f"the module that reads the mutated file did not fail: {printed}")
        self.assertIn("caught", printed)
        self.assertIn("1 of 1 mutations caught", printed)
        self.assertEqual((root / "source" / "target.py").read_text(encoding="utf-8"), ORIGINAL)

    def test_the_verifier_refuses_a_mapping_that_names_a_module_which_does_not_observe(self):
        """Catches the verifier going vacuous, which is the only way a wrong mapping
        becomes undetectable: a normal run cannot tell `caught` from `caught for the
        wrong reason`. Here the entry names the bystander, the whole tree is run, and
        the watcher fails without being named - so the entry's claim about what protects
        the behaviour is false in both directions."""
        self._tree()

        code, printed, reported = self._verify(self._entry("test_bystander"))

        self.assertEqual(code, 1, f"a mapping naming the wrong module was accepted: {printed}")
        self.assertIn("DIFFERS", printed)
        self.assertIn("test_watcher failed and is not named", reported)
        self.assertIn("test_bystander is named and did not fail", reported)

    def test_an_entry_disagreeing_in_both_directions_is_counted_once(self):
        """Catches the summary tallying complaints rather than the entries it names.

        One entry raises a message in each direction, so subtracting the message
        list from the entry count answers a different question than the sentence
        asks. Break it by restoring `len(mutations) - len(wrong)`: a population
        of one entry then reports `-1 of 1`, a count below zero.
        """
        self._tree()

        _, printed, _ = self._verify(self._entry("test_bystander"))

        self.assertIn("0 of 1 entries name what observes them", printed)

    def test_the_verifier_accepts_the_mapping_that_matches_what_failed(self):
        """The sensitivity control for the verifier: one that refused every entry would
        pass the check above and could not distinguish a right mapping from a wrong one."""
        self._tree()

        code, printed, reported = self._verify(self._entry("test_watcher"))

        self.assertEqual(code, 0, f"a mapping that matches the failures was refused: {reported}")
        self.assertIn("agrees", printed)
        self.assertIn("1 of 1 entries name what observes them", printed)

    def test_a_suite_run_that_reported_nothing_is_refused(self):
        """Catches a run whose child never reported being read as a pass or a fail. Both
        are conclusions about a suite that did not run: as a pass it makes every entry
        survive, and as a fail it makes every entry caught - the second silently."""
        root = self._tree()
        (root / "tests" / "test_dies.py").write_text(DIES, encoding="utf-8")

        with self.assertRaises(SystemExit) as raised:
            gate.run_modules(["test_dies"])

        self.assertIn("reported nothing", str(raised.exception))
        self.assertIn("exit 3", str(raised.exception))

    def test_every_shipped_entry_names_test_modules_this_repository_holds(self):
        """Runs the gate's own precondition over the shipped table, so an observer that
        was renamed away fails here rather than in the middle of a gate run somebody is
        waiting on - where it would read as `caught`. Reads the table and the names of
        files, both of which mean the same thing while a mutation is applied, so unlike
        its `find` counterpart this one never has to skip."""
        gate.check_mapping(gate.MUTATIONS)

        self.assertGreater(len(gate.MUTATIONS), 0, "the table is empty, so this checked nothing")

    def test_no_shipped_entry_names_the_same_module_twice(self):
        """Catches a mapping pasted in twice over. A duplicate runs the module twice and
        doubles the cost of the entry, and it is the shape a hand-edited table grows."""
        for mutation in gate.MUTATIONS:
            with self.subTest(mutation=mutation.name):
                self.assertEqual(
                    len(set(mutation.observers)),
                    len(mutation.observers),
                    f"{mutation.name} names {mutation.observers}",
                )


if __name__ == "__main__":
    unittest.main()
