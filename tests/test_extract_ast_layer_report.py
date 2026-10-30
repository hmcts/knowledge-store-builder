"""`extract-ast` says what the layer is made of, and what it could not parse (#535, #531).

Three things a green run used to hide, each a real event on a consuming store:

* graphify 0.9.97 began extracting YAML as one node per key, about 269k nodes where
  0.9.93 gave none, and nothing in the library noticed: the vanished-file guard watches
  for loss, and the swamping ceiling compares against a semantic layer that may not exist.
* A node id appeared twice in the layer. graphify emits one `import Foundation` module
  node per importing Swift file, all sharing one id; `graphify update` collapses them.
* Files graphify could not parse were reported as "could not be read", which sent
  operators looking for an I/O fault in what were Helm templates.

Clones are real; the parser is stubbed at its boundary, the per-repository extract call.
`ExtractAstDuplicateIdParityTest` in `test_extract_ast_parity` runs the real parser for the
duplicate-id case.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import settings_isolation  # noqa: F401  (path setup and settings isolation)
from test_extract_ast_declared import _DeclaredStoreCase  # noqa: E402

from knowledgestore import extract_ast  # noqa: E402


class _Emits:
    """graphify's extract call: for each file named in `plan`, emit the nodes listed.

    A listed node is a dict; `source_file` is added relative to `cache_root`, as graphify
    writes it, unless the node carries its own. `unread` names files to return in
    `failed_sources`, spelled relative to `cache_root`.
    """

    def __init__(self, plan: "dict[str, list[dict]]", unread=(), fails=()):
        self.plan = plan
        self.unread = tuple(unread)
        self.fails = tuple(fails)

    def __call__(self, files, cache_root=None):
        if any(f"/{name}/" in str(files[0]) for name in self.fails):
            raise RuntimeError("unparseable grammar")
        found = []
        for path in files:
            relative = Path(path).relative_to(cache_root).as_posix()
            for node in self.plan.get(Path(path).name, []):
                found.append({"source_file": relative, **node})
        unread = [
            Path(path).relative_to(cache_root).as_posix()
            for path in files
            if Path(path).name in self.unread
        ]
        return {"nodes": found, "edges": [], "failed_sources": unread, "skipped_sources": []}


def _keys(prefix: str, count: int) -> "list[dict]":
    return [{"id": f"{prefix}_{n}", "label": f"key{n}"} for n in range(count)]


class _LayerCase(_DeclaredStoreCase):
    def _store(self, *names: str, repo: str = "repo-a") -> Path:
        clone = self._clone(repo, *names)
        self._declare(repo)
        return self._detect([clone / name for name in names])

    def _extension_record(self) -> dict:
        path = self.root / "graphify-out" / ".graphify_ast_extension_counts.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _file_record(self) -> dict:
        path = self.root / "graphify-out" / ".graphify_ast_file_counts.json"
        return json.loads(path.read_text(encoding="utf-8"))


class ExtensionGrowthTest(_LayerCase, unittest.TestCase):
    def test_a_new_extension_taking_a_large_share_is_named_with_its_counts(self):
        """Catches the 0.9.97 event going unnoticed: YAML from nothing to most of the layer.

        Hand-derived: 10 `.py` nodes before; 10 `.py` and 30 `.yaml` after, so `.yaml`
        is 30 of 40 nodes, 75% of the layer, up from 0.
        """
        detect = self._store("main.py", "values.yaml")
        self._run(_Emits({"main.py": _keys("py", 10)}), ["--detect", str(detect)])
        code, out, err = self._run(
            _Emits({"main.py": _keys("py", 10), "values.yaml": _keys("yaml", 30)}),
            ["--detect", str(detect)],
        )
        self.assertEqual(code, 0, "growth is a report and never a refusal: " + out + err)
        self.assertIn(".yaml: 0 -> 30 nodes (75% of the layer)", err)
        self.assertNotIn(".py: ", err, "an extension that did not grow was named")
        self.assertIn("graphify-ignore.txt", err, "the warning did not say what to do")

    def test_ordinary_growth_does_not_warn(self):
        """Catches a threshold of zero, which fires on every run and is then ignored.

        `.py` goes from 40 of 50 nodes (80%) to 50 of 60 (83%): growth, but 3 points.
        """
        detect = self._store("main.py", "values.yaml")
        self._run(
            _Emits({"main.py": _keys("py", 40), "values.yaml": _keys("yaml", 10)}),
            ["--detect", str(detect)],
        )
        code, out, err = self._run(
            _Emits({"main.py": _keys("py", 50), "values.yaml": _keys("yaml", 10)}),
            ["--detect", str(detect)],
        )
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("warning", err)
        self.assertIn(".py 50", out, "the per-extension counts were not printed")

    def test_the_first_run_records_a_baseline_and_does_not_warn(self):
        """Catches a missing baseline being read as growth from nothing everywhere."""
        detect = self._store("main.py", "values.yaml")
        code, out, err = self._run(
            _Emits({"main.py": _keys("py", 3), "values.yaml": _keys("yaml", 9)}),
            ["--detect", str(detect)],
        )
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("grew", err)
        self.assertIn("no per-extension record", out)
        self.assertEqual(self._extension_record(), {".py": 3, ".yaml": 9})

    def test_a_run_with_a_failed_repository_leaves_the_baseline_alone(self):
        """Catches a partial layer becoming the baseline.

        The failed repository's nodes are absent from the layer, so recording it would make
        the next complete run read the missing repository as growth in every extension.
        """
        clone_a = self._clone("repo-a", "main.py")
        clone_b = self._clone("repo-b", "app.js")
        self._declare("repo-a", "repo-b")
        detect = self._detect([clone_a / "main.py", clone_b / "app.js"])
        plan = {"main.py": _keys("py", 4), "app.js": _keys("js", 6)}
        self._run(_Emits(plan), ["--detect", str(detect)])
        self.assertEqual(self._extension_record(), {".js": 6, ".py": 4})
        code, out, err = self._run(_Emits(plan, fails=("repo-b",)), ["--detect", str(detect)])
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self._extension_record(), {".js": 6, ".py": 4})

    def test_the_record_is_sorted_and_lower_cases_extensions(self):
        """Catches an unstable record: two runs on one input must write the same bytes."""
        detect = self._store("b.PY", "a.yaml", "Makefile")
        self._run(
            _Emits({"b.PY": _keys("p", 2), "a.yaml": _keys("y", 1), "Makefile": _keys("m", 3)}),
            ["--detect", str(detect)],
        )
        record = self._extension_record()
        self.assertEqual(record, {"(none)": 3, ".py": 2, ".yaml": 1})
        self.assertEqual(list(record), sorted(record))


class DuplicateNodeIdsTest(_LayerCase, unittest.TestCase):
    def _anchors(self):
        """Two files each emitting a module node `foundation`, as Swift imports do."""
        anchor = {"id": "foundation", "label": "Foundation", "type": "module"}
        return {
            "one.swift": [{**anchor, "source_location": "L1"}, {"id": "a", "label": "A"}],
            "two.swift": [{**anchor, "source_location": "L3"}],
        }

    def test_the_same_node_emitted_twice_is_kept_once_with_the_last_copy(self):
        """Catches duplicate ids reaching the layer when `graphify update` would not give them.

        Both copies are one node seen in two files, so they differ only in `source_file` and
        `source_location`. `update` keeps the last written; so must this.
        """
        detect = self._store("one.swift", "two.swift")
        code, out, err = self._run(_Emits(self._anchors()), ["--detect", str(detect)])
        self.assertEqual(code, 0, out + err)
        nodes = self._layer()["nodes"]
        self.assertEqual(
            sorted(n["id"] for n in nodes), ["repo-a::a", "repo-a::foundation"], "ids repeat"
        )
        kept = next(n for n in nodes if n["id"] == "repo-a::foundation")
        self.assertEqual(kept["source_file"], "two.swift")
        self.assertIn("1 duplicate node id(s) merged", out)
        self.assertNotIn("shared by distinct nodes", err)

    def test_merging_does_not_send_a_file_whose_only_node_is_shared_to_zero(self):
        """Catches the per-file record being counted after the merge.

        `one.swift` emits only the shared `foundation` node and `two.swift` that node
        and `b`. The merge keeps the last copy, `two.swift`'s, so counted afterwards
        `one.swift` is 0 and the second run fails it as vanished; counted before, it is 1.
        """
        anchor = {"id": "foundation", "label": "Foundation", "type": "module"}
        plan = {"one.swift": [anchor], "two.swift": [anchor, {"id": "b", "label": "B"}]}
        detect = self._store("one.swift", "two.swift")
        self._run(_Emits(plan), ["--detect", str(detect)])
        self.assertEqual(
            self._file_record(),
            {"repositories/repo-a/one.swift": 1, "repositories/repo-a/two.swift": 2},
        )
        code, out, err = self._run(_Emits(plan), ["--detect", str(detect)])
        self.assertEqual(code, 0, "a file that extracted fine was refused: " + out + err)

    def test_distinct_nodes_sharing_an_id_are_all_kept_and_named(self):
        """Catches a silent drop: two different nodes colliding on one id.

        They differ in `label`, which is not provenance, so they are not one node twice.
        Deduplicating would lose one with no word said.
        """
        plan = {
            "one.swift": [{"id": "clash", "label": "Alpha"}],
            "two.swift": [{"id": "clash", "label": "Beta"}],
        }
        detect = self._store("one.swift", "two.swift")
        code, out, err = self._run(_Emits(plan), ["--detect", str(detect)])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sorted(n["label"] for n in self._layer()["nodes"]), ["Alpha", "Beta"])
        self.assertIn("1 node id(s) are shared by distinct nodes", err)
        self.assertIn("repo-a::clash", err)
        self.assertNotIn("merged", out)

    def test_the_merge_is_deterministic(self):
        """Catches an unordered merge: two runs on one input must write identical layers."""
        detect = self._store("one.swift", "two.swift")
        self._run(_Emits(self._anchors()), ["--detect", str(detect)])
        first = (self.root / "graphify-out" / ".graphify_ast.json").read_bytes()
        self._run(_Emits(self._anchors()), ["--detect", str(detect)])
        self.assertEqual(first, (self.root / "graphify-out" / ".graphify_ast.json").read_bytes())


class ParseFailureWordingTest(_LayerCase, unittest.TestCase):
    def test_files_the_parser_rejected_are_called_unparsed_with_an_extension_breakdown(self):
        """Catches the old wording: a Helm template is not an I/O fault.

        Three files exist and are readable, so a failure to extract them is the parser's.
        """
        names = ("a.yaml", "b.yaml", "c.tpl", "ok.py")
        detect = self._store(*names)
        code, out, err = self._run(
            _Emits({"ok.py": _keys("py", 1)}, unread=("a.yaml", "b.yaml", "c.tpl")),
            ["--detect", str(detect)],
        )
        self.assertEqual(code, 0, out + err)
        self.assertIn("3 of 4 file(s) were handed to the extractor and could not be parsed", out)
        self.assertIn("(2 .yaml, 1 .tpl)", out)
        self.assertNotIn("could not be read", out)

    def test_a_file_the_system_cannot_open_is_still_called_unread(self):
        """Catches the two causes being merged into one word: a real I/O error stays 'read'.

        `gone.yaml` is in the content set and was deleted before extraction ran, so opening
        it fails with an OS error, unlike `bad.yaml`, which opens and is the parser's.
        """
        detect = self._store("bad.yaml", "gone.yaml")
        (self.root / "repositories" / "repo-a" / "gone.yaml").unlink()
        code, out, err = self._run(
            _Emits({}, unread=("bad.yaml", "gone.yaml")), ["--detect", str(detect)]
        )
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out.count("could not be parsed"), 1)
        self.assertEqual(out.count("could not be read"), 1)
        # Each file sits under its own heading: bad.yaml after "parsed" and before "read".
        self.assertLess(out.index("could not be parsed"), out.index("bad.yaml"))
        self.assertLess(out.index("bad.yaml"), out.index("could not be read"))
        self.assertLess(out.index("could not be read"), out.index("gone.yaml"))

    def test_the_breakdown_orders_ties_by_extension(self):
        """Catches an unordered tally: the same input must print the same line twice."""
        self.assertEqual(
            extract_ast.extension_breakdown(["b.yml", "a.tpl", "c.yaml", "d.YAML", "e"]),
            "2 .yaml, 1 (none), 1 .tpl, 1 .yml",
        )


if __name__ == "__main__":
    unittest.main()
