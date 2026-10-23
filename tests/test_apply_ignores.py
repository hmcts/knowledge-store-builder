"""`apply-ignores` puts back what `sync` deletes, from a declaration the store commits (#492).

`sync` ends in `git clean -fd -e graphify-out`, so every clone's untracked
`.graphifyignore` is gone after each sync - `test_documented_sync_side_effects`
asserts that against real git. A store relying on one re-applied it with its own
script, and nothing failed when that step was skipped. These run the real clean
from `sync` over real clones, then the real stage, and read the files it wrote.
"""

from __future__ import annotations

import io as _io
import unittest
from contextlib import redirect_stderr, redirect_stdout

import settings_isolation  # noqa: F401  (path setup and settings isolation)
from test_extract_ast_declared import _DeclaredStoreCase  # noqa: E402

from knowledgestore import apply_ignores  # noqa: E402
from knowledgestore import sync_repositories as sync  # noqa: E402


class ApplyIgnoresTest(_DeclaredStoreCase, unittest.TestCase):
    def _apply(self) -> "tuple[int, str, str]":
        out, err = _io.StringIO(), _io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = apply_ignores.main()
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def _lines(path) -> "list[str]":
        return path.read_text(encoding="utf-8").splitlines()

    def test_after_syncs_clean_it_writes_every_declared_exclusion_and_is_idempotent(self):
        """Catches the stage not writing, or appending again on every run.

        A global exclusion reaches every clone; a per-repository one reaches only its
        repository. A second run must leave the bytes as they were, or a store that
        applies after every sync grows its ignore files without bound.
        """
        a = self._clone("repo-a", "a.py")
        b = self._clone("repo-b", "b.py")
        self._declare("repo-a", "repo-b")
        self._declare_exclusions("# every clone\nvendor/\nrepo-a | build/generated/\n")
        (a / ".graphifyignore").write_text("stale\n", encoding="utf-8")
        for clone in (a, b):
            sync.run_git(["-C", str(clone), *sync.CLEAN_ARGS])
        self.assertFalse((a / ".graphifyignore").exists(), "the fixture's clean did not run")

        code, out, err = self._apply()
        self.assertEqual(code, 0, out + err)
        a_lines = self._lines(a / ".graphifyignore")
        b_lines = self._lines(b / ".graphifyignore")
        self.assertIn("vendor/", a_lines)
        self.assertIn("build/generated/", a_lines)
        self.assertIn("vendor/", b_lines)
        self.assertNotIn("build/generated/", b_lines, "a per-repository exclusion leaked")

        before = {c: (c / ".graphifyignore").read_bytes() for c in (a, b)}
        code, _, _ = self._apply()
        self.assertEqual(code, 0)
        self.assertEqual({c: (c / ".graphifyignore").read_bytes() for c in (a, b)}, before)

    def test_a_repositorys_own_ignore_lines_are_kept(self):
        """Catches the file being overwritten: a tracked ignore file loses its own rules.

        A repository may commit its own `.graphifyignore`; `sync` restores it, and the
        declared exclusions are added to it rather than replacing it.
        """
        a = self._clone("repo-a", "a.py", ignore="docs/\n")
        self._declare("repo-a")
        self._declare_exclusions("vendor/\n")
        code, out, err = self._apply()
        self.assertEqual(code, 0, out + err)
        lines = self._lines(a / ".graphifyignore")
        self.assertEqual(lines[0], "docs/")
        self.assertIn("vendor/", lines)

    def test_an_exclusion_for_an_undeclared_repository_is_named(self):
        """Catches a mistyped repository name applying nothing, silently."""
        self._clone("repo-a", "a.py")
        self._declare("repo-a")
        self._declare_exclusions("repo-typo | vendor/\n")
        code, out, err = self._apply()
        self.assertEqual(code, 0, out + err)
        self.assertIn("repo-typo", err)


if __name__ == "__main__":
    unittest.main()
