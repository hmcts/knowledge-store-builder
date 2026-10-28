"""`extract-ast` extracts the declared repositories, and nothing else (#525).

The detect command scans the store root, so the content set also holds fetch-only clones
under `external/`, the store's own `.claude/`, `tests/` and `scripts/`, and its generated
`knowledge/` layers. Extracted, they became nodes with no repository, while `json-schema`
skipped the same files as `outside-repositories`. The per-repository route hands
`merge-graphs` the declared repositories only, so that is the scope here; anything else is
reported as outside it and never extracted.

The parser is stubbed at its true boundary - the per-repository extract call - exactly as
`test_extract_ast` does; every clone is a real git repository.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import settings_isolation  # noqa: F401  (path setup and settings isolation)
from test_extract_ast_declared import _DeclaredStoreCase  # noqa: E402
from test_extract_ast import _Extractor  # noqa: E402


class ScopeTest(_DeclaredStoreCase, unittest.TestCase):
    """Which files reach the extractor, and what the run says about the rest."""

    def _files(self) -> "list[Path]":
        found = []
        for name in ("repo-a", "repo-b"):
            self._clone(name, "src/one.py")
            found.append(self.root / "repositories" / name / "src" / "one.py")
        self._declare("repo-a", "repo-b")
        for stray in ("knowledge/notes.py", "external/vendor/lib.py", "scripts/run.py"):
            path = self.root / stray
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x = 1\n", encoding="utf-8")
            found.append(path)
        return found

    def test_only_declared_repositories_reach_the_extractor(self):
        detect = self._detect(self._files())
        extractor = _Extractor()
        code, out, err = self._run(extractor, ["--detect", str(detect)])
        self.assertEqual(code, 0, out + err)

        # Break caught: the content set is extracted whole again.
        seen = sorted(Path(p).relative_to(self.root).as_posix() for p in extractor.files_seen)
        self.assertEqual(
            seen,
            ["repositories/repo-a/src/one.py", "repositories/repo-b/src/one.py"],
        )
        self.assertIn("3 file(s) outside scope, skipped", out)
        # Break caught: a file is dropped without being named, so an operator cannot tell
        # one left out of scope from one that failed to extract. `knowledge/` holds the
        # store's own generated layers, so extracting it would feed output back as input.
        for stray in ("external/vendor/lib.py", "scripts/run.py", "knowledge/notes.py"):
            self.assertIn(f"  {stray}", out)

        # Break caught: a stray keeps arriving as a node with no repository, which is what
        # broke the file-to-ticket join for it.
        layer = self._layer()
        self.assertEqual({n["repo"] for n in layer["nodes"]}, {"repo-a", "repo-b"})

    def test_an_undeclared_clone_is_outside_scope_when_the_store_declares_repositories(self):
        files = self._files()
        files.append(self._repo_file("repo-extra", "src/two.py"))
        detect = self._detect(files)
        extractor = _Extractor()
        code, out, _err = self._run(extractor, ["--detect", str(detect)])

        # Break caught: a clone `config/repositories.txt` does not name is extracted, so the
        # layer holds a repository the store never declared.
        self.assertEqual(code, 0)
        self.assertNotIn("repo-extra", " ".join(extractor.files_seen))
        self.assertIn("4 file(s) outside scope, skipped", out)
        self.assertIn("repositories/repo-extra/src/two.py", out)

    def test_a_store_declaring_nothing_still_leaves_strays_out_and_keeps_its_clones(self):
        self._clone("repo-a", "src/one.py")
        stray = self.root / "scripts" / "run.py"
        stray.parent.mkdir(parents=True)
        stray.write_text("x = 1\n", encoding="utf-8")
        files = [self.root / "repositories" / "repo-a" / "src" / "one.py", stray]
        extractor = _Extractor()
        code, out, _err = self._run(extractor, ["--detect", str(self._detect(files))])

        # Break caught: with no declaration to filter by, the cut between a clone and a
        # stray is lost, and the stray is extracted as a node with no repository.
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(Path(p).relative_to(self.root).as_posix() for p in extractor.files_seen),
            ["repositories/repo-a/src/one.py"],
        )
        self.assertIn("1 file(s) outside scope, skipped", out)

    def test_nothing_in_scope_is_refused(self):
        self._clone("repo-a", "src/one.py")
        self._declare("repo-a")
        stray = self.root / "scripts" / "run.py"
        stray.parent.mkdir(parents=True)
        stray.write_text("x = 1\n", encoding="utf-8")
        extractor = _Extractor()
        code, _out, err = self._run(extractor, ["--detect", str(self._detect([stray]))])

        # Break caught: a content set holding only strays writes an empty layer that reads
        # as a successful run.
        self.assertEqual(code, 1)
        self.assertIn("outside scope", err)
        self.assertEqual(extractor.calls, [])


if __name__ == "__main__":
    unittest.main()
