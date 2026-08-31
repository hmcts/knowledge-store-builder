"""The mapping check, sliced across runners, and the reconciliation that keeps it honest.

`--verify-mapping` applies every entry against the whole suite, so it costs what
this gate cost before entries named observers: 28 minutes in CI against 90 seconds
for the fast mode. It shards cleanly because every entry runs the same thing, so
entries are interchangeable units of work with no ordering, no accumulation and no
cross-entry state - and one runner per shard means no shared filesystem, which is
the only version with no shared subject to corrupt (#285).

The defect that shape invites is a slice that drops an entry. Nothing about the
short run looks wrong: the leg prints `47 of 47 entries name what observes them`
and passes, and the entry nobody checked is the one whose mapping had gone stale.
Two shards overlapping is the same defect wearing the other sign, and it is the
half a total against `len(MUTATIONS)` cannot see - a duplicate pays for a drop and
the sum comes out right. So the union is compared as a set as well as counted, and
both are checked before anything is applied.

Every check here drives the gate's own `shard`, `check_shards`, `shard_argument`
and `main`: over the shipped table, over deliberately broken slicers, and over a
purpose-built tree of two entries where which entries a shard *ran* is readable
from what it printed. The broken slicers are the point of the module - a
reconciliation nobody has watched refuse is a reconciliation nobody knows can.

The last class reads `tests.yml`, because the layer above is where the same defect
becomes invisible again: the reconciliation checks that the slices the gate would
take are a partition, and it cannot know how many legs the matrix actually ran.
Two legs listed as `1` runs shard 1 twice and shard 4 never, with every leg green
and every reconciliation passing. The divisor is taken from `strategy.job-total`
so a leg added or removed cannot disagree with it, and the check here is that the
legs are `1..N` exactly once each - which is only worth anything while that
divisor is the leg count, so that is asserted too.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mutation_gate as gate

try:  # PyYAML is the `deploy` extra, not a runtime dependency of this library
    import yaml

    HAS_YAML = True
except ImportError:  # pragma: no cover - the default-install CI job takes this path
    HAS_YAML = False

needs_yaml = unittest.skipUnless(HAS_YAML, "needs the `deploy` extra (PyYAML)")

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "tests.yml"

# One source file per entry, so a shard applying an entry it was not given is
# visible in what the run printed rather than only in a count.
SOURCES = {"first.py": 'VALUE = "FIRST"\n', "second.py": 'VALUE = "SECOND"\n'}

# Reads the file its entry mutates, so it fails while that mutation is applied and
# passes when it is not. The path comes from its own location: the tree is built in
# a temporary directory whose name it cannot know.
OBSERVER = """
import unittest
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "src" / "knowledgestore" / "{module}"


class Observe(unittest.TestCase):
    def test_the_target_is_unmutated(self):
        self.assertIn("{value}", TARGET.read_text(encoding="utf-8"))
"""

ENTRIES = (
    gate.Mutation(
        "the first target loses its value",
        "first.py",
        "FIRST",
        "MUTATED",
        "a purpose-built target",
        ("test_first.Observe.test_the_target_is_unmutated",),
    ),
    gate.Mutation(
        "the second target loses its value",
        "second.py",
        "SECOND",
        "MUTATED",
        "a purpose-built target",
        ("test_second.Observe.test_the_target_is_unmutated",),
    ),
)


class ShardsPartitionTheTableTest(unittest.TestCase):
    """The slicing itself, against the table that ships."""

    def test_every_entry_lands_in_exactly_one_shard_of_every_count(self):
        """Catches an off-by-one in the slice expression, which is silent by
        construction: the shards' verdicts are counted against the entries a shard
        held, so a slice missing one entry prints `N of N` and passes.

        The property is asserted here rather than by calling `check_shards`, which
        would be the gate agreeing with itself: the shards are concatenated and
        compared against the table by hand, in both directions - nothing dropped
        and nothing held twice - and their lengths summed to the table's size.

        Several counts, because a partition that holds for four and not for one or
        five is an arithmetic accident rather than a property.
        """
        expected = sorted(entry.name for entry in gate.MUTATIONS)

        for count in (1, 2, 3, 4, 5, 7):
            with self.subTest(shards=count):
                slices = [gate.shard(gate.MUTATIONS, index, count) for index in range(1, count + 1)]
                held = sorted(entry.name for slice_ in slices for entry in slice_)

                self.assertEqual(
                    sum(len(slice_) for slice_ in slices),
                    len(gate.MUTATIONS),
                    "the shards do not hold the table's entries between them",
                )
                self.assertEqual(held, expected, "an entry is missing from every shard, or in two")

    def test_the_shipped_table_reconciles(self):
        """The sensitivity control for the two refusals below: a `check_shards` that
        refused every set would pass both of them while stopping every sharded run,
        and the refusal is a `SystemExit` raised before the first entry is applied -
        so a run would end with no verdict rather than with a wrong one."""
        for count in (1, 2, 3, 4, 5, 7):
            with self.subTest(shards=count):
                self.assertIsNone(gate.check_shards(gate.MUTATIONS, count))

    def _slicer(self, slices: dict[int, tuple[gate.Mutation, ...]]) -> None:
        """Replace the gate's slice expression with one that returns `slices`."""
        self.addCleanup(setattr, gate, "shard", gate.shard)

        def broken(mutations: object, index: int, count: object) -> tuple[gate.Mutation, ...]:
            return slices[index]

        gate.shard = broken

    def test_a_slice_that_drops_an_entry_is_refused_and_names_both_counts(self):
        """Catches the reconciliation being dropped, or reduced to a shape that cannot
        act as one. The count it got has to be named against the count it expected,
        because the number a reader can see - the leg's own `N of N` - is consistent
        with the defect: the arithmetic only disagrees against the table's size."""
        entries = gate.MUTATIONS
        self._slicer({1: entries[0:1], 2: entries[2:]})

        with self.assertRaises(SystemExit) as raised:
            gate.check_shards(entries, 2)

        message = str(raised.exception)
        self.assertIn(str(len(entries) - 1), message)
        self.assertIn(str(len(entries)), message)
        self.assertIn(entries[1].name, message)

    def test_two_shards_holding_one_entry_between_them_twice_are_refused(self):
        """Catches a reconciliation that only sums. This slicer holds one entry twice
        and another not at all, so the total is exactly the table's size and every
        count in sight agrees - the disagreement exists only as a set. It is not a
        contrived shape: a slice expression corrected on one side and not the other
        produces it, and it costs the same as a drop, because the entry nobody
        applied is the one whose mapping nobody checked."""
        entries = gate.MUTATIONS
        self._slicer({1: entries[0:1] + entries[1:2], 2: entries[1:2] + entries[3:]})
        self.assertEqual(
            sum(len(gate.shard(entries, index, 2)) for index in (1, 2)),
            len(entries),
            "this fixture has to be the case a sum against the table's size cannot see",
        )

        with self.assertRaises(SystemExit) as raised:
            gate.check_shards(entries, 2)

        message = str(raised.exception)
        self.assertIn(entries[1].name, message)
        self.assertIn(entries[2].name, message)


class ShardArgumentTest(unittest.TestCase):
    def test_the_slices_the_argument_names_are_the_ones_it_takes(self):
        """The control for the refusals below, and the parse itself: `2/4` has to be
        the second of four rather than the fourth of two."""
        self.assertEqual(gate.shard_argument("2/4"), (2, 4))
        self.assertEqual(gate.shard_argument("1/1"), (1, 1))

    def test_a_specification_that_is_not_n_of_m_is_refused(self):
        """Catches an unvalidated argument, every wrong form of which is quiet.

        `4` alone and `0/4` run an empty slice, and every mode of this gate reports
        an empty table as a pass. `5/4` is worse than empty: `MUTATIONS[4::4]` is a
        real slice of real entries that overlaps shard 1 and misses three quarters
        of the table, and the reconciliation cannot see it - the shards it checks
        are the four the count names, not the fifth the run took.
        """
        for specification in (
            "4",
            "",
            "1/",
            "/4",
            "one/four",
            "5/4",
            "0/4",
            "1/0",
            "-1/4",
            "1/4/4",
        ):
            with self.subTest(specification=specification):
                with self.assertRaises(SystemExit) as raised:
                    gate.shard_argument(specification)
                self.assertIn("--shard", str(raised.exception))


class OneShardRunsItsOwnSliceTest(unittest.TestCase):
    """The gate's real `main`, driven over a purpose-built tree of two entries."""

    def _tree(self) -> Path:
        """A repository the gate can be pointed at: two source files, two observers.

        The same shape `test_mutation_gate_refusals` builds, because `main` starts by
        asking a child where it imports `knowledgestore` from and refuses a run that
        would conclude from another tree.
        """
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Resolved, because a child's reported path is compared against the gate's
        # own `SRC`, and macOS hands out temporary directories behind a symlink.
        root = Path(temporary.name).resolve()
        package = root / "src" / "knowledgestore"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (root / "tests").mkdir()
        for module, source in SOURCES.items():
            (package / module).write_text(source, encoding="utf-8")
            value = source.partition('"')[2].partition('"')[0]
            (root / "tests" / f"test_{module}").write_text(
                OBSERVER.format(module=module, value=value), encoding="utf-8"
            )

        for name in ("ROOT", "SRC", "RECOVERY_PATH", "MUTATIONS"):
            self.addCleanup(setattr, gate, name, getattr(gate, name))
        gate.ROOT = root
        gate.SRC = package
        gate.RECOVERY_PATH = root / "sidecar"
        gate.MUTATIONS = ENTRIES

        patched = mock.patch.dict(os.environ, {"PYTHONPATH": str(root / "src")})
        patched.start()
        self.addCleanup(patched.stop)
        return root

    @staticmethod
    def _main(argv: list[str]) -> tuple[int, str, str]:
        printed, reported = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(reported):
            code = gate.main(argv)
        return code, printed.getvalue(), reported.getvalue()

    def test_each_shard_runs_its_own_entries_and_between_them_they_run_the_table(self):
        """Catches the selection not reaching the run at all - a `--shard` accepted and
        ignored runs the whole table on every leg, which is correct, four times the
        cost, and looks exactly like the change working.

        Which entries ran is read from what the run printed rather than from a count,
        because the count is what a wrong slice gets right.
        """
        root = self._tree()
        first, second = (entry.name for entry in ENTRIES)

        ran = {}
        for index in (1, 2):
            code, printed, reported = self._main(["--shard", f"{index}/2"])
            self.assertEqual(code, 0, f"shard {index} of 2 failed: {reported}")
            ran[index] = {name for name in (first, second) if name in printed}

        self.assertEqual(ran, {1: {first}, 2: {second}})
        self.assertEqual(
            (root / "src" / "knowledgestore" / "first.py").read_text(encoding="utf-8"),
            SOURCES["first.py"],
        )

    def test_a_shard_says_how_its_entries_reconcile_with_the_whole_table(self):
        """Catches the reconciliation happening and going unsaid. The leg's own verdict
        is `1 of 1 entries name what observes them`, which is indistinguishable from
        the whole table agreeing - so the sentence beside it has to carry the shard,
        the count it ran and the table it came out of.

        `--verify-mapping` because that is the mode the workflow shards, and the two
        halves have to be exercised together: the reconciliation is printed by the
        run rather than by the mode, so a mode that returned before it would be
        silent here.
        """
        self._tree()

        code, printed, reported = self._main(["--verify-mapping", "--shard", "2/2"])

        self.assertEqual(code, 0, reported)
        self.assertIn("1 of 1 entries name what observes them", printed)
        self.assertIn("shard 2 of 2", printed)
        self.assertIn(f"1 of {len(ENTRIES)} entries", printed)

    def test_a_run_reconciles_before_it_applies_anything(self):
        """Catches the reconciliation being wired in after the entries start running,
        which is most of the way to not having it: the run that cannot be trusted has
        already mutated a source file, and on the schedule nobody is watching it. The
        refusal has to arrive with the tree untouched and no recovery record."""
        root = self._tree()
        self.addCleanup(setattr, gate, "shard", gate.shard)

        def holds_nothing(mutations: object, index: object, count: object) -> tuple[()]:
            return ()

        gate.shard = holds_nothing

        with self.assertRaises(SystemExit) as raised:
            self._main(["--shard", "1/2"])

        self.assertIn(str(len(ENTRIES)), str(raised.exception))
        self.assertEqual(
            (root / "src" / "knowledgestore" / "first.py").read_text(encoding="utf-8"),
            SOURCES["first.py"],
        )
        self.assertFalse((root / "sidecar").exists(), "an entry was applied despite the refusal")

    def test_an_unsharded_run_still_runs_the_whole_table(self):
        """The control for every check above: a `--shard` default that sliced when
        nobody asked would leave the fast gate running a quarter of its entries on a
        pull request, which is the mode that has to stay complete."""
        self._tree()

        code, printed, reported = self._main([])

        self.assertEqual(code, 0, reported)
        self.assertIn(f"{len(ENTRIES)} of {len(ENTRIES)} mutations caught", printed)


@needs_yaml
class WorkflowShardsTest(unittest.TestCase):
    """The layer the in-process reconciliation cannot see: how many legs actually ran."""

    def setUp(self) -> None:
        self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.jobs = self.workflow["jobs"]

    def _sharded_step(self) -> dict[str, object]:
        steps = [
            step
            for step in self.jobs["mapping"]["steps"]
            if "--verify-mapping" in str(step.get("run", ""))
        ]
        self.assertEqual(len(steps), 1, "the mapping job runs the mapping check once")
        return steps[0]

    def test_the_matrix_legs_are_one_to_n_and_the_divisor_is_the_number_of_them(self):
        """Catches a leg dropped, duplicated or renumbered, which the gate's own
        reconciliation cannot: it checks that the slices of `1..M` are a partition,
        and a leg that never ran is not a slice it was asked about. Legs `[1, 1, 2]`
        runs shard 1 twice and shard 3 never, with three green legs and three
        reconciliations that passed.

        The second half is what stops the first going vacuous. Comparing the legs
        against themselves says nothing unless the divisor is the number of them, so
        the divisor has to come from `strategy.job-total` rather than from a literal
        somebody has to remember to change.
        """
        legs = self.jobs["mapping"]["strategy"]["matrix"]["shard"]

        self.assertEqual(legs, list(range(1, len(legs) + 1)))
        self.assertGreater(len(legs), 1, "one leg is not a matrix, and shards nothing")
        self.assertIn("strategy.job-total", str(self._sharded_step()["env"]))
        self.assertNotIn("job-total", str(self._sharded_step()["run"]))

    def test_the_shard_the_leg_runs_is_the_leg_it_is(self):
        """Catches the two halves of the argument coming from different places - a
        matrix of four legs all running `--shard 1/4` is four passing legs over a
        quarter of the table."""
        step = self._sharded_step()

        self.assertIn("matrix.shard", str(step["env"]))
        self.assertRegex(str(step["run"]), r"--shard\s+\"?\$\{?\w+\}?/\$\{?\w+\}?\"?")

    def test_the_mapping_job_runs_the_environment_the_tests_job_runs(self):
        """Catches the two jobs' environments drifting. They install the same package,
        the same pinned extras and the same interpreter, and the mapping check
        concludes about the tests the suite runs - so a job that installs one fewer
        extra verifies a mapping derived under another environment and reports it as
        the table being wrong. Two jobs is the cost of one runner per shard; this is
        what keeps it from being two environments."""
        mapping = self.jobs["mapping"]["steps"]
        environment = mapping[: len(mapping) - 1]

        self.assertGreater(len(environment), 1, "the mapping job sets nothing up")
        self.assertEqual(environment, self.jobs["tests"]["steps"][: len(environment)])

    def test_a_leg_that_did_not_pass_fails_the_summary_job(self):
        """Catches the summary reporting one verdict that cannot be a bad one.

        GitHub reports each leg separately, so the summary is what a reader and a
        required check would read - and it is driven here rather than matched, with
        the result the runner would hand it. The result reaches the script through
        the environment, which is what makes that possible; interpolating it into the
        script is also the injection shape the analysers read.
        """
        summary = self.jobs["mapping-summary"]
        step = summary["steps"][0]
        self.assertIn("needs.mapping.result", str(step["env"]))

        for result, expected in (("success", 0), ("failure", 1), ("cancelled", 1), ("", 1)):
            with self.subTest(result=result):
                completed = subprocess.run(
                    ["bash", "-c", str(step["run"])],
                    env={**os.environ, **{name: result for name in step["env"]}},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, expected, completed.stderr)
                if expected and result:
                    self.assertIn(result, completed.stdout + completed.stderr)

    def test_the_summary_runs_even_when_a_leg_failed(self):
        """Catches the summary being skipped by the failure it exists to report. A job
        that `needs` a failed job does not run, and a skipped check is not a failed
        one - a required context would read it as satisfied, so the verdict above
        would be correct and never reached."""
        summary = self.jobs["mapping-summary"]

        self.assertIn("mapping", summary["needs"])
        self.assertIn("always()", summary["if"])

    def test_the_sharded_check_runs_on_what_the_fast_gate_does_not(self):
        """Catches the 28-minute check landing on pull requests, and the other way
        round - the fast gate is what runs per pull request and this is what runs on
        the schedule. Both conditions name the same two events, so a mapping check
        that quietly started running on every push would be visible here."""
        events = self.jobs["mapping"]["if"]

        self.assertIn("schedule", events)
        self.assertIn("workflow_dispatch", events)
        self.assertNotIn("pull_request", events)
        self.assertEqual(
            [
                step
                for step in self.jobs["tests"]["steps"]
                if "--verify-mapping" in str(step.get("run", ""))
            ],
            [],
            "the tests job still runs the whole-suite mapping check",
        )


if __name__ == "__main__":
    unittest.main()
