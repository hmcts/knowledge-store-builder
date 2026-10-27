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

    Nodes carry `source_file` relative to `cache_root`, which the stage sets to the
    clone, as graphify writes it. `declined` names files to report in `skipped_sources`,
    the key graphify 0.9.94 added for files an extractor declines by design, in the
    spelling observed from it: the absolute path it was given.
    """

    def __init__(
        self, root: Path, nodes: "dict[str, int]", declined=(), unread=(), fails=(), spell=None
    ):
        self.root = root
        self.nodes = nodes
        self.declined = tuple(declined)
        # `failed_sources`, spelled relative to `cache_root` like `source_file`.
        self.unread = tuple(unread)
        # Repositories whose extract call raises, as a grammar error does.
        self.fails = tuple(fails)
        # How `source_file` is spelled; a wrong rule is the instrument mismatch.
        self.spell = spell or (lambda relative: relative)

    def __call__(self, files, cache_root=None):
        if any(f"/{name}/" in str(files[0]) for name in self.fails):
            raise RuntimeError("unparseable grammar")
        found = []
        for path in files:
            relative = Path(path).relative_to(cache_root).as_posix()
            for n in range(self.nodes.get(Path(path).name, 0)):
                found.append({"id": f"{relative}#{n}", "source_file": self.spell(relative)})
        # The absolute path the extractor was handed, verbatim: what graphify 0.9.94
        # was observed to return, whether `cache_root` is the store or the clone.
        skipped = [str(path) for path in files if Path(path).name in self.declined]
        unread = [
            Path(path).relative_to(cache_root).as_posix()
            for path in files
            if Path(path).name in self.unread
        ]
        return {"nodes": found, "edges": [], "failed_sources": unread, "skipped_sources": skipped}


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

    def test_a_run_with_no_source_file_node_at_all_fails_when_the_record_had_files(self):
        """Catches the total loss going quiet: an extractor that returns nothing.

        With no node carrying `source_file` there is nothing to join on, and the
        stage once returned early and passed - the one case the guard exists for.
        Every baseline file with nodes is vanished, so the run fails naming them and
        the baseline stands.
        """
        detect = self._store("schema.json", "main.py")
        self._run(_PerFile(self.root, {"schema.json": 3, "main.py": 2}), ["--detect", str(detect)])
        code, out, err = self._run(_PerFile(self.root, {}), ["--detect", str(detect)])
        self.assertEqual(code, 1, out + err)
        self.assertIn("schema.json", err)
        self.assertIn("main.py", err)
        self.assertIn("2 file(s) vanished, 5 node(s) lost", err)
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

    def test_allow_vanished_passes_the_file_it_names_and_records_zero(self):
        """Catches the escape not working: an intended emptying must be passable."""
        detect = self._store("schema.json", "main.py")
        self._run(_PerFile(self.root, {"schema.json": 3, "main.py": 2}), ["--detect", str(detect)])
        code, out, err = self._run(
            _PerFile(self.root, {"main.py": 2}),
            ["--detect", str(detect), "--allow-vanished", "repositories/repo-a/schema.json"],
        )
        self.assertEqual(code, 0, out + err)
        self.assertIn("schema.json", err)
        self.assertEqual(self._record()["repositories/repo-a/schema.json"], 0)

    def test_allow_vanished_does_not_pass_a_file_it_does_not_name(self):
        """Catches the escape accepting everything at once: one intended loss hid the rest.

        Two files go to zero and one is named. The named one is accepted and recorded
        as 0; the other still fails the run and keeps its previous count.
        """
        detect = self._store("schema.json", "other.json", "main.py")
        self._run(
            _PerFile(self.root, {"schema.json": 3, "other.json": 4, "main.py": 2}),
            ["--detect", str(detect)],
        )
        code, out, err = self._run(
            _PerFile(self.root, {"main.py": 2}),
            ["--detect", str(detect), "--allow-vanished", "repositories/repo-a/schema.json"],
        )
        self.assertEqual(code, 1, out + err)
        other = next(line for line in err.splitlines() if "other.json" in line)
        self.assertIn("refused", other)
        self.assertEqual(self._record()["repositories/repo-a/schema.json"], 0)
        self.assertEqual(self._record()["repositories/repo-a/other.json"], 4)

    def test_a_file_graphify_could_not_read_is_named_unread_and_still_fails(self):
        """Catches an unread file reported as VANISHED, or passed: lost is lost."""
        detect = self._store("schema.json", "main.py")
        self._run(_PerFile(self.root, {"schema.json": 3, "main.py": 2}), ["--detect", str(detect)])
        code, out, err = self._run(
            _PerFile(self.root, {"main.py": 2}, unread=("schema.json",)),
            ["--detect", str(detect)],
        )
        self.assertEqual(code, 1, out + err)
        line = next(line for line in err.splitlines() if "  unread  " in line)
        self.assertIn("repositories/repo-a/schema.json", line)

    def test_a_failed_repositorys_files_are_neither_vanished_nor_forgotten(self):
        """Catches a failed repository read as every file vanishing, or losing its baseline.

        `repo-b` raises this run. Its files did not extract, so they are not losses
        to report, and their previous counts stay in the record so the run after it
        is compared against what they last produced.
        """
        a = self._clone("repo-a", "a.py")
        b = self._clone("repo-b", "b.py")
        self._declare("repo-a", "repo-b")
        detect = self._detect([a / "a.py", b / "b.py"])
        self._run(_PerFile(self.root, {"a.py": 2, "b.py": 5}), ["--detect", str(detect)])
        code, out, err = self._run(
            _PerFile(self.root, {"a.py": 2, "b.py": 5}, fails=("repo-b",)),
            ["--detect", str(detect)],
        )
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("VANISHED", err)
        self.assertIn("0 file(s) vanished", out)
        self.assertEqual(
            self._record(), {"repositories/repo-a/a.py": 2, "repositories/repo-b/b.py": 5}
        )

    def test_a_source_file_join_that_misses_refuses_and_writes_no_record(self):
        """Catches the instrument measuring a neighbour: an all-zero record never fires.

        Every node names a `source_file` spelled outside its clone - the shape graphify
        0.9.80 and 0.9.94 were observed to write for a store under a doubly-symlinked
        path - which no rule can map back to a content-set path, so every file would
        count 0. The stage refuses and writes no record.
        """
        detect = self._store("schema.json", "main.py")
        code, out, err = self._run(
            _PerFile(
                self.root,
                {"schema.json": 3, "main.py": 2},
                spell=lambda r: f"../elsewhere/{Path(r).name}",
            ),
            ["--detect", str(detect)],
        )
        self.assertEqual(code, 1, out + err)
        self.assertIn("instrument mismatch", err)
        self.assertIn("5 of 5", err)
        self.assertFalse((self.root / "graphify-out" / ".graphify_ast_file_counts.json").exists())

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
