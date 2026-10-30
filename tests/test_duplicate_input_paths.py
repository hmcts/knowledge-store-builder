"""A list naming one file twice counts it once, and says it dropped a repeat.

The three stages that read a file list (`extract-ast`, `json-schema`, `content-set`)
each reported counts doubled by a list that spelled one file as an absolute path
and again as a store-relative one, and nothing warned. A list generator can emit
both without either spelling being wrong.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from settings_isolation import SettingsIsolated  # noqa: E402
from test_extract_ast import _Extractor, _StoreCase  # noqa: E402
from test_extract_json_schema import StoreCase as JsonSchemaStoreCase  # noqa: E402

from knowledgestore import build_content_set, config, content_set, extract_ast  # noqa: E402
from knowledgestore import extract_json_schema as json_schema_stage  # noqa: E402

NOTICE = "1 duplicate path(s) in the input ignored"


class ExtractAstTest(_StoreCase, unittest.TestCase):
    def spellings(self, file: Path) -> list[str]:
        """The absolute spelling and the store-relative one of the same file."""
        return [str(file), str(file.relative_to(self.root))]

    def test_both_spellings_reach_the_parser_once_and_the_repeat_is_reported(self):
        """Break caught: the list handed to the extractor as written, so the file is
        parsed twice and its nodes are doubled."""
        first = self._repo_file("repo-a", "src/a.py")
        other = self._repo_file("repo-a", "src/b.py")
        listing = self.root / "files.txt"
        listing.write_text("\n".join([*self.spellings(first), str(other)]) + "\n", encoding="utf-8")
        extractor = _Extractor()
        code, out, err = self._run(extractor, ["--files", str(listing)])
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(path.name for path in extractor.calls[0]), ["a.py", "b.py"])
        self.assertEqual(len(self._layer()["nodes"]), 2)
        self.assertIn(NOTICE, out)
        self.assertIn("Extracting 2 file(s)", out)

    def test_the_absolute_spelling_is_kept_when_the_relative_one_comes_first(self):
        """Break caught: the first spelling kept whatever it is, so a relative one is
        read against the working directory and the file lands outside every repository,
        which the stage now reports as outside its scope rather than extracting."""
        only = self._repo_file("repo-a", "src/a.py")
        listing = self.root / "files.txt"
        listing.write_text("\n".join(reversed(self.spellings(only))) + "\n", encoding="utf-8")
        extractor = _Extractor()
        code, out, err = self._run(extractor, ["--files", str(listing)])
        self.assertEqual(code, 0, err)
        self.assertEqual(extractor.calls, [[only]])
        self.assertIn("repo-a", out)
        self.assertNotIn("outside scope", out)

    def test_a_list_without_repeats_says_nothing_about_duplicates(self):
        """Break caught: the notice printed unconditionally."""
        only = self._repo_file("repo-a", "src/a.py")
        listing = self.root / "files.txt"
        listing.write_text(f"{only}\n", encoding="utf-8")
        code, out, err = self._run(_Extractor(), ["--files", str(listing)])
        self.assertEqual(code, 0, err)
        self.assertNotIn("duplicate path", out)

    @unittest.skipUnless(shutil.which("graphify"), "needs graphify")
    def test_the_real_extractor_doubles_nodes_for_a_repeated_file(self):
        """Pins what the repeat would cost: graphify's own extractor, given one file
        twice, returns every node twice. Dropping the repeat keeps the file from being
        parsed twice. The layer no longer shows the doubling, because identical nodes
        merge, so the dropped-repeat notice and the per-repository count - recorded
        before the merge - are what catch a repeat that reaches the parser."""
        from graphify.extract import extract

        path = self._repo_file("repo-a", "a.py")
        path.write_text("def f():\n    return 1\n", encoding="utf-8")
        # The store-relative spelling resolves against the working directory, as it
        # does when the stage is run from the store root.
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        once = extract([path], cache_root=self.root)
        twice = extract([path, path], cache_root=self.root)
        self.assertEqual(len(twice["nodes"]), 2 * len(once["nodes"]))

        listing = self.root / "files.txt"
        listing.write_text("\n".join(self.spellings(path)) + "\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = extract_ast.main(["--files", str(listing)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("1 duplicate path(s) in the input ignored", out.getvalue())
        counts = json.loads(
            (self.root / "graphify-out" / ".graphify_ast_counts.json").read_text(encoding="utf-8")
        )
        self.assertEqual(counts, {"repo-a": len(once["nodes"])})
        self.assertEqual(len(self._layer()["nodes"]), len(once["nodes"]))
        ids = [n["id"] for n in self._layer()["nodes"]]
        self.assertEqual(len(ids), len(set(ids)))


class JsonSchemaTest(JsonSchemaStoreCase):
    def test_both_spellings_of_a_schema_count_once(self):
        """Break caught: the content set counted as written, so the report says two
        `.json` files and the file is read twice."""
        config.DETECT_PATH.write_text(
            json.dumps(
                {
                    "files": {
                        "code": [
                            str(self.widget_file),
                            str(self.widget_file.relative_to(self.root)),
                            str(self.person_file),
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = json_schema_stage.main([])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn(NOTICE, text)
        self.assertIn("json-schema: 2 .json files in the content set", text)


class ContentSetTest(SettingsIsolated):
    def test_both_spellings_are_one_path_and_the_repeat_is_counted(self):
        """Break caught: `content_paths` listing a file under both spellings, or
        deduplicating without saying so."""
        with tempfile.TemporaryDirectory() as holder:
            root = Path(holder).resolve()
            config.configure(root=str(root))
            target = root / "repositories" / "repo-a" / "x.py"
            detect = {"files": {"code": [str(target), "repositories/repo-a/x.py"]}}
            self.assertEqual(
                content_set.content_paths_counted(detect), (["repositories/repo-a/x.py"], 1)
            )
            self.assertEqual(content_set.content_paths(detect), ["repositories/repo-a/x.py"])

    def test_the_stage_reports_the_repeat(self):
        """Break caught: the stage ignoring the count `content_paths_counted` returns."""
        with tempfile.TemporaryDirectory() as holder:
            root = Path(holder).resolve()
            config.configure(root=str(root))
            clone = root / "repositories" / "repo-a"
            clone.mkdir(parents=True)
            (clone / "x.py").write_text("x = 1\n", encoding="utf-8")
            config.DETECT_PATH.parent.mkdir(parents=True, exist_ok=True)
            config.DETECT_PATH.write_text(
                json.dumps({"files": {"code": [str(clone / "x.py"), "repositories/repo-a/x.py"]}}),
                encoding="utf-8",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = build_content_set.main([])
            self.assertEqual(code, 0, out.getvalue())
            self.assertIn(NOTICE, out.getvalue())
            self.assertIn("1 content files", out.getvalue())


if __name__ == "__main__":
    unittest.main()
