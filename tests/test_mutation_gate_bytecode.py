"""A mutation-gate suite run must exercise the file on disk, not a cached `.pyc`.

CPython invalidates a cached `.pyc` on the source's `(mtime seconds, size)` pair.
The gate rewrites one source file per mutation, runs the suite in a subprocess and
restores - so two mutations of one module that produce a file of identical size
inside the same wall-clock second are indistinguishable to that check, and the
second run imports the first mutation's bytecode. Same-size pairs are routine: the
natural mutation swaps one expression for another of similar length.

The result is not a crash but a spurious `caught` or `SURVIVED` - the gate
reporting confidently about a mutation it never ran (issue #228). Two mutations
producing files of 56,347 bytes were observed doing exactly that.

Two halves, in the order they matter:

- the stale read is constructed directly, on this interpreter, with `os.utime`
  forcing the identical mtime rather than racing the clock, and shown to happen
  without `PYTHONDONTWRITEBYTECODE` and not to happen with it. That is this
  module's sensitivity check: it fails if the mechanism it describes ever stops
  being real, which is the only way the fix could become decoration.
- the environment `run_suite` hands its subprocess is read back from a real
  subprocess of a real suite run, so what is asserted is what the child actually
  got: the variable at a value CPython honours, and every inherited variable
  still present. A fix that dropped `PYTHONPATH` would make every suite run
  exercise the installed package instead of `src/`, which is worse than the defect
  being fixed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import mutation_gate as gate

# Two bodies of identical length, so `(mtime, size)` cannot tell them apart once
# the mtimes match. Hand-counted: 16 bytes each.
FIRST_BODY = 'VALUE = "first"\n'
SECOND_BODY = 'VALUE = "other"\n'

# A fixed mtime for both writes. Waiting for two writes to land inside the same
# second would make this test a race; forcing the stat the cache is validated
# against makes the collision certain on every run.
STAMP = 1_700_000_000.0

# Discovered and run by the real `run_suite` in a throwaway tree. It records the
# environment of the process the gate started, which is the only place the
# question can be answered - the parent's own environment is not evidence of what
# it passed on. Where it writes is derived from its own path rather than from a
# variable, so an environment the gate got wrong still produces an observation to
# assert against instead of a suite that fails for a second reason.
OBSERVER = """
import json
import os
import sys
import unittest
from pathlib import Path


class Observe(unittest.TestCase):
    def test_record_the_environment(self):
        Path(__file__).resolve().parent.parent.joinpath("observation.json").write_text(
            json.dumps(
                {"environ": dict(os.environ), "dont_write_bytecode": sys.dont_write_bytecode}
            ),
            encoding="utf-8",
        )
"""


class SuiteSubprocessBytecodeTest(unittest.TestCase):
    def test_a_same_size_rewrite_at_the_same_mtime_is_read_from_stale_bytecode(self):
        """Catches this module going vacuous. Everything here rests on CPython
        answering an import from a `.pyc` whose source has changed underneath it,
        so the claim is made against the interpreter running the suite rather than
        from the documentation: without the variable the second import returns the
        first body, with it the second import returns the second body and no
        bytecode is cached at all."""
        stale = self._import_twice(bytecode_written=True)
        self.assertEqual(
            stale["second"],
            "first",
            "the second import did not read stale bytecode, so nothing in this module "
            "is protecting anything: either the invalidation rule changed or the two "
            "bodies are no longer the same size at the same mtime",
        )
        self.assertTrue(
            stale["cached"],
            "no .pyc was written, so the first import cannot have cached anything and "
            "the check above proved nothing",
        )

        fresh = self._import_twice(bytecode_written=False)
        self.assertEqual(
            fresh["second"],
            "other",
            "PYTHONDONTWRITEBYTECODE=1 did not stop the stale read, which is the whole "
            "reason run_suite sets it",
        )
        self.assertFalse(fresh["cached"], "bytecode was cached despite PYTHONDONTWRITEBYTECODE=1")
        self.assertEqual(stale["first"], "first")
        self.assertEqual(fresh["first"], "first")

    def test_the_suite_subprocess_is_told_not_to_write_bytecode(self):
        """Catches `run_suite` passing no environment, or an environment CPython does
        not honour - `PYTHONDONTWRITEBYTECODE=""` reads as unset, so a value is as
        load-bearing as the name. Read from inside the child, because the gate's
        promise is about the process it starts."""
        observation = self._observe_a_real_suite_run()

        self.assertIs(
            observation["dont_write_bytecode"],
            True,
            "the child ran with bytecode writing enabled, whatever the variable said: "
            f"PYTHONDONTWRITEBYTECODE={observation['environ'].get('PYTHONDONTWRITEBYTECODE')!r}",
        )
        self.assertEqual(observation["environ"].get("PYTHONDONTWRITEBYTECODE"), "1")

    def test_the_suite_subprocess_keeps_the_environment_it_inherits(self):
        """Catches an environment replaced rather than extended. `PYTHONPATH` is how
        the suite reaches `src/` on a machine holding a non-editable install of this
        library, so dropping it silently moves every mutation run onto the installed
        package - a green gate over code that is not in the tree."""
        observation = self._observe_a_real_suite_run()

        self.assertEqual(observation["environ"].get("PYTHONPATH"), os.environ["PYTHONPATH"])
        self.assertEqual(observation["environ"].get("GATE_INHERITED"), "kept")

    def _observe_a_real_suite_run(self) -> dict[str, Any]:
        """Run the gate's own `run_suite` over a throwaway suite that records its
        environment. Real subprocess, real discovery, the gate's real code."""
        root = Path(self._workspace())
        (root / "tests").mkdir()
        (root / "tests" / "test_observer.py").write_text(OBSERVER, encoding="utf-8")
        observation = root / "observation.json"

        environment = {
            "GATE_INHERITED": "kept",
            # Set explicitly rather than read from the operator's shell: the
            # assertion is that what this process holds arrives in the child, and
            # an absent variable would make it pass by vacuity.
            "PYTHONPATH": os.pathsep.join([str(root / "on-the-path"), str(root)]),
        }
        patched = mock.patch.dict(os.environ, environment)
        patched.start()
        self.addCleanup(patched.stop)
        # The child must not be able to inherit the variable under test, or a
        # run_suite that passes nothing at all would look correct.
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)

        self.addCleanup(setattr, gate, "ROOT", gate.ROOT)
        gate.ROOT = root

        self.assertTrue(
            gate.run_suite(), "the throwaway suite did not pass, so it observed nothing"
        )
        self.assertTrue(observation.is_file(), "the throwaway suite never ran")
        return json.loads(observation.read_text(encoding="utf-8"))

    def _import_twice(self, *, bytecode_written: bool) -> dict[str, object]:
        """Import a module, rewrite it to a same-size body at the same mtime, import
        again. A fresh directory each time, because `PYTHONDONTWRITEBYTECODE` stops
        bytecode being written and not being read: a `.pyc` left by an earlier
        scenario would be used by a later one."""
        directory = self._workspace()
        module = Path(directory, "probe.py")

        self._write(module, FIRST_BODY)
        first = self._value_of(directory, bytecode_written=bytecode_written)
        cached = any(Path(directory).rglob("*.pyc"))

        self._write(module, SECOND_BODY)
        second = self._value_of(directory, bytecode_written=bytecode_written)
        return {"first": first, "second": second, "cached": cached}

    @staticmethod
    def _write(module: Path, body: str) -> None:
        module.write_text(body, encoding="utf-8")
        os.utime(module, (STAMP, STAMP))

    @staticmethod
    def _value_of(directory: str, *, bytecode_written: bool) -> str:
        """`probe.VALUE` as a fresh interpreter sees it. The environment is built by
        hand so the operator's own shell cannot decide the scenario, and
        PYTHONPYCACHEPREFIX is dropped so a cached file lands where the caller looks
        for it."""
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"}
        }
        if not bytecode_written:
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-c", "import probe; print(probe.VALUE)"],
            cwd=directory,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    def _workspace(self) -> str:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return temporary.name


if __name__ == "__main__":
    unittest.main()
