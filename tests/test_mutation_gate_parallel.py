"""The slow modes, spread over workers that each mutate a private copy of the tree.

`--derive-mapping`, `--verify-mapping`, `--isolate` and `observer_staleness.py
--settle` run one whole suite per entry, and entries share nothing - so a run over
six entries is six independent questions asked one after another. They were serial
for one reason, and it is a good one: the gate mutates a real file in the tree, and
two workers doing that in one checkout corrupt each other's subject.

`--jobs N` keeps the reason and removes the cost. Each worker gets its own copy of
the checkout - every file git does not ignore, and a clone of its repository,
because the suite reads documents, skills and its own git history as well as `src/`
- and the suite runs there with `PYTHONPATH` pointing at that copy's `src`. The
real checkout is never mutated, so the record `--status` reads is never written by a
parallel run; the run leaves a marker naming itself instead, so `--status` can still
say a gate run is going and what it holds.

What could go wrong is mostly quiet, which is why each check below names its break:

- a worker that mutates the real tree rather than its copy reads green, because the
  restore puts the file back - only *where* the mutation was seen can tell;
- output collected in finish order is still correct output, in an order that
  differs between two runs over the same table (CLAUDE.md 1.3);
- an entry that could not be applied, or whose worker died, can simply be missing
  from the results - and a mode that counts what came back prints `N of N`;
- a copy whose suite imports another tree's package concludes about the wrong code,
  which is the guard `check_import_path` exists for and has to hold per copy;
- an interrupted run that leaves its copies behind fills the temporary directory one
  checkout at a time.

The tree each check runs over is real: a git repository holding a package and the
tests that observe it, real worker processes, real suite runs. Only the table is
forged.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import mutation_gate as gate
import observer_staleness as staleness

TESTS = Path(__file__).resolve().parent

SOURCES = {
    "first.py": 'VALUE = "FIRST"\n',
    "second.py": 'VALUE = "SECOND"\n',
    "third.py": 'VALUE = "THIRD"\n',
}

# Reads the file its entry mutates and fails while that mutation is applied. When it
# fails it records *where* it read the mutation, which is the only evidence of which
# tree a worker mutated: a restore puts the file back either way. The other tokens
# give a test a way to make one entry slow, kill its own run, or hold it open.
OBSERVER = """
import os
import time
import unittest
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "src" / "knowledgestore" / "{module}"


class Observe(unittest.TestCase):
    def test_the_target_is_unmutated(self):
        text = TARGET.read_text(encoding="utf-8")
        if "{value}" not in text:
            seen = os.environ.get("PARALLEL_GATE_SEEN")
            if seen:
                with open(seen, "a", encoding="utf-8") as log:
                    log.write(str(TARGET) + "\\n")
            if "SLOW" in text:
                time.sleep(3)
            if "CRASH" in text:
                os._exit(3)
            if "HANG" in text:
                # Appended, one line per held run: the worker's pid, then the suite's.
                with open(os.environ["PARALLEL_GATE_READY"], "a", encoding="utf-8") as ready:
                    ready.write(f"{{os.getppid()}} {{os.getpid()}}\\n")
                time.sleep(60)
        self.assertIn("{value}", text)
"""

BYSTANDER = """
import unittest


class Bystander(unittest.TestCase):
    def test_nothing_about_any_target(self):
        self.assertEqual(1 + 1, 2)
"""


def by_name(derived: dict[str, object]) -> str:
    return str(derived["name"])


def observer(module: str) -> str:
    return f"test_{module}.Observe.test_the_target_is_unmutated"


def entry(module: str, replace: str = "MUTATED", find: str = "") -> gate.Mutation:
    return gate.Mutation(
        f"the {module} target loses its value",
        f"{module}.py",
        find or module.upper(),
        replace,
        "a purpose-built target",
        (observer(module),),
    )


ENTRIES = (entry("first"), entry("second"), entry("third"))

# Run as a separate process so a test can signal it: a gate run over the tree named
# in the settings file, with the table and the workspace that file gives it.
HARNESS = """
import json
import signal
import sys
from pathlib import Path

# A terminal's Ctrl-C reaches a process with Python's own handler installed. A
# suite launched in the background of a non-interactive shell starts with SIGINT
# ignored, and that disposition is inherited - so without this the SIGINT case
# would measure how the suite was launched rather than how the gate is stopped.
signal.signal(signal.SIGINT, signal.default_int_handler)

settings = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sys.path.insert(0, settings["tests"])
import mutation_gate as gate

root = Path(settings["root"])
gate.ROOT = root
gate.SRC = root / "src" / "knowledgestore"
gate.RECOVERY_PATH = root / "sidecar"
gate.WORKSPACE = settings["workspace"]
gate.MUTATIONS = tuple(
    gate.Mutation(**{**raw, "observers": tuple(raw["observers"])}) for raw in settings["entries"]
)
raise SystemExit(gate.main(["--derive-mapping", "--jobs", "2"]))
"""


def git(root: Path, *arguments: str) -> None:
    """Run git in the fixture with automatic maintenance off.

    A commit otherwise starts `git maintenance run --auto` detached in the
    background, which writes `objects/maintenance.lock` into the fixture's `.git`
    whenever it gets round to it. The checks below fingerprint that directory to
    show the gate wrote nothing there, so a lock written later by the fixture's own
    commit would read as the gate's write - one run in a few hundred did, under load.
    """
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=gate",
            "-c",
            "user.email=gate@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            *arguments,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
    )


def fingerprint(root: Path) -> dict[str, str]:
    """Every file under `root`, git metadata included, by content hash.

    The git directory is in it because a copy is made by cloning, and a clone that
    wrote anything into the repository it read from would be the checkout mutated
    by a different route.
    """
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class ParallelTree(unittest.TestCase):
    """A git repository the gate is pointed at, and the helpers that read a run of it.

    No test of its own, so the classes below share the tree without each running
    the others' checks.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Resolved, because the gate compares a child's import path against its own
        # `SRC`, and macOS hands out temporary directories behind a symlink.
        base = Path(temporary.name).resolve()
        self.root = base / "checkout"
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        self.seen = base / "seen.log"
        self.ready = base / "ready"

        package = self.root / "src" / "knowledgestore"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "tests").mkdir()
        for module, source in SOURCES.items():
            (package / module).write_text(source, encoding="utf-8")
            name = module.removesuffix(".py")
            (self.root / "tests" / f"test_{module}").write_text(
                OBSERVER.format(module=module, value=name.upper()), encoding="utf-8"
            )
        (self.root / "tests" / "test_bystander.py").write_text(BYSTANDER, encoding="utf-8")
        # As the real repository ignores them: what a run writes beside the tree is not
        # part of the tree a worker copies.
        (self.root / ".gitignore").write_text("sidecar\n.mutation-gate-workers\n", encoding="utf-8")
        git(self.root, "init", "-q")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "fixture")

        for name in ("ROOT", "SRC", "RECOVERY_PATH", "MUTATIONS", "WORKSPACE"):
            self.addCleanup(setattr, gate, name, getattr(gate, name))
        gate.ROOT = self.root
        gate.SRC = package
        gate.RECOVERY_PATH = self.root / "sidecar"
        gate.WORKSPACE = str(self.workspace)
        gate.MUTATIONS = ENTRIES

        patched = mock.patch.dict(
            os.environ,
            {
                "PYTHONPATH": str(self.root / "src"),
                "PARALLEL_GATE_SEEN": str(self.seen),
                "PARALLEL_GATE_READY": str(self.ready),
            },
        )
        patched.start()
        self.addCleanup(patched.stop)

    @staticmethod
    def _main(*argv: str) -> tuple[int, str, str]:
        printed, reported = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(reported):
            code = gate.main(list(argv))
        return code, printed.getvalue(), reported.getvalue()

    @staticmethod
    def _derived(printed: str) -> list[dict[str, object]]:
        return [json.loads(line) for line in printed.splitlines() if line.startswith("{")]

    def _left_behind(self) -> list[str]:
        return sorted(path.name for path in self.workspace.iterdir())


class ParallelGateTest(ParallelTree):
    """The gate's real `main`, over that repository."""

    def test_two_workers_derive_the_mapping_one_worker_derives(self):
        """Catches the parallel path answering a different question than the serial
        one - a worker running the wrong selection, or results attached to the wrong
        entry. Both runs are compared against the mapping written out by hand, not
        only against each other, because two runs agreeing on a wrong answer is the
        shape a shared defect takes.

        The parallel run is compared by name rather than in order. Order is the next
        check's question, and it is answerable there because one entry is made slow:
        here the entries finish close together, so an ordering defect would fail this
        only when the race went one way - and an observer that fails by chance makes
        the mapping that names it flap."""
        expected = [{"name": each.name, "observers": list(each.observers)} for each in ENTRIES]

        serial, serial_printed, serial_reported = self._main("--derive-mapping", "--jobs", "1")
        parallel, parallel_printed, parallel_reported = self._main(
            "--derive-mapping", "--jobs", "2"
        )

        self.assertEqual(serial, 0, serial_reported)
        self.assertEqual(parallel, 0, parallel_reported)
        self.assertEqual(self._derived(serial_printed), expected)
        self.assertEqual(sorted(self._derived(parallel_printed), key=by_name), expected)

    def test_a_parallel_run_never_mutates_the_checkout(self):
        """Catches a worker applying its mutation to the real tree. The restore would
        put the file back, so the tree's hash afterwards cannot tell on its own - the
        observers record where they read each mutation, and every one of those reads
        has to be inside the workspace and none inside the checkout. The hash is the
        second half: nothing else in the checkout, its git metadata included, moved.
        """
        before = fingerprint(self.root)

        code, _, reported = self._main("--derive-mapping", "--jobs", "2")

        self.assertEqual(code, 0, reported)
        seen = self.seen.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(seen), len(ENTRIES), f"each entry is seen once: {seen}")
        for path in seen:
            self.assertTrue(
                path.startswith(str(self.workspace)), f"a mutation was read outside a copy: {path}"
            )
        self.assertEqual(fingerprint(self.root), before)
        self.assertEqual(self._left_behind(), [], "a worker copy outlived the run")

    def test_results_print_in_table_order_whatever_order_they_finish_in(self):
        """Catches results printed as workers finish. The first entry sleeps once its
        mutation is seen, so with two workers the other two finish before it - and a
        run that printed on arrival would put it last, in an order that changes with
        machine load (CLAUDE.md 1.3)."""
        slow = (entry("first", replace="SLOW"), entry("second"), entry("third"))
        gate.MUTATIONS = slow

        code, printed, reported = self._main("--verify-mapping", "--jobs", "2")

        self.assertEqual(code, 0, reported)
        verdicts = [line.split(None, 1)[1] for line in printed.splitlines() if "agrees" in line]
        self.assertEqual(verdicts, [each.name for each in slow])

    def test_an_entry_that_cannot_be_applied_is_reported_and_the_rest_still_run(self):
        """Catches a refusal inside a worker being lost. Serially, a `find` that is
        absent stops the run; in a worker it is one entry's answer, and it has to come
        back against that entry - not as a missing line, and not as a dead run that
        also drops the entries queued behind it."""
        absent = entry("second", find="NOT IN THE FILE")
        gate.MUTATIONS = (entry("first"), absent, entry("third"))

        code, printed, reported = self._main("--derive-mapping", "--jobs", "2")

        self.assertEqual(code, 1, printed)
        self.assertIn(f"NOT RUN: {absent.name}", reported)
        self.assertIn("no longer applies", reported)
        # As a set: which entries came back is this check's question, and the order
        # they print in is the ordering check's.
        self.assertEqual(
            sorted(each["name"] for each in self._derived(printed)),
            sorted([entry("first").name, entry("third").name]),
        )

    def test_an_entry_whose_suite_dies_is_reported_against_that_entry(self):
        """Catches a suite that ends before reporting being read as an observation.
        `_run` refuses it serially; in a worker the refusal has to reach the parent as
        that entry's failure, and the verifier must not count it as agreeing."""
        crashing = entry("second", replace="CRASH")
        gate.MUTATIONS = (entry("first"), crashing, entry("third"))

        code, printed, reported = self._main("--verify-mapping", "--jobs", "2")

        self.assertEqual(code, 1, printed)
        self.assertIn(f"NOT RUN: {crashing.name}", reported)
        self.assertIn("reported nothing", reported)
        self.assertIn("2 of 3 entries name what observes them", printed)

    def _kill_when_held(self, runs: int) -> threading.Thread:
        """A thread that SIGKILLs every worker holding a `HANG` run, once `runs` are held,
        and the suite run under each - which is how a worker dies with no cleanup."""

        def kill() -> None:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if self.ready.is_file():
                    lines = self.ready.read_text(encoding="utf-8").splitlines()
                    if len(lines) >= runs:
                        break
                time.sleep(0.05)
            time.sleep(0.2)
            for pid in self.ready.read_text(encoding="utf-8").split():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid), signal.SIGKILL)

        killer = threading.Thread(target=kill)
        killer.start()
        return killer

    def test_a_worker_that_dies_is_reported_and_its_queue_is_finished_by_the_others(self):
        """Catches a dead worker taking its entry, or the entries behind it, with it.
        The worker holding the first entry is killed while that entry's suite runs; the
        entry must come back as not run, naming the worker, and the other worker must
        still derive the two entries left in the queue."""
        held = entry("first", replace="HANG")
        gate.MUTATIONS = (held, entry("second"), entry("third"))

        killer = self._kill_when_held(1)
        code, printed, reported = self._main("--derive-mapping", "--jobs", "2")
        killer.join()

        self.assertEqual(code, 1, printed)
        self.assertIn(f"NOT RUN: {held.name}", reported)
        self.assertIn("worker", reported)
        self.assertEqual(
            [each["name"] for each in self._derived(printed)],
            [entry("second").name, entry("third").name],
        )
        self.assertEqual(self._left_behind(), [], "a worker copy outlived the run")

    def test_entries_left_when_every_worker_has_died_are_reported_not_run(self):
        """Catches the queue being abandoned silently, or the run hanging on it, once
        no worker is left to take it. Both workers are killed while each holds an
        entry, so the third entry is never handed to anyone - and it has to come back
        named, as not run, rather than missing from a count of what did."""
        first, second, third = (
            entry("first", replace="HANG"),
            entry("second", "HANG"),
            entry("third"),
        )
        gate.MUTATIONS = (first, second, third)

        killer = self._kill_when_held(2)
        code, printed, reported = self._main("--derive-mapping", "--jobs", "2")
        killer.join()

        self.assertEqual(code, 1, printed)
        for each in (first, second, third):
            self.assertIn(f"NOT RUN: {each.name}", reported)
        self.assertIn("every worker had stopped", reported)
        self.assertEqual(self._derived(printed), [])
        self.assertEqual(self._left_behind(), [], "a worker copy outlived the run")

    def test_a_copy_whose_suite_imports_another_tree_is_refused(self):
        """Catches the import-path guard holding for the checkout and not for a copy.
        The package's `__init__.py` is ignored here, so the checkout has it and every
        copy lacks it: the checkout passes the guard, and a copy's suite would import
        some other `knowledgestore` and conclude about code nobody is changing."""
        (self.root / ".gitignore").write_text(
            "sidecar\n.mutation-gate-workers\nsrc/knowledgestore/__init__.py\n", encoding="utf-8"
        )
        git(self.root, "rm", "-q", "--cached", "src/knowledgestore/__init__.py")
        git(self.root, "commit", "-q", "-am", "ignore the package marker")

        with self.assertRaises(SystemExit) as raised:
            self._main("--derive-mapping", "--jobs", "2")

        message = str(raised.exception)
        self.assertIn("imports knowledgestore", message)
        self.assertIn(str(self.workspace), message)
        self.assertFalse(self.seen.exists(), "a mutation ran in a copy the guard refused")
        self.assertEqual(self._left_behind(), [], "a refused copy outlived the run")

    def test_a_worker_imports_from_its_copy_whatever_the_path_held(self):
        """Catches a worker's path still reaching the checkout. With no `PYTHONPATH`
        at all - an editable install - the copy's `src` has to be put first, or the
        install resolves the package to the checkout and every mutation in the copy is
        invisible; an entry naming the checkout's `src` has to be moved to the copy's;
        and an entry elsewhere is left alone, because it is not this tree's to move.
        The expected paths are written out rather than derived."""
        copy = self.workspace / "worker-1"
        elsewhere = str(self.root.parent / "elsewhere")
        cases = (
            ("", [str(copy / "src")]),
            (str(self.root / "src"), [str(copy / "src")]),
            (os.pathsep.join([elsewhere, str(self.root / "src")]), [str(copy / "src"), elsewhere]),
        )
        for written, expected in cases:
            with (
                self.subTest(written=written),
                mock.patch.dict(os.environ, {"PYTHONPATH": written}),
            ):
                path = gate._worker_environment(copy)["PYTHONPATH"]  # noqa: SLF001
                self.assertEqual(path.split(os.pathsep), expected)

    def test_jobs_is_refused_where_nothing_would_run_in_parallel(self):
        """Catches `--jobs` accepted and ignored by the fast sweep, which stays serial:
        an operator asking for workers there would wait the serial time while
        believing they had asked for less."""
        with self.assertRaises(SystemExit) as raised:
            self._main("--jobs", "2")

        self.assertIn("--jobs", str(raised.exception))


class StatusSeesAParallelRunTest(ParallelTree):
    """`--status` and the run marker, against a gate run in another process."""

    def _spawn(self, entries: tuple[gate.Mutation, ...]) -> subprocess.Popen[bytes]:
        settings = self.root.parent / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "tests": str(TESTS),
                    "root": str(self.root),
                    "workspace": str(self.workspace),
                    "entries": [
                        {
                            "name": each.name,
                            "module": each.module,
                            "find": each.find,
                            "replace": each.replace,
                            "escaped_as": each.escaped_as,
                            "observers": list(each.observers),
                        }
                        for each in entries
                    ],
                }
            ),
            encoding="utf-8",
        )
        harness = self.root.parent / "harness.py"
        harness.write_text(HARNESS, encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, "-B", str(harness), str(settings)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self._reap, process)
        return process

    def _reap(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.kill()
        process.communicate()
        if self.ready.is_file():
            for pid in self.ready.read_text(encoding="utf-8").split():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid), signal.SIGKILL)

    def _wait_until_mutated(self, process: subprocess.Popen[bytes]) -> list[int]:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.ready.is_file() and self.ready.read_text(encoding="utf-8").strip():
                return [int(pid) for pid in self.ready.read_text(encoding="utf-8").split()]
            if process.poll() is not None:
                self.fail(f"the run exited before mutating: {process.communicate()[1]!r}")
            time.sleep(0.05)
        self.fail("the run never reported a mutation applied")

    @staticmethod
    def _status() -> tuple[int, dict[str, object]]:
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(io.StringIO()):
            code = gate.status(as_json=True)
        return code, json.loads(printed.getvalue().splitlines()[0])

    @staticmethod
    def _gone(pids: list[int]) -> bool:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            alive = []
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    alive.append(pid)
                except ProcessLookupError:
                    pass
            if not alive:
                return True
            time.sleep(0.1)
        return False

    def test_an_interrupted_run_removes_every_worker_copy(self):
        """Catches the copies outliving an interrupt. SIGTERM is what a timeout, a job
        kill and a cancelled CI step send, and SIGINT is Ctrl-C; each has to end with
        the workspace empty, the marker gone, the workers and their suites stopped,
        and the checkout exactly as it was.

        Sent to the parent alone rather than its process group, because that is the
        hard case: a terminal's Ctrl-C reaches every worker by itself, and a `kill`
        reaches only the pid it names."""
        before = fingerprint(self.root)
        for number in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signal.Signals(number).name):
                self.ready.unlink(missing_ok=True)
                process = self._spawn((entry("first", replace="HANG"), entry("second")))
                pids = self._wait_until_mutated(process)
                self.assertNotEqual(self._left_behind(), [], "the run made no copy to remove")

                process.send_signal(number)
                process.communicate(timeout=60)

                self.assertNotEqual(process.returncode, 0, "an interrupted run reported success")
                self.assertEqual(self._left_behind(), [], "a worker copy outlived the interrupt")
                self.assertFalse(gate.workers_path().exists(), "the run marker outlived the run")
                self.assertTrue(self._gone(pids), f"a worker or its suite survived: {pids}")
                self.assertEqual(fingerprint(self.root), before)

    def test_status_names_a_live_parallel_run_and_says_the_tree_is_not_mutated(self):
        """Catches `--status` going blind to a parallel run. The tree is not mutated,
        so the pre-commit hook it backs must still pass - but a reader asking whether
        a gate run holds this checkout has to be told one does, and which pid to
        signal."""
        process = self._spawn((entry("first", replace="HANG"),))
        self._wait_until_mutated(process)

        code, reported = self._status()

        self.assertEqual(code, 0, reported)
        self.assertFalse(reported["applied"])
        self.assertEqual(reported["workers"]["state"], gate.LIVE)
        self.assertEqual(reported["workers"]["pid"], process.pid)

    def test_a_second_parallel_run_beside_a_live_one_is_refused_and_leaves_it_visible(self):
        """Catches a refused run cleaning up what is not its own. Two runs would each
        take the marker for theirs, so the second is refused - and its way out must not
        delete the first's marker or copies, or `--status` goes blind to a run that is
        still mutating its copies."""
        process = self._spawn((entry("first", replace="HANG"),))
        self._wait_until_mutated(process)
        held = self._left_behind()

        with self.assertRaises(SystemExit) as raised:
            self._main("--derive-mapping", "--jobs", "2")

        self.assertIn("live", str(raised.exception))
        self.assertIn(str(process.pid), str(raised.exception))
        self.assertEqual(self._status()[1]["workers"]["state"], gate.LIVE)
        self.assertEqual(self._left_behind(), held, "the refused run touched the live one's copies")

    def test_a_killed_run_is_reported_abandoned_and_its_copies_are_reclaimed(self):
        """Catches the marker going stale silently. SIGKILL cannot be handled, so the
        copies stay behind; `--status` has to call the run abandoned rather than live,
        and the next parallel run has to remove what it left before making its own."""
        process = self._spawn((entry("first", replace="HANG"),))
        pids = self._wait_until_mutated(process)
        process.kill()
        process.communicate()
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        left = self._left_behind()
        self.assertNotEqual(left, [], "SIGKILL left nothing, so this checks nothing")

        code, reported = self._status()
        self.assertEqual(code, 0, reported)
        self.assertEqual(reported["workers"]["state"], gate.ABANDONED)

        self.ready.unlink()
        code, _, stderr = self._main("--derive-mapping", "--jobs", "2")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self._left_behind(), [], f"{left} was not reclaimed")


class JobsArgumentTest(unittest.TestCase):
    def test_a_count_is_taken_as_written_and_auto_is_half_the_cores(self):
        """The parse, and `auto` against the core count it is defined by, including a
        machine that reports one core or none."""
        self.assertEqual(gate.jobs_argument("1"), 1)
        self.assertEqual(gate.jobs_argument("6"), 6)
        for cores, expected in ((12, 6), (3, 1), (1, 1), (None, 1)):
            with (
                self.subTest(cores=cores),
                mock.patch.object(gate.os, "cpu_count", return_value=cores),
            ):
                self.assertEqual(gate.jobs_argument("auto"), expected)

    def test_a_count_that_names_no_workers_is_refused(self):
        """Catches `0` or a typo running no workers - every mode here reports an empty
        run as a pass."""
        for written in ("0", "-2", "two", "", "1.5"):
            with self.subTest(written=written):
                with self.assertRaises(SystemExit) as raised:
                    gate.jobs_argument(written)
                self.assertIn("--jobs", str(raised.exception))


class ParallelSettleTest(ParallelTree):
    """`observer_staleness --settle`, with its entries spread over workers."""

    def test_two_workers_settle_what_one_worker_settles(self):
        """Catches settling in parallel reading a different answer. The second entry
        names the wrong test, so the arrived `test_second` observes it unnamed: STALE,
        with exactly that entry and that test, from either path."""
        wrong = gate.Mutation(
            "the second target loses its value",
            "second.py",
            "SECOND",
            "MUTATED",
            "a purpose-built target",
            (observer("first"),),
        )
        table = (entry("first"), wrong, entry("third"))
        arrived = {"test_second": ("Observe.test_the_target_is_unmutated",)}
        before = fingerprint(self.root)

        with contextlib.redirect_stderr(io.StringIO()):
            serial = staleness.settle(table, arrived, "the base", 600.0, jobs=1)
            self.seen.unlink()
            parallel = staleness.settle(table, arrived, "the base", 600.0, jobs=2)

        # Where the arrival saw the mutation: in a worker's copy, or the parallel
        # answer was the serial one by another name.
        seen = self.seen.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(seen), 1, seen)
        self.assertTrue(seen[0].startswith(str(self.workspace)), seen)

        for report in (serial, parallel):
            self.assertEqual(report.verdict, staleness.STALE)
            self.assertEqual(
                [(stale.entry, stale.unnamed) for stale in report.stale],
                [(wrong.name, (observer("second"),))],
            )
        self.assertEqual(fingerprint(self.root), before)
        self.assertEqual(self._left_behind(), [])


class BudgetIsWallTimeTest(unittest.TestCase):
    def test_workers_divide_the_entries_a_module_is_run_under(self):
        """Catches the budget ignoring `--jobs`. The budget bounds how long the job
        waits, and with N workers each module is run under about entries/N entries
        one after another rather than under all of them - so a module that does not
        fit serially can fit in parallel, and the projection printed for one that
        does not has to be the parallel figure too."""
        timings = {"test_slow": 10.0}

        self.assertEqual(staleness.affordable(timings, 6, 30.0, jobs=1), ((), ("test_slow",)))
        self.assertEqual(staleness.affordable(timings, 6, 30.0, jobs=3), (("test_slow",), ()))
        # Seven entries over three workers is three rounds, not two and a third.
        self.assertEqual(staleness.affordable(timings, 7, 20.0, jobs=3), ((), ("test_slow",)))


if __name__ == "__main__":
    unittest.main()
