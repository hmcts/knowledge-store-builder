"""A killed mutation-gate run must not leave a mutation applied to a source file.

`tests/mutation_gate.py` mutates a file, runs the suite, and restores the file in
a `finally`. `finally` unwinds on SIGINT, because CPython raises
`KeyboardInterrupt` for it - but not on SIGTERM, which ends the process with no
unwinding at all, and SIGTERM is what a timeout, a job kill or a cancelled CI
step sends. The gate takes most of ten minutes, so it is long enough to be
killed, and the failure is silent and misleading: the file keeps a deliberately
introduced defect, the next suite run in that tree reports real-looking failures
in real code, and nothing on screen says a mutation is still applied. That cost a
session's rework once, in a tree that was about to be pushed (issue #227).

Real signals against a real subprocess, because the process boundary is the true
IO boundary here: a mutation left applied is only observable from outside the
process that died. The harness below drives the gate's own `main`, `apply`,
`restore` and `recover` with the suite runner - and only that - replaced, so what
is exercised is the gate's code rather than a re-implementation of its pattern.
Killing the real gate is not an option: it is the very defect under test, and it
would leave a mutation in this working tree.

The last check here is about the gate's table rather than its signals: the
entries covering this behaviour target the gate's own file, which is the one file
whose text quotes the strings it mutates.

The records written by hand below are spelt out field by field rather than built
with the gate's own writer: what the header carries is part of what a killed run
hands its successor, so a field silently dropped from it has to fail here rather
than be reproduced on both sides. What a *reader outside the process* does with
those fields is `tests/test_mutation_gate_visibility.py`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import mutation_gate as gate

TESTS = Path(__file__).resolve().parent

# Bytes a text round trip cannot reproduce: universal newlines turn "\r\n" into
# "\n" when reading, on every platform, so a restore that re-encodes decoded text
# hands back different bytes from the ones it was given. Non-ASCII for the same
# reason at the encoding layer.
ORIGINAL = b"# harness target\r\nVALUE = 'ORIGINAL'\n# caf\xc3\xa9\n"
MUTATED = ORIGINAL.replace(b"ORIGINAL", b"MUTATED")


def record_for(module: str) -> bytes:
    """A recovery record as a killed run leaves it: one JSON header line naming
    the file and the run that mutated it, then the original bytes verbatim. The
    header is written out here rather than taken from the gate, so a field the
    gate stops writing is a failure rather than a value agreeing with itself."""
    header = {
        "module": module,
        "root": str(gate.ROOT),
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started": "",
    }
    return json.dumps(header).encode("utf-8") + b"\n" + ORIGINAL


# Drives the real gate over a purpose-built source directory. Only the calls that
# start a child are replaced - the process boundary the gate's own docstring calls
# out - and the two runners record what the tree looked like at each call, which is
# the only way to see whether a mutation was applied when the suite ran. Both
# runners, because the gate runs the whole suite once for the pre-check and the
# entry's own modules after that: leaving `run_observers` real would run this
# repository's suite from inside a test in it. `check_import_path` is the third
# such call and returns 0 here, because it asks a child where it imports
# knowledgestore from and answers about this repository, while the source
# directory below is a temporary one holding a single file.
HARNESS = """
import json
import sys
import time
from pathlib import Path

settings = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sys.path.insert(0, settings["tests"])

import mutation_gate as gate

gate.SRC = Path(settings["source"])
gate.RECOVERY_PATH = Path(settings["sidecar"])
gate.MUTATIONS = (
    # The observer names a test of this repository because `check_mapping` reads
    # the real tests directory, and nothing runs it: `run_observers` below is what
    # the gate calls instead.
    gate.Mutation(
        "harness",
        "target.py",
        "ORIGINAL",
        "MUTATED",
        "a purpose-built target",
        (
            "test_mutation_gate_recovery.MutationGateRecoveryTest."
            "test_a_clean_run_leaves_no_record_behind",
        ),
    ),
)

calls = []


def observe():
    calls.append(None)
    Path(settings["observations"], f"{len(calls)}.json").write_text(
        json.dumps(
            {
                "target": (gate.SRC / "target.py").read_bytes().hex(),
                "sidecar": Path(settings["sidecar"]).is_file(),
            }
        ),
        encoding="utf-8",
    )


def run_suite():
    observe()
    # A SuiteRun rather than a bool: the refusal that reads it names the tests
    # that failed, so a red pre-check has to carry one.
    return gate.SuiteRun(settings["suite_passes"], () if settings["suite_passes"] else ("harness",))


def run_observers(observers):
    observe()
    if settings["hang"]:
        Path(settings["ready"]).write_text("mutated", encoding="utf-8")
        time.sleep(120)
    # Every named observer, which is what a caught mutation looks like: the gate
    # reads emptiness as SURVIVED, so returning a falsy value here would make the
    # clean run this harness drives exit non-zero for a reason of its own.
    return tuple(observers)


gate.check_import_path = lambda: 0
gate.run_suite = run_suite
gate.run_observers = run_observers
raise SystemExit(gate.main([]))
"""


class MutationGateRecoveryTest(unittest.TestCase):
    def _workspace(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "source").mkdir()
        (root / "source" / "target.py").write_bytes(ORIGINAL)
        (root / "harness.py").write_text(HARNESS, encoding="utf-8")
        return root

    def _spawn(
        self, root: Path, label: str, *, suite_passes: bool = True, hang: bool = False
    ) -> subprocess.Popen[bytes]:
        (root / f"observations-{label}").mkdir()
        settings = root / f"settings-{label}.json"
        settings.write_text(
            json.dumps(
                {
                    "tests": str(TESTS),
                    "source": str(root / "source"),
                    "sidecar": str(root / "sidecar"),
                    "observations": str(root / f"observations-{label}"),
                    "ready": str(root / f"ready-{label}"),
                    "suite_passes": suite_passes,
                    "hang": hang,
                }
            ),
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, str(root / "harness.py"), str(settings)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # A harness left sleeping by an assertion that failed before the signal
        # outlives the test otherwise.
        self.addCleanup(self._reap, process)
        return process

    @staticmethod
    def _reap(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.kill()
        process.communicate()

    def _wait_until_mutated(self, root: Path, label: str, process: subprocess.Popen[bytes]) -> None:
        ready = root / f"ready-{label}"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if ready.is_file():
                return
            if process.poll() is not None:
                self.fail(f"the harness exited before mutating: {process.communicate()[1]!r}")
            time.sleep(0.02)
        process.kill()
        self.fail("the harness never reported the mutation applied")

    def _observation(self, root: Path, label: str, call: int) -> dict[str, object]:
        path = root / f"observations-{label}" / f"{call}.json"
        self.assertTrue(path.is_file(), f"the harness never reached suite run {call}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _target(self, root: Path) -> bytes:
        return (root / "source" / "target.py").read_bytes()

    def test_a_termination_signal_leaves_the_source_file_byte_identical(self):
        """Catches the restore never running on SIGTERM or SIGHUP: without a handler
        the process dies where it stands and the mutated file is what is left on
        disk, which the next suite run reports as a defect in real code."""
        for number in [signal.SIGTERM, getattr(signal, "SIGHUP", None)]:
            if number is None:
                continue
            with self.subTest(signal=signal.Signals(number).name):
                root = self._workspace()
                label = signal.Signals(number).name
                process = self._spawn(root, label, hang=True)
                self._wait_until_mutated(root, label, process)

                # The scenario is only worth anything if the mutation really was
                # applied when the signal arrived. Loosely, on the mutated token
                # alone: an exact comparison here would fail for the byte-exactness
                # the assertions below are about, before reaching them.
                self.assertIn(
                    b"MUTATED",
                    bytes.fromhex(str(self._observation(root, label, 2)["target"])),
                    "the harness signalled readiness without the mutation applied",
                )

                process.send_signal(number)
                process.communicate(timeout=60)

                self.assertEqual(
                    self._target(root),
                    ORIGINAL,
                    f"{label} left the file mutated: a deliberately introduced defect "
                    "stays in the tree and reads as a real failure",
                )
                self.assertNotEqual(process.returncode, 0, "a killed run must not report success")
                self.assertFalse(
                    (root / "sidecar").exists(),
                    "the restore was clean, so the recovery record must not survive it",
                )

    def test_a_record_left_by_a_killed_run_is_used_before_anything_else_runs(self):
        """Catches a gate that starts work on a corrupted tree. SIGKILL cannot be
        handled, so only something written before the mutation can undo it - and it
        has to be read before the pre-check, which would otherwise report the
        mutation's own failures as the suite being broken."""
        root = self._workspace()
        killed = self._spawn(root, "killed", hang=True)
        self._wait_until_mutated(root, "killed", killed)
        killed.kill()
        killed.communicate(timeout=60)

        self.assertIn(
            b"MUTATED", self._target(root), "SIGKILL should have left the mutation applied"
        )
        self.assertTrue(
            (root / "sidecar").is_file(),
            "nothing survived the kill, so the original bytes are unrecoverable",
        )

        # The second run stops at the pre-check, so what it saw there is exactly
        # what the tree looked like before any mutation of its own.
        recovering = self._spawn(root, "recovering", suite_passes=False)
        recovering.communicate(timeout=60)

        self.assertEqual(
            bytes.fromhex(str(self._observation(root, "recovering", 1)["target"])),
            ORIGINAL,
            "the gate ran the suite with a previous run's mutation still applied",
        )
        self.assertEqual(self._target(root), ORIGINAL)
        self.assertFalse((root / "sidecar").exists(), "a used record must not be left behind")
        self.assertEqual(recovering.returncode, 1)

    def test_a_clean_run_leaves_no_record_behind(self):
        """Catches a record that is written and never deleted: every later run would
        then restore from a stale one, or refuse, on a tree that is perfectly fine.
        The other half of the same break is a record that is never written, which
        the mutated run's view of the tree catches here."""
        root = self._workspace()
        clean = self._spawn(root, "clean")
        clean.communicate(timeout=60)

        self.assertEqual(clean.returncode, 0)
        self.assertTrue(
            self._observation(root, "clean", 2)["sidecar"],
            "no recovery record existed while the mutation was applied, which is the "
            "only window in which a kill can lose the original",
        )
        self.assertFalse(
            (root / "sidecar").exists(), "a finished run left its recovery record on disk"
        )
        self.assertEqual(self._target(root), ORIGINAL)

    def test_recovery_restores_the_exact_original_bytes(self):
        """Catches a restore that re-derives the file instead of replaying it. Reading
        text decodes and translates newlines, so a record kept as text hands back
        different bytes from the ones it was given - and the gate's whole claim is
        that the tree is unchanged."""
        root = self._workspace()
        self._point_gate_at(root)
        (root / "source" / "target.py").write_bytes(MUTATED)
        (root / "sidecar").write_bytes(record_for("target.py"))

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.recover(), 0)

        self.assertEqual(self._target(root), ORIGINAL)
        self.assertFalse((root / "sidecar").exists())

    def test_a_record_naming_no_restorable_file_refuses_and_names_the_path(self):
        """Catches a gate that shrugs off a record it cannot act on. Deleting it
        silently, or ignoring it, leaves whatever was mutated mutated with nothing on
        screen to say so - so the refusal has to carry the path to remove by hand."""
        root = self._workspace()
        self._point_gate_at(root)
        (root / "sidecar").write_bytes(record_for("absent-module.py"))

        reported = io.StringIO()
        with contextlib.redirect_stderr(reported):
            code = gate.recover()

        self.assertEqual(code, 1, "a record the gate cannot act on must stop the run")
        self.assertIn(str(root / "sidecar"), reported.getvalue())
        self.assertTrue((root / "sidecar").is_file(), "the record is the only copy of the original")

    def test_the_signal_handlers_are_removed_once_the_loop_is_over(self):
        """Catches handlers installed at import or left installed afterwards. This
        module is imported by the suite that the gate itself runs, so a disposition
        changed as a side effect would silently change how every test process
        responds to being killed."""
        before = signal.getsignal(signal.SIGTERM)

        with gate.restoring_on_termination():
            self.assertIsNot(
                signal.getsignal(signal.SIGTERM),
                before,
                "SIGTERM is not routed through the gate's handler inside the loop",
            )

        self.assertIs(
            signal.getsignal(signal.SIGTERM),
            before,
            "the gate left its own SIGTERM handler installed",
        )

    def test_a_mutation_of_this_gate_cannot_rewrite_its_own_table(self):
        """Catches an entry targeting the gate's own file whose `find` is written as
        one contiguous literal. The table quotes the code it mutates and sits earlier
        in the file, so `apply` - which replaces the first occurrence - would rewrite
        the entry instead of the code: the mutation then survives, with the reason
        nowhere on screen. Splitting each `find` across adjacent literals is what
        prevents it, and only a count can tell that it was done.

        Named in the gate's `TABLE_GUARDS`, so it never counts as an observer. It
        reads the table, and applying any entry that targets that file makes it red
        whatever the mutation did - which is correct here and meaningless there
        (#274). The gate subtracts it rather than this test skipping, so the check
        stays live in an ordinary suite run."""
        own = TESTS / "mutation_gate.py"
        text = own.read_text(encoding="utf-8")

        checked = 0
        for mutation in gate.MUTATIONS:
            if gate.target_of(mutation.module) != own:
                continue
            checked += 1
            with self.subTest(mutation=mutation.name):
                self.assertEqual(
                    text.count(mutation.find),
                    1,
                    f"{mutation.find!r} appears {text.count(mutation.find)} times in "
                    f"{own.name}, and the one occurrence has to be the code: none means "
                    "the entry no longer matches the file, two means it was written as "
                    "one literal and matches itself",
                )

        self.assertGreater(
            checked, 0, "no entry targets the gate's own file, so this check saw nothing"
        )

    def test_an_ambiguous_find_is_refused_rather_than_applied_to_the_first_site(self):
        """Catches an entry whose `find` matches its module twice. `apply` replaces
        one occurrence, so an ambiguous entry mutates whichever site comes first: the
        gate then reports `caught` about a line the entry does not describe, and a
        run cannot say so - a pass and a fail are all it has. Checked here, on the
        gate's own `apply`, rather than by walking the table from the suite: a suite
        test comparing every `find` against its file fails while any mutation is
        applied, which is every moment of a gate run, and would report each one as
        caught whatever else the suite did.

        The refusal has to leave the file alone. A gate that half-applies an entry it
        is rejecting is the defect this module exists for.
        """
        root = self._workspace()
        self._point_gate_at(root)
        target = root / "source" / "target.py"
        target.write_bytes(ORIGINAL + b"VALUE = 'ORIGINAL'\n")
        ambiguous = gate.Mutation(
            "twice over",
            "target.py",
            "ORIGINAL",
            "MUTATED",
            "a purpose-built target",
            ("test_mutation_gate_recovery",),
        )

        with self.assertRaises(SystemExit) as raised:
            gate.apply(ambiguous)

        self.assertIn("ambiguous", str(raised.exception))
        self.assertIn("2 times", str(raised.exception))
        self.assertEqual(target.read_bytes(), ORIGINAL + b"VALUE = 'ORIGINAL'\n")
        self.assertFalse(gate.RECOVERY_PATH.is_file())

    def test_a_find_naming_one_site_still_applies(self):
        """The sensitivity control for the refusal above: a gate that refused every
        entry would pass that test and test nothing at all."""
        root = self._workspace()
        self._point_gate_at(root)
        unique = gate.Mutation(
            "once only",
            "target.py",
            "ORIGINAL",
            "MUTATED",
            "a purpose-built target",
            ("test_mutation_gate_recovery",),
        )

        original = gate.apply(unique)

        self.assertEqual(original, ORIGINAL)
        self.assertEqual((root / "source" / "target.py").read_bytes(), MUTATED)
        gate.restore(unique.module, original)

    def test_every_table_entry_names_one_site_in_its_target(self):
        """Runs the gate's own precondition over the shipped table, so an entry that
        has become ambiguous is a failure now rather than a misattributed `caught` in
        the next ten-minute gate run. Reads the tree, so it is skipped while a
        mutation is applied - the count it depends on is only meaningful in an
        unmutated tree, and a gate run mutates one file at a time.

        Named in the gate's `TABLE_GUARDS` as well, which is belt and braces: the
        skip means it does not fail during a run, and the exclusion means it would
        not be read as an observation if the skip were ever removed (#274)."""
        if gate.RECOVERY_PATH.is_file():
            self.skipTest("a mutation is applied: its target does not hold its own `find`")
        for mutation in gate.MUTATIONS:
            with self.subTest(mutation=mutation.name):
                target = gate.target_of(mutation.module)
                occurrences = target.read_text(encoding="utf-8").count(mutation.find)
                self.assertEqual(
                    occurrences,
                    1,
                    f"{mutation.find!r} appears {occurrences} times in {mutation.module}: "
                    "none means the entry no longer matches the file, more than one means "
                    "the mutation applied is not the one the entry describes",
                )

    def _point_gate_at(self, root: Path) -> None:
        self.addCleanup(setattr, gate, "SRC", gate.SRC)
        self.addCleanup(setattr, gate, "RECOVERY_PATH", gate.RECOVERY_PATH)
        gate.SRC = root / "source"
        gate.RECOVERY_PATH = root / "sidecar"


if __name__ == "__main__":
    unittest.main()
