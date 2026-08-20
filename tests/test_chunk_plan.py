"""The chunk plan: relative at rest, absolute in flight, and reproducible.

The plan is the only map from chunk number to file list, so a committed chunk
archive whose plan is wrong is evidence of nothing. Two properties carry that:
the file commits no machine-specific path, and a dispatcher still receives the
absolute paths the extraction spec requires it to echo.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import build_chunk_plan, config, store_paths


class PlanningTest(unittest.TestCase):
    def test_a_directory_is_kept_together(self):
        """Cross-file relationships are what the semantic layer exists to find, and an
        agent cannot relate two files it never saw together.

        The sizes here are chosen so a FLAT split would demonstrably fail: three files
        per directory against a chunk size of four puts the boundary one file into the
        second directory. An earlier version of this test used three files total with
        the same chunk size, so everything landed in one chunk and the assertion held
        whether or not grouping happened - it passed against a mutation that removed
        the grouping entirely.
        """
        detect = {
            "files": {
                "document": [
                    "/s/repositories/a/1.md",
                    "/s/repositories/a/2.md",
                    "/s/repositories/a/3.md",
                    "/s/repositories/b/1.md",
                    "/s/repositories/b/2.md",
                    "/s/repositories/b/3.md",
                ]
            }
        }
        plan = build_chunk_plan.plan_chunks(detect, chunk_size=4)
        chunks = list(plan.values())
        self.assertGreater(len(chunks), 1, "one chunk would make this vacuous")
        for directory in ("/s/repositories/a/", "/s/repositories/b/"):
            holding = [c for c in chunks if any(f.startswith(directory) for f in c)]
            self.assertEqual(len(holding), 1, f"{directory} was split across {len(holding)} chunks")
        # And no chunk mixes the two, which is the property grouping buys.
        for chunk in chunks:
            directories = {f.rsplit("/", 1)[0] for f in chunk}
            self.assertEqual(len(directories), 1, f"chunk mixes directories: {sorted(directories)}")

    def test_no_chunk_exceeds_the_requested_size(self):
        """An over-long FILE_LIST is what pushes an agent into the output limit that
        destroys its whole batch, so a large directory is split rather than kept whole."""
        detect = {"files": {"document": [f"/s/repositories/big/{i:03d}.md" for i in range(50)]}}
        plan = build_chunk_plan.plan_chunks(detect, chunk_size=7)
        self.assertTrue(plan)
        for name, files in plan.items():
            self.assertLessEqual(len(files), 7, f"chunk {name} holds {len(files)}")

    def test_every_file_appears_exactly_once(self):
        detect = {
            "files": {
                "document": [f"/s/repositories/d{i % 3}/{i}.md" for i in range(30)],
                "image": ["/s/repositories/d0/diagram.png"],
            }
        }
        plan = build_chunk_plan.plan_chunks(detect, chunk_size=6)
        planned = [f for files in plan.values() for f in files]
        self.assertEqual(sorted(planned), sorted(set(planned)), "a file was planned twice")
        self.assertEqual(len(planned), 31)

    def test_each_image_gets_its_own_chunk(self):
        """Vision needs its own context; mixing images with documents makes an agent do
        two jobs in one prompt."""
        detect = {
            "files": {
                "document": ["/s/repositories/a/one.md"],
                "image": ["/s/repositories/a/x.png", "/s/repositories/a/y.png"],
            }
        }
        plan = build_chunk_plan.plan_chunks(detect, chunk_size=10)
        image_chunks = [c for c in plan.values() if any(f.endswith(".png") for f in c)]
        self.assertEqual(len(image_chunks), 2)
        for chunk in image_chunks:
            self.assertEqual(len(chunk), 1)

    def test_the_plan_is_reproducible(self):
        """Same corpus, same plan. A plan that reshuffles invalidates a chunk archive
        that is keyed on chunk number."""
        detect = {"files": {"document": [f"/s/repositories/d{i % 4}/{i}.md" for i in range(40)]}}
        self.assertEqual(
            build_chunk_plan.plan_chunks(detect, 9), build_chunk_plan.plan_chunks(detect, 9)
        )

    def test_a_code_only_corpus_plans_nothing(self):
        self.assertEqual(build_chunk_plan.plan_chunks({"files": {"code": ["/s/a.py"]}}), {})


class TheWrittenPlan(SettingsIsolated):
    def _store(self, documents: int = 6, inside: bool = True) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        (root / "graphify-out").mkdir(parents=True)
        corpus = (root / "repositories" / "demo") if inside else (root.parent / "elsewhere")
        corpus.mkdir(parents=True, exist_ok=True)
        files = []
        for i in range(documents):
            path = corpus / f"doc{i}.md"
            path.write_text(f"# {i}\n", encoding="utf-8")
            files.append(str(path))
        config.configure(root=str(root))
        (root / "graphify-out" / ".graphify_detect.json").write_text(
            json.dumps({"scan_root": str(root), "files": {"document": sorted(files)}})
        )
        return root

    def _run(self, *argv: str) -> tuple[int, str]:
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            code = build_chunk_plan.main(list(argv))
        return code, out.getvalue()

    def test_nothing_machine_specific_is_written(self):
        self._store()
        code, _ = self._run("--chunk-size", "3")
        self.assertEqual(code, 0)
        written = json.loads(config.CHUNK_PLAN_PATH.read_text())
        paths = [f for files in written.values() for f in files]
        self.assertTrue(paths)
        self.assertEqual([p for p in paths if p.startswith("/")], [], "absolute paths committed")

    def test_a_dispatcher_still_receives_absolute_paths(self):
        """The round trip is the contract: the extraction spec requires agents to echo
        paths verbatim and absolute, so relativising at rest must not change what a
        dispatcher hands them."""
        self._store()
        self._run("--chunk-size", "3")
        loaded = store_paths.load_plan()
        paths = [f for files in loaded.values() for f in files]
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(path.startswith("/"), path)
            self.assertTrue(Path(path).is_file(), f"{path} does not resolve to a real file")

    def test_it_warns_when_paths_cannot_be_made_relative(self):
        """A corpus outside the store root stays absolute and the file looks fine, which
        is the silent half of the defect this stage removes."""
        self._store(inside=False)
        code, text = self._run("--chunk-size", "3")
        self.assertEqual(code, 0)
        self.assertIn("WARNING", text)
        self.assertIn("could not be made relative", text)

    def test_it_does_not_warn_when_they_can(self):
        """The sensitivity check on the test above: if the warning fired either way it
        would pass while saying nothing about relativising."""
        self._store(inside=True)
        _, text = self._run("--chunk-size", "3")
        self.assertNotIn("WARNING", text)

    def test_no_detection_results_is_a_refusal(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config.configure(root=tmp.name)
        code, text = self._run()
        self.assertEqual(code, 2)
        self.assertIn("No detection results", text)

    def test_a_nonsense_chunk_size_is_a_refusal(self):
        self._store()
        self.assertEqual(self._run("--chunk-size", "0")[0], 2)

    def test_uncached_restricts_the_plan(self):
        root = self._store(documents=6)
        detect = json.loads((root / "graphify-out" / ".graphify_detect.json").read_text())
        keep = detect["files"]["document"][:2]
        config.UNCACHED_PATH.write_text("\n".join(keep), encoding="utf-8")
        self._run("--chunk-size", "3", "--uncached")
        written = json.loads(config.CHUNK_PLAN_PATH.read_text())
        self.assertEqual(sum(len(v) for v in written.values()), 2)

    def test_a_code_only_estate_writes_no_plan_and_says_so(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        (root / "graphify-out").mkdir(parents=True)
        config.configure(root=str(root))
        config.DETECT_PATH.write_text(json.dumps({"files": {"code": ["/s/a.py"]}}))
        code, text = self._run()
        self.assertEqual(code, 0)
        self.assertIn("nothing to split", text)
        self.assertFalse(config.CHUNK_PLAN_PATH.exists(), "an empty plan was written anyway")


if __name__ == "__main__":
    unittest.main()
