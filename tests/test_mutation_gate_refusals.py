"""What the mutation gate checks before it starts, and what its refusals name.

`tests/mutation_gate.py` spawns the suite in `tests/` with the environment it was
given, so a relative `PYTHONPATH=src` - the form the testing instructions
document - resolves to `tests/src` in that child, which does not exist. The child
then imports whatever `knowledgestore` is installed, and no mutation of this tree
is visible to any run: every entry survives, or `--derive-mapping` reports every
one as observed by nothing. Neither symptom points at the cause, and two
operators lost about forty minutes to it on one day (#269).

Watching for the symptoms is not enough, which is why this is a precondition
rather than a heuristic. The gate already warned when *every* entry was
unobserved and that warning did not fire, because an entry targeting a file
outside the installed package - `tests/`, `docs/` - is still seen: one run
reported 7 entries observed and 72 observed by nothing, and printed nothing. A
partial wipeout looks like a mapping rather than like a broken run.

So the checks here drive the gate's real `check_import_path` and `main` over a
purpose-built tree holding two copies of a package with the same name, one of
them where this gate expects it. The child that decides is a real subprocess with
a real `PYTHONPATH`, because that is the whole mechanism - stubbing the import
would stub the thing under test. The gate's `run=` seam is used once, for the
case a machine holding an install cannot produce: a child that imports nothing at
all.

The second half is about the refusal above it. "The suite is already failing"
named no module, which is fine when the reader is already looking at a red suite
and expensive in the case that happens: a table-level guard failing while every
test in the module under edit passes. The name was available to the gate and
thrown away.
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

ORIGINAL = 'VALUE = "ORIGINAL"\n'

# Reads the file the entry below mutates, so it fails while the mutation is
# applied and passes when it is not. The path comes from its own location: the
# tree is built in a temporary directory whose name it cannot know.
OBSERVER = """
import unittest
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "src" / "knowledgestore" / "target.py"


class Observe(unittest.TestCase):
    def test_the_target_is_unmutated(self):
        self.assertIn("ORIGINAL", TARGET.read_text(encoding="utf-8"))
"""

# Red for its own reasons, and nothing to do with the module an entry author
# would be editing: the shape that made the anonymous refusal expensive is a
# guard over the table failing while every test in that module passes.
TABLE_GUARD = """
import unittest


class GuardTheTable(unittest.TestCase):
    def test_every_entry_names_one_site(self):
        self.assertEqual(1, 2, "a guard over the table, red for its own reasons")
"""

ENTRY = gate.Mutation(
    "the target loses its value",
    "target.py",
    "ORIGINAL",
    "MUTATED",
    "a purpose-built target",
    ("test_observer",),
)


class MutationGateRefusalsTest(unittest.TestCase):
    def _tree(self, *, red: bool = False) -> Path:
        """A repository the gate can be pointed at, and a second copy of the package.

        `elsewhere/` stands for whatever the child would otherwise import: an
        installed release, another checkout, another worktree. Same package name,
        a tree nobody is mutating.
        """
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Resolved, because the check compares a path reported by a child against
        # the gate's own `SRC`, and macOS hands out temporary directories behind
        # a symlink - an unresolved root would make the two disagree here for a
        # reason that has nothing to do with the tree.
        root = Path(temporary.name).resolve()
        for holder in (root / "src", root / "elsewhere"):
            (holder / "knowledgestore").mkdir(parents=True)
            (holder / "knowledgestore" / "__init__.py").write_text("", encoding="utf-8")
        (root / "src" / "knowledgestore" / "target.py").write_text(ORIGINAL, encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_observer.py").write_text(OBSERVER, encoding="utf-8")
        if red:
            (root / "tests" / "test_table_guard.py").write_text(TABLE_GUARD, encoding="utf-8")

        self.addCleanup(setattr, gate, "ROOT", gate.ROOT)
        self.addCleanup(setattr, gate, "SRC", gate.SRC)
        self.addCleanup(setattr, gate, "RECOVERY_PATH", gate.RECOVERY_PATH)
        self.addCleanup(setattr, gate, "MUTATIONS", gate.MUTATIONS)
        gate.ROOT = root
        gate.SRC = root / "src" / "knowledgestore"
        gate.RECOVERY_PATH = root / "sidecar"
        gate.MUTATIONS = (ENTRY,)
        return root

    def _child_reads(self, directory: Path) -> None:
        """Point the child's `PYTHONPATH` at a directory, the way an operator does."""
        patched = mock.patch.dict(os.environ, {"PYTHONPATH": str(directory)})
        patched.start()
        self.addCleanup(patched.stop)
        # Inherited from the gate's own suite runner when these tests are run by
        # it, and it is what the bytecode assertion below is about - left in
        # place, that assertion would hold whether or not the probe suppressed
        # bytecode itself. `patch.dict` puts the whole environment back.
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)

    @staticmethod
    def _drive_main() -> tuple[int, str, str]:
        printed, reported = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(reported):
            code = gate.main([])
        return code, printed.getvalue(), reported.getvalue()

    def test_a_child_importing_another_tree_is_refused_and_the_path_is_named(self):
        """Catches the check being dropped, and a refusal too vague to act on.

        Without it the gate concludes from a child that never read this tree:
        every entry survives, or derives as observed by nothing, and the two
        diagnoses that follow both start at the tests. The path the child
        actually imported is what distinguishes this from a real survivor, so it
        has to be in the message alongside the one that was expected - and so do
        both ways out, because the operator holding a relative `PYTHONPATH` and
        the one holding no install need different ones.
        """
        root = self._tree()
        self._child_reads(root / "elsewhere")

        reported = io.StringIO()
        with contextlib.redirect_stderr(reported):
            code = gate.check_import_path()

        message = reported.getvalue()
        self.assertEqual(code, 1, "a child reading another tree was accepted")
        self.assertIn(str(root / "elsewhere" / "knowledgestore" / "__init__.py"), message)
        self.assertIn(str(root / "src" / "knowledgestore"), message)
        self.assertIn(f"PYTHONPATH={root / 'src'}", message)
        self.assertIn("pip install -e .", message)

    def test_a_child_importing_this_tree_is_not_refused_and_leaves_no_bytecode(self):
        """The sensitivity control: a check that refused every run would pass the test
        above and stop the gate from ever running. The absolute `PYTHONPATH` here is the
        remedy the refusal prints, so this is also the check that the remedy works.

        The bytecode half is a second break, and a quiet one. This probe imports a module
        of the tree the gate is about to mutate, so a `.pyc` written here is one a mutated
        run can read back: CPython invalidates on `(mtime seconds, size)`, which two
        mutations inside one second cannot always disturb, and the run then reports on
        code that was never in the tree (#228).
        """
        root = self._tree()
        self._child_reads(root / "src")

        reported = io.StringIO()
        with contextlib.redirect_stderr(reported):
            code = gate.check_import_path()

        self.assertEqual(code, 0, f"a child reading this tree was refused: {reported.getvalue()}")
        self.assertEqual(reported.getvalue(), "")
        self.assertEqual(
            [], list((root / "src").rglob("__pycache__")), "the probe cached bytecode of the tree"
        )

    def test_a_child_that_imports_nothing_is_refused_rather_than_read_as_reading_this_tree(self):
        """Catches the no-install case falling through as correct. Nothing is
        installed and nothing is on the path, so the child imports no package at
        all - and a check that only compared two paths would have none to compare
        and could take that either way. The refusal has to carry what the child
        said, or the reader is told their tree is wrong with no evidence.

        The one check here that does not spawn a child: a machine holding an
        install cannot produce this, and the gate's `run=` seam is what makes it
        reachable without uninstalling anything.
        """
        root = self._tree()

        def imports_nothing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="ModuleNotFoundError: No module named 'knowledgestore'\n",
            )

        reported = io.StringIO()
        with contextlib.redirect_stderr(reported):
            code = gate.check_import_path(run=imports_nothing)

        message = reported.getvalue()
        self.assertEqual(code, 1, "a child that imported nothing was read as reading this tree")
        self.assertIn("No module named 'knowledgestore'", message)
        self.assertIn(str(root / "src" / "knowledgestore"), message)
        self.assertIn("pip install -e .", message)

    def test_the_wrong_tree_is_refused_before_the_tests_are_blamed(self):
        """Catches the check running after the pre-check rather than before it.

        Order is the point rather than tidiness. A child reading an installed
        release of a branch that adds functions fails the suite, so the run that
        cannot conclude anything prints a refusal blaming tests that are fine -
        the message that cost the forty minutes. It has to be unreachable for
        this cause, and nothing but the order makes it so.
        """
        root = self._tree(red=True)
        self._child_reads(root / "elsewhere")

        code, _, reported = self._drive_main()

        self.assertEqual(code, 1)
        self.assertIn(str(root / "elsewhere" / "knowledgestore" / "__init__.py"), reported)
        self.assertNotIn("The suite is already failing", reported)
        self.assertEqual(
            (root / "src" / "knowledgestore" / "target.py").read_text(encoding="utf-8"),
            ORIGINAL,
            "a run that could not observe a mutation applied one anyway",
        )

    def test_the_refusal_names_the_test_module_that_failed(self):
        """Catches a refusal that reports a red suite and keeps the suspect to itself.

        The reader is not always looking at the failure: a guard over the mutation
        table fails the whole-suite pre-check while every test in the module the
        author is editing passes, so the gate refuses over a module that is green
        and names nothing. `_run` returns the failing modules already, so the
        answer was in hand and discarded.

        Names what failed rather than everything it ran, which is the half that
        can go vacuous: a refusal listing every module, or an empty list, reads as
        informative and says nothing.
        """
        root = self._tree(red=True)
        self._child_reads(root / "src")

        code, _, reported = self._drive_main()

        self.assertEqual(code, 1)
        self.assertIn("The suite is already failing", reported)
        self.assertIn("test_table_guard", reported)
        self.assertNotIn("test_observer", reported)

    def test_a_green_tree_reaches_the_mutations_and_neither_refusal_fires(self):
        """The sensitivity control for both refusals at once, and the only check here
        that runs the gate end to end. A check_import_path that refused everything, or
        a pre-check that read a passing suite as failing, would each pass one of the
        tests above while making the gate incapable of ever reporting on a mutation."""
        root = self._tree()
        self._child_reads(root / "src")

        code, printed, reported = self._drive_main()

        self.assertEqual(code, 0, f"a correct tree was refused: {reported}")
        self.assertIn("1 of 1 mutations caught", printed)
        self.assertEqual(reported, "")
        self.assertEqual(
            (root / "src" / "knowledgestore" / "target.py").read_text(encoding="utf-8"), ORIGINAL
        )


if __name__ == "__main__":
    unittest.main()
