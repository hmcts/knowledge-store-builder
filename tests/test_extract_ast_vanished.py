"""A file that produced nodes last run and none now fails `extract-ast` (#492).

A consuming store found graphify extracting zero nodes from JSON Schema files that
use `definitions` or `$defs`, with `failed_sources` empty. The repository-level
decrease report saw a smaller number and reported it, as it is designed to; a
decrease is not a refusal, because deleted code is legitimate. A file going from
some nodes to none, while still in the content set, is a different event: the
content is there and the extractor stopped reading it.

So each file's node count is recorded beside the layer, and the next run fails on
a file that went to zero - unless `--allow-vanished` says the change is intended.
Clones are real; the parser is stubbed at its boundary, the per-repository extract
call, returning nodes carrying `source_file` the way graphify's do.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import settings_isolation  # noqa: F401  (path setup and settings isolation)
from test_extract_ast_declared import _DeclaredStoreCase  # noqa: E402


class _PerFile:
    """graphify's extract call: `nodes[name]` nodes for each file whose name is listed.

    Nodes carry `source_file` store-relative, as graphify writes it with the store
    root as `cache_root`. `declined` names files to report in `skipped_sources`,
    the key graphify 0.9.94 added for files an extractor declines by design.
    """

    def __init__(self, root: Path, nodes: "dict[str, int]", declined=()):
        self.root = root
        self.nodes = nodes
        self.declined = tuple(declined)

    def __call__(self, files, cache_root=None):
        found = []
        for path in files:
            relative = Path(path).relative_to(self.root).as_posix()
            for n in range(self.nodes.get(Path(path).name, 0)):
                found.append({"id": f"{relative}#{n}", "source_file": relative})
        skipped = [
            Path(path).relative_to(self.root).as_posix()
            for path in files
            if Path(path).name in self.declined
        ]
        return {"nodes": found, "edges": [], "failed_sources": [], "skipped_sources": skipped}


class VanishedFilesTest(_DeclaredStoreCase, unittest.TestCase):
    def _store(self, *names: str) -> Path:
        clone = self._clone("repo-a", *names)
        self._declare("repo-a")
        return self._detect([clone / name for name in names])

    def _record(self) -> dict:
        path = self.root / "graphify-out" / ".graphify_ast_file_counts.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_first_run_says_there_is_no_record_and_writes_one(self):
        """Catches the record not being written, which makes every later check vacuous."""
        detect = self._store("schema.json", "main.py")
        code, out, err = self._run(
            _PerFile(self.root, {"schema.json": 3, "main.py": 2}), ["--detect", str(detect)]
        )
        self.assertEqual(code, 0, out + err)
        self.assertIn("no per-file record", out)
        self.assertEqual(
            self._record(),
            {"repositories/repo-a/main.py": 2, "repositories/repo-a/schema.json": 3},
        )
        self.assertEqual(
            list(self._record()), sorted(self._record()), "the record is not sorted by path"
        )

    def test_a_file_that_went_to_zero_fails_the_stage_naming_it(self):
        """Catches the guard going quiet: the JSON Schema files that extracted to nothing.

        `failed_sources` is empty in that case, so nothing else this stage prints
        names the file. The run fails with its path and its previous count, and the
        summary says how much was lost.
        """
        detect = self._store("schema.json", "main.py")
        self._run(_PerFile(self.root, {"schema.json": 3, "main.py": 2}), ["--detect", str(detect)])
        code, out, err = self._run(_PerFile(self.root, {"main.py": 2}), ["--detect", str(detect)])
        self.assertEqual(code, 1, out + err)
        line = next(line for line in err.splitlines() if "schema.json" in line)
        self.assertIn("VANISHED", line)
        self.assertIn("3", line)
        self.assertIn("1 file(s) vanished, 3 node(s) lost", err)
        self.assertNotIn("main.py", err)
        # The baseline stands, so the next run still fails until someone decides.
        self.assertEqual(self._record()["repositories/repo-a/schema.json"], 3)

    def test_a_file_graphify_declined_is_named_as_declined_and_still_fails(self):
        """Catches a declined file passing: declined by design is still lost data."""
        detect = self._store("schema.json", "main.py")
        self._run(_PerFile(self.root, {"schema.json": 3, "main.py": 2}), ["--detect", str(detect)])
        code, out, err = self._run(
            _PerFile(self.root, {"main.py": 2}, declined=("schema.json",)),
            ["--detect", str(detect)],
        )
        self.assertEqual(code, 1, out + err)
        line = next(line for line in err.splitlines() if "schema.json" in line)
        self.assertIn("declined by graphify", line)
        self.assertNotIn("VANISHED", line)

    def test_allow_vanished_passes_and_accepts_the_new_count(self):
        """Catches the escape not working: an intended emptying must be passable."""
        detect = self._store("schema.json", "main.py")
        self._run(_PerFile(self.root, {"schema.json": 3, "main.py": 2}), ["--detect", str(detect)])
        code, out, err = self._run(
            _PerFile(self.root, {"main.py": 2}), ["--detect", str(detect), "--allow-vanished"]
        )
        self.assertEqual(code, 0, out + err)
        self.assertIn("schema.json", err)
        self.assertEqual(self._record()["repositories/repo-a/schema.json"], 0)

    def test_a_file_removed_from_the_content_set_does_not_count(self):
        """Catches the guard reading absence as loss: a removed file is not a vanished one."""
        detect = self._store("schema.json", "main.py")
        self._run(_PerFile(self.root, {"schema.json": 3, "main.py": 2}), ["--detect", str(detect)])
        detect = self._detect([self.root / "repositories" / "repo-a" / "main.py"])
        code, out, err = self._run(_PerFile(self.root, {"main.py": 2}), ["--detect", str(detect)])
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("schema.json", err)
        self.assertNotIn("repositories/repo-a/schema.json", self._record())

    def test_a_file_that_shrank_but_kept_nodes_does_not_fail(self):
        """Catches the guard firing on any decrease: that is the repository report's job."""
        detect = self._store("schema.json", "main.py")
        self._run(_PerFile(self.root, {"schema.json": 3, "main.py": 2}), ["--detect", str(detect)])
        code, out, err = self._run(
            _PerFile(self.root, {"schema.json": 1, "main.py": 2}), ["--detect", str(detect)]
        )
        self.assertEqual(code, 0, out + err)
        self.assertIn("0 file(s) vanished", out)


if __name__ == "__main__":
    unittest.main()
