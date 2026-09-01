"""A mutation-gate run must be visible from outside the process that started it.

`tests/mutation_gate.py` puts a deliberately introduced defect into a real file
under `src/` for as long as the test modules that entry names take to run, then
restores it. While that is true the working tree cannot be read for anything, and
nothing on disk says so: `git status` reports an ordinary edit. Three readers were
misled by that in one day (issue #268). One staged the mutated file and nearly
committed it. One checked the suite was green before starting a long job and
refused against a tree that ran green minutes later. And one, stopping a run it
believed was stale, matched the script's name with `pgrep -f` - which finds every
checkout on the machine - and killed the wrapper of a different worktree's run.

The record the gate already writes before it touches a file is the signal. What
it lacked was identity: "a mutation is applied" does not say which checkout holds
it or which process to signal, so a stale record and a live run read the same.

The breaks these tests catch:

- the record stops naming the tree and the process, so a reader can neither tell
  which checkout is mutated nor find the pid to signal;
- liveness decided from the pid alone, which a reused pid answers `live` for -
  the reading that sends someone to wait for a run that died days ago, and the
  reading that makes a stale record indistinguishable from a live one;
- the query reporting a clean tree while a mutation is applied, which is the
  failure that reads as protection;
- the pre-commit hook losing the query, so a live mutation can be committed
  again.

Real processes and a real `ps` wherever the question is about this machine: this
test's own pid is a live process and a child that has exited and been reaped is a
dead one, so the decision is exercised against the thing it is about, and the
expected start time is read by a `ps` call of this module's own rather than by the
function under test. The `run=` seam stands in only for pid reuse, which cannot be
arranged on demand.

The hook is exercised by running the command the committed
`.pre-commit-config.yaml` names, in a throwaway tree holding a copy of the gate,
so what is driven is the hook's own command rather than a restatement of it. Never
this repository's own record: a gate run may legitimately be holding one, and a
test that wrote there would corrupt it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import mutation_gate as gate

try:  # PyYAML is the `deploy` extra, not a runtime dependency of this library
    import yaml

    HAS_YAML = True
except ImportError:  # pragma: no cover - the default-install CI job takes this path
    HAS_YAML = False

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
CONFIG = ROOT / ".pre-commit-config.yaml"
HOOK_ID = "mutation-gate-live"

ORIGINAL = b"# harness target\nVALUE = 'ORIGINAL'\n"

needs_yaml = unittest.skipUnless(HAS_YAML, "needs the `deploy` extra (PyYAML)")


def _ps(pid: int) -> str:
    """The start time `ps` reports for a pid, read independently of the gate: the
    expected value in a test must not come from the code under test."""
    completed = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _header(module: str, **fields: object) -> dict[str, object]:
    """A record header written out by hand, field by field, so a header the gate
    stops writing is a failure here rather than a value derived from itself."""
    header: dict[str, object] = {
        "module": module,
        "root": "",
        "pid": 0,
        "host": "",
        "started": "",
    }
    header.update(fields)
    return header


def _record(module: str, original: bytes, **fields: object) -> bytes:
    return json.dumps(_header(module, **fields)).encode("utf-8") + b"\n" + original


class MutationGateVisibilityTest(unittest.TestCase):
    def _tree(self) -> Path:
        """A throwaway source directory the gate can be pointed at, with the real
        module globals restored afterwards."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        (root / "source").mkdir()
        (root / "source" / "target.py").write_bytes(ORIGINAL)
        self.addCleanup(setattr, gate, "SRC", gate.SRC)
        self.addCleanup(setattr, gate, "RECOVERY_PATH", gate.RECOVERY_PATH)
        gate.SRC = root / "source"
        gate.RECOVERY_PATH = root / "sidecar"
        return root

    def _live_record(self, module: str = "target.py") -> bytes:
        """A record describing this test process, which is genuinely running."""
        return _record(
            module,
            ORIGINAL,
            root=str(gate.ROOT),
            pid=os.getpid(),
            host=socket.gethostname(),
            started=_ps(os.getpid()),
        )

    @staticmethod
    def _status(**keywords) -> tuple[int, str, str]:
        out, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(error):
            code = gate.status(**keywords)
        return code, out.getvalue(), error.getvalue()

    def test_the_record_apply_writes_names_the_tree_and_the_process(self):
        """Catches a record that says only that a mutation exists. Without the root
        a reader cannot tell which checkout is mutated, and without the pid the only
        way to find the run is to match process arguments - which matches every
        checkout on the machine and killed the wrong one."""
        root = self._tree()
        mutation = gate.Mutation(
            "harness", "target.py", "ORIGINAL", "MUTATED", "a purpose-built target"
        )

        original = gate.apply(mutation)
        try:
            record = gate.read_record()
        finally:
            gate.restore(mutation.module, original)

        self.assertIsNotNone(record, "apply wrote no record a reader can parse")
        assert record is not None
        self.assertEqual(record.module, "target.py")
        self.assertEqual(record.original, ORIGINAL, "the record must carry the exact original")
        self.assertEqual(record.root, str(gate.ROOT), "the record does not name the checkout")
        self.assertEqual(record.pid, os.getpid(), "the record does not name the process")
        self.assertEqual(record.host, socket.gethostname())
        self.assertEqual(
            record.started,
            _ps(os.getpid()),
            "without the pid's start time a reused pid reads as a live run",
        )
        self.assertFalse((root / "sidecar").exists(), "the restore left the record behind")

    def test_liveness_needs_the_tree_the_host_the_pid_and_its_start_time_to_agree(self):
        """Catches liveness decided from the pid alone. A pid is reused, so `live` on
        a pid that merely exists reports a run that died days ago as still going -
        which is the stale-versus-live confusion this whole change is about. The last
        three cases are the other half: where the record cannot be attributed to this
        machine and tree, the answer has to be "cannot tell" rather than a guess."""
        here, mine = socket.gethostname(), str(gate.ROOT)
        cases = [
            ("the recorded process is still running", mine, here, "STAMP", "STAMP", gate.LIVE),
            ("the pid belongs to something else now", mine, here, "STAMP", "LATER", gate.ABANDONED),
            ("no process holds the pid", mine, here, "STAMP", "", gate.ABANDONED),
            ("another checkout wrote it", "/elsewhere", here, "STAMP", "STAMP", gate.ELSEWHERE),
            ("another machine wrote it", mine, "other-host", "STAMP", "STAMP", gate.ELSEWHERE),
            ("no start time was recorded", mine, here, "", "", gate.UNDECIDABLE),
        ]
        for label, root, host, started, reported, expected in cases:
            with self.subTest(label):
                record = gate.Record(
                    module="target.py",
                    original=b"",
                    root=root,
                    pid=4242,
                    host=host,
                    started=started,
                )
                self.assertEqual(
                    gate.liveness(record, run=lambda _pid, answer=reported: answer),
                    expected,
                    label,
                )

    def test_liveness_reads_a_real_running_process_and_a_real_exited_one(self):
        """The sensitivity control for the stubbed table above, against real `ps`: a
        decision that answered `undecidable` everywhere, or `live` everywhere, would
        satisfy a stub and be useless here. Also the one place the two answers are
        shown to differ for a process nobody described."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(self._reap, child)
        started = _ps(child.pid)
        self.assertNotEqual(started, "", "ps reported nothing for a process that is running")
        record = gate.Record(
            module="target.py",
            original=b"",
            root=str(gate.ROOT),
            pid=child.pid,
            host=socket.gethostname(),
            started=started,
        )

        self.assertEqual(gate.liveness(record), gate.LIVE)

        child.terminate()
        child.wait(timeout=60)

        self.assertEqual(
            gate.liveness(record),
            gate.ABANDONED,
            "a process that has exited and been reaped still read as a live run",
        )

    @staticmethod
    def _reap(child: subprocess.Popen[bytes]) -> None:
        if child.poll() is None:
            child.kill()
        child.wait()

    def test_the_query_reports_a_clean_tree_and_exits_zero(self):
        """The sensitivity half of the refusal below, and the half that matters more:
        a query that refused whatever the tree held would pass every other test here
        and block every commit."""
        self._tree()

        code, out, error = self._status()

        self.assertEqual(code, 0, f"a clean tree was refused: {error}")
        self.assertIn(str(gate.ROOT), out)
        self.assertEqual(error, "", "nothing is wrong, so nothing belongs on stderr")

    def test_the_query_refuses_while_a_mutation_is_applied_and_names_the_path(self):
        """Catches a query that reads as clean while a source file holds a deliberately
        introduced defect - the case a person about to commit needs it for. The path
        has to be in the message: "something is mutated" leaves the reader looking."""
        root = self._tree()
        (root / "sidecar").write_bytes(self._live_record())

        code, _, error = self._status()

        self.assertEqual(code, 1, "a mutated tree was reported as clean")
        self.assertIn(str(root / "source" / "target.py"), error, "the refusal names no path")
        self.assertIn(str(os.getpid()), error, "the refusal names no process to signal")
        self.assertIn("kill -TERM", error, "the refusal does not say how to stop the run safely")
        self.assertIn(
            "-d cwd",
            error,
            "the refusal forbids a `pgrep` without saying how to find the checkout instead",
        )

    def test_the_live_remedy_says_how_to_find_which_checkout_a_run_holds(self):
        """Catches the remedy keeping the prohibition and losing the alternative.

        A gate's own arguments are `tests/mutation_gate.py --verify-mapping`: the
        checkout it holds is its working directory and appears nowhere in them. So
        every way of asking `ps` about the script name answers a different question -
        a pattern naming the checkout matches nothing however many gates run, and one
        naming the script matches every clone on the machine. Both have happened here
        within a day, in opposite directions, and each cost a run: one killed a
        sibling worktree's, one nearly published five fabricated findings from two
        gates mutating one tree.

        Telling the reader not to `pgrep` leaves them without the thing they wanted,
        which is which checkout this process is holding. This pins that the remedy
        carries the way to get it, not just the way not to.
        """
        root = self._tree()
        (root / "sidecar").write_bytes(
            _record(
                "target.py",
                ORIGINAL,
                root=str(gate.ROOT),
                pid=os.getpid(),
                host=socket.gethostname(),
                started=gate.run_ps(os.getpid()),
            )
        )

        code, _, error = self._status()

        self.assertEqual(code, 1)
        self.assertIn("lsof", error, "the remedy names no way to read a process's directory")
        self.assertIn("-d cwd", error, "the remedy reads something other than the directory")
        self.assertIn(
            "pgrep -f mutation_gate.py",
            error,
            "the remedy enumerates gates by a pattern that does not find them",
        )

    def test_the_query_tells_an_abandoned_record_from_a_live_run(self):
        """Catches the distinction collapsing. Both readings are expensive and they
        are opposite: told a dead run is live, a session waits for a run that will
        never finish, and told a live run is dead it kills a working one - which
        happened, in the wrong worktree."""
        root = self._tree()
        (root / "sidecar").write_bytes(
            _record(
                "target.py",
                ORIGINAL,
                root=str(gate.ROOT),
                pid=os.getpid(),
                host=socket.gethostname(),
                # A stamp `ps` cannot report for this pid: whatever holds it now, it
                # is not the process that wrote this record.
                started="Thu Jan  1 00:00:00 1970",
            )
        )

        code, _, error = self._status()

        self.assertEqual(code, 1)
        self.assertIn(str(root / "source" / "target.py"), error)
        self.assertIn("mutation_gate.py", error, "an abandoned record's remedy is to recover")
        self.assertNotIn(
            "kill -TERM",
            error,
            "nothing is running, so telling the reader to signal a pid sends them at "
            "whatever holds it now",
        )

    def test_the_query_carries_the_pid_for_a_caller_that_wants_to_signal_it(self):
        """Catches a machine-readable answer that a script cannot act on. Without the
        pid in it the only way to find the run is to match command lines, which is the
        thing that killed another worktree's run."""
        root = self._tree()
        (root / "sidecar").write_bytes(self._live_record())

        code, out, _ = self._status(as_json=True)
        payload = json.loads(out)

        self.assertEqual(code, 1)
        self.assertTrue(payload["applied"])
        self.assertEqual(payload["pid"], os.getpid())
        self.assertEqual(payload["root"], str(gate.ROOT))
        self.assertEqual(payload["path"], str(root / "source" / "target.py"))
        self.assertEqual(payload["state"], gate.LIVE)

        (root / "sidecar").unlink()
        clean_code, clean_out, _ = self._status(as_json=True)

        self.assertEqual(clean_code, 0)
        self.assertFalse(json.loads(clean_out)["applied"])

    def test_a_record_the_query_cannot_read_is_refused_rather_than_ignored(self):
        """Catches a record shrugged off because it will not parse. Something wrote it
        before mutating a file, so the file may still be mutated; reading past it
        reports the tree as safe on exactly the evidence that says it is not."""
        root = self._tree()
        (root / "sidecar").write_bytes(b"not a header at all\n" + ORIGINAL)

        code, _, error = self._status()

        self.assertEqual(code, 1, "an unreadable record was treated as no record")
        self.assertIn(str(root / "sidecar"), error, "the refusal names no record to remove")

    @needs_yaml
    def test_the_pre_commit_hook_runs_the_query_and_refuses_a_live_mutation(self):
        """Catches the hook losing the check, or the check losing the hook. Runs the
        command the committed configuration names, in a throwaway tree carrying a copy
        of the gate: with a record present the command fails and names the mutated
        path, without one it succeeds. A hook that cannot tell those apart reads as
        protection and is none."""
        entry = self._hook_entry()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        (root / "tests").mkdir()
        (root / "src" / "knowledgestore").mkdir(parents=True)
        shutil.copy(TESTS / "mutation_gate.py", root / "tests" / "mutation_gate.py")
        (root / "src" / "knowledgestore" / "target.py").write_bytes(ORIGINAL)
        record = root / "tests" / ".mutation-gate-recovery"
        record.write_bytes(
            _record(
                "target.py",
                ORIGINAL,
                root=str(root),
                pid=os.getpid(),
                host=socket.gethostname(),
                started=_ps(os.getpid()),
            )
        )

        refused = subprocess.run(
            shlex.split(entry), cwd=root, capture_output=True, text=True, check=False
        )

        self.assertEqual(
            refused.returncode,
            1,
            f"the hook command allowed a commit with a mutation applied: {refused.stderr}",
        )
        self.assertIn(
            str(root / "src" / "knowledgestore" / "target.py"),
            refused.stderr,
            "the hook's refusal does not name the mutated file",
        )

        record.unlink()
        allowed = subprocess.run(
            shlex.split(entry), cwd=root, capture_output=True, text=True, check=False
        )

        self.assertEqual(
            allowed.returncode,
            0,
            f"the hook command refuses a clean tree, which blocks every commit: {allowed.stderr}",
        )

    def _hook_entry(self) -> str:
        """The command the pre-commit hook runs, read from the configuration that
        ships rather than restated here - a hook removed from it must fail this."""
        configured = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        entries = [
            hook["entry"]
            for repository in configured["repos"]
            for hook in repository["hooks"]
            if hook["id"] == HOOK_ID
        ]
        self.assertEqual(
            len(entries),
            1,
            f"{CONFIG.name} declares {len(entries)} hooks with id {HOOK_ID!r}: a commit is "
            "only refused while one of them runs the query",
        )
        return entries[0]


if __name__ == "__main__":
    unittest.main()
