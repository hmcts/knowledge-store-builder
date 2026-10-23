"""`extract-ast` against what the store declares, and against an interrupted run (#492).

Three gaps a store's own per-repository driver covered and this stage did not:

- **A declared repository with no clone was invisible.** The stage groups the
  content set it is handed and compared only with its own last run, so on a first
  run a repository `sync` never cloned simply was not there - no error, no name,
  and a layer that looked complete.
- **The counts were written after the loop.** An interrupted run lost every
  count it had measured, including the one that showed a repository going
  backwards.
- **Nothing failed when an exclusion was missing.** `sync` ends in
  `git clean -fd -e graphify-out`, which deletes every clone's `.graphifyignore`,
  so an exclusion a store relies on is one forgotten re-apply away from gone.

Every clone here is a real git repository, and the parser is stubbed at its true
boundary - the per-repository extract call - exactly as `test_extract_ast` does.
"""

from __future__ import annotations

import io as _io
import json
import subprocess
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import settings_isolation  # noqa: F401  (path setup and settings isolation)
from test_extract_ast import _Extractor, _StoreCase  # noqa: E402

from knowledgestore import apply_ignores, extract_ast  # noqa: E402


class _Interrupting(_Extractor):
    """The parser, interrupted while it works on one repository - a Ctrl-C mid-run."""

    def __init__(self, interrupt_at: str, **kwargs):
        super().__init__(**kwargs)
        self.interrupt_at = interrupt_at

    def _per_file(self, path):
        if self.interrupt_at in str(path):
            raise KeyboardInterrupt
        super()._per_file(path)


class _DeclaredStoreCase(_StoreCase):
    def _clone(self, name: str, *files: str, ignore: "str | None" = None) -> Path:
        """A real git clone under repositories/, holding `files`, each committed."""
        clone = self.root / "repositories" / name
        clone.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        for relative in files:
            path = clone / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x = 1\n", encoding="utf-8")
        if ignore is not None:
            (clone / ".graphifyignore").write_text(ignore, encoding="utf-8")
        subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(clone),
                "-c",
                "user.email=t@example.invalid",
                "-c",
                "user.name=t",
                "commit",
                "-qm",
                "init",
            ],
            check=True,
        )
        return clone

    def _declare(self, *names: str) -> None:
        path = self.root / "config" / "repositories.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{name} | https://example.invalid/{name}.git | main" for name in names]
        path.write_text("# declared\n" + "\n".join(lines) + "\n", encoding="utf-8")

    def _declare_exclusions(self, text: str) -> None:
        path = self.root / "config" / "graphify-ignore.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _sidecar(self) -> Path:
        return self.root / "graphify-out" / ".graphify_ast_counts.json"

    def _counts(self) -> dict:
        return json.loads(self._sidecar().read_text(encoding="utf-8"))

    def _run_capturing(self, extractor, argv):
        """Run main() with the parser stubbed, keeping the output when it raises.

        `_run` loses stdout when main() raises, and an interrupted run is exactly the
        case where what was printed before the interrupt is the evidence.
        """
        module = types.ModuleType("graphify")
        submodule = types.ModuleType("graphify.extract")
        submodule.extract = extractor
        module.extract = submodule
        saved = {k: sys.modules.get(k) for k in ("graphify", "graphify.extract")}
        sys.modules["graphify"] = module
        sys.modules["graphify.extract"] = submodule
        out, err = _io.StringIO(), _io.StringIO()
        raised: "BaseException | None" = None
        code = None
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = extract_ast.main(argv)
        except KeyboardInterrupt as interrupt:
            raised = interrupt
        finally:
            for name, previous in saved.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous
        return code, raised, out.getvalue(), err.getvalue()


class DeclaredRepositoriesTest(_DeclaredStoreCase, unittest.TestCase):
    def test_a_declared_repository_with_no_clone_fails_on_a_first_run_and_writes_nothing(self):
        """Catches the reconciliation being dropped: a missing clone extracts as a smaller estate.

        A first run, so there is no sidecar for the movement report to compare with -
        the only comparison this stage made before. Without the declaration check the
        run succeeds over the one clone it has, and the store commits a layer missing
        a whole repository with nothing naming it.
        """
        a = self._clone("repo-a", "a.py")
        self._declare("repo-a", "repo-gone", "repo-also-gone")
        detect = self._detect([a / "a.py"])
        extractor = _Extractor()
        code, out, err = self._run(extractor, ["--detect", str(detect)])
        self.assertEqual(code, 1, out + err)
        self.assertIn("repo-gone", err)
        self.assertIn("repo-also-gone", err)
        self.assertNotIn("repo-a:", err, "a cloned repository was named as missing")
        self.assertEqual(extractor.calls, [], "the parser ran despite a missing clone")
        self.assertFalse((self.root / "graphify-out" / ".graphify_ast.json").exists())
        self.assertFalse(self._sidecar().exists(), "a refused run wrote the counts sidecar")

    def test_a_declared_repository_with_nothing_parseable_is_warned_about_and_does_not_fail(self):
        """Catches the warning going silent: a clone holding no code drops out of the layer unnamed.

        A repository of documents alone is legitimate, so this reports rather than
        refuses - but the repository is absent from the layer, and before this it was
        absent from every line the stage printed too.
        """
        a = self._clone("repo-a", "a.py")
        self._clone("repo-docs", "README.md")
        self._declare("repo-a", "repo-docs")
        detect = self._detect([a / "a.py"])
        code, out, err = self._run(_Extractor(), ["--detect", str(detect)])
        self.assertEqual(code, 0, out + err)
        warnings = [line for line in err.splitlines() if "no parseable code" in line]
        self.assertEqual(len(warnings), 1, err)
        self.assertIn("repo-docs", warnings[0])
        self.assertEqual(len(self._layer()["nodes"]), 1)


class CountsAsYouGoTest(_DeclaredStoreCase, unittest.TestCase):
    def test_an_interrupted_first_run_keeps_the_counts_of_the_repositories_it_finished(self):
        """Catches the sidecar being written only after the loop: a Ctrl-C loses every count.

        Interrupted while extracting the second repository, on a first run. The first
        repository's count is the only record that it was measured, and writing the
        sidecar after the loop is what threw it away.
        """
        a = self._clone("repo-a", "a.py", "b.py")
        b = self._clone("repo-b", "c.py")
        self._declare("repo-a", "repo-b")
        detect = self._detect([a / "a.py", a / "b.py", b / "c.py"])
        code, raised, out, err = self._run_capturing(
            _Interrupting("repo-b", nodes_per_file=2), ["--detect", str(detect)]
        )
        self.assertIsNone(code, out + err)
        self.assertIsInstance(raised, KeyboardInterrupt)
        self.assertEqual(self._counts(), {"repo-a": 4})

    def test_an_interrupted_run_keeps_the_baseline_of_the_repositories_it_never_reached(self):
        """Catches a checkpoint that drops the unreached: they read as absent, then as new.

        A checkpoint holding only what this run measured would erase the previous
        count for every repository after the interrupt - so the next run reports them
        "new" and cannot show a decrease against the count it no longer has.

        And a completed repository that went backwards says so on its own line, as it
        finishes: the checkpoint replaces its baseline, so an interrupt before the
        end-of-run report would otherwise leave the decrease recorded nowhere.
        """
        a = self._clone("repo-a", "a.py")
        b = self._clone("repo-b", "b.py")
        self._declare("repo-a", "repo-b")
        detect = self._detect([a / "a.py", b / "b.py"])
        code, _, err = self._run(_Extractor(nodes_per_file=3), ["--detect", str(detect)])
        self.assertEqual(code, 0, err)
        self.assertEqual(self._counts(), {"repo-a": 3, "repo-b": 3})

        _, raised, out, _ = self._run_capturing(
            _Interrupting("repo-b", nodes_per_file=1), ["--detect", str(detect)]
        )
        self.assertIsInstance(raised, KeyboardInterrupt)
        self.assertEqual(self._counts(), {"repo-a": 1, "repo-b": 3})
        line = next(line for line in out.splitlines() if "repo-a" in line)
        self.assertIn("down from 3", line)


class DeclaredExclusionsTest(_DeclaredStoreCase, unittest.TestCase):
    def test_a_declared_exclusion_absent_from_a_clone_refuses_and_names_it(self):
        """Catches the exclusion check being dropped: sync deleted the ignore file and nothing failed.

        `repo-a` lost its `.graphifyignore` to the clean that ends every sync;
        `repo-b` kept a different one that still lacks the declared line. Both are
        named with the missing exclusion, and nothing is extracted.
        """
        a = self._clone("repo-a", "a.py")
        b = self._clone("repo-b", "b.py", ignore="docs/\n")
        self._declare("repo-a", "repo-b")
        self._declare_exclusions("vendor/\n")
        detect = self._detect([a / "a.py", b / "b.py"])
        extractor = _Extractor()
        code, out, err = self._run(extractor, ["--detect", str(detect)])
        self.assertEqual(code, 1, out + err)
        self.assertIn("repo-a: vendor/", err)
        self.assertIn("repo-b: vendor/", err)
        self.assertIn("knowledgestore apply-ignores", err)
        self.assertEqual(extractor.calls, [])
        self.assertFalse((self.root / "graphify-out" / ".graphify_ast.json").exists())

    def test_once_applied_the_same_store_extracts(self):
        """The guard on the refusal above: one that fired always would pass it too."""
        a = self._clone("repo-a", "a.py")
        b = self._clone("repo-b", "b.py", ignore="docs/\n")
        self._declare("repo-a", "repo-b")
        self._declare_exclusions("vendor/\nrepo-b | build/\n")
        detect = self._detect([a / "a.py", b / "b.py"])
        with redirect_stdout(_io.StringIO()):
            self.assertEqual(apply_ignores.main(), 0)
        code, out, err = self._run(_Extractor(), ["--detect", str(detect)])
        self.assertEqual(code, 0, out + err)

    def test_a_store_that_declares_nothing_is_unchanged(self):
        """Catches the feature leaking into stores that never opted in.

        No `config/graphify-ignore.txt`: extraction runs over clones with no ignore
        file, and `apply-ignores` writes nothing into any clone.
        """
        a = self._clone("repo-a", "a.py")
        self._declare("repo-a")
        detect = self._detect([a / "a.py"])
        out = _io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(apply_ignores.main(), 0)
        self.assertFalse((a / ".graphifyignore").exists(), "a clone was written to")
        self.assertIn("nothing declared", out.getvalue())
        code, _, err = self._run(_Extractor(), ["--detect", str(detect)])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self._layer()["nodes"]), 1)


if __name__ == "__main__":
    unittest.main()
