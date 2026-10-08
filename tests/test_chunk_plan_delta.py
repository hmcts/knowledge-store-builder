"""`chunk-plan --delta` and `--one-per-batch`: a resync's chunks, sized for what it costs.

A delta touches a file or two per directory, so the full plan's one-directory-per-chunk
rule pays a worker's fixed cost once per directory. A delta is packed per repository
instead, and each chunk can go to its own batch so no worker carries another chunk's
reads in its context.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import tempfile
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from test_drift import Estate, _commit

from knowledgestore import build_chunk_plan, config, drift


class _Delta(SettingsIsolated):
    def _store(self, plan: dict[str, list[str]] | None = None, images: tuple = ()) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        config.configure(root=str(root))
        # The detect result is where the full planner learns which files are images,
        # and a delta reads the same one. Absolute, as graphify writes it.
        detect = {"files": {"document": [], "image": [str(root / f) for f in images]}}
        config.DETECT_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.DETECT_PATH.write_text(json.dumps(detect), encoding="utf-8")
        if plan is not None:
            config.CHUNK_PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
            config.CHUNK_PLAN_PATH.write_text(json.dumps(plan), encoding="utf-8")
        return root

    def _drift(self, root: Path, new: list[str], changed: list[str], deleted=()) -> Path:
        path = root / "drift.json"
        stale = sorted({*new, *changed, *deleted})
        document = {"stale": stale, "new": new, "changed": changed, "deleted": list(deleted)}
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = _io.StringIO(), _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = build_chunk_plan.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _delta(self, root: Path, delta: Path, *argv: str) -> tuple[int, dict]:
        out = root / "additions.json"
        code, _, _ = self._run("--delta", str(delta), "--out", str(out), *argv)
        return code, (json.loads(out.read_text(encoding="utf-8")) if out.exists() else {})


class DeltaTest(_Delta):
    def test_a_delta_is_packed_per_repository_not_per_directory(self):
        """Breaks if the delta reuses the full plan's one-directory rule, which pays a
        worker per directory, or mixes two repositories into one chunk. New chunks are
        numbered after the plan's last, whatever order its keys are in."""
        root = self._store({"0007": ["repositories/z/a.md"], "0001": ["repositories/z/b.md"]})
        alpha = [
            "repositories/alpha/apps/a/f0.yaml",
            "repositories/alpha/apps/b/f1.yaml",
            "repositories/alpha/charts/c/f2.yaml",
        ]
        delta = self._drift(root, new=alpha[:1], changed=[*alpha[1:], "repositories/beta/x/f.tf"])
        code, additions = self._delta(root, delta)
        self.assertEqual(code, 0)
        self.assertEqual(additions, {"0008": alpha, "0009": ["repositories/beta/x/f.tf"]})

    def test_a_chunk_fills_to_the_chunk_size_in_directory_order(self):
        """Breaks if `--chunk-size` stops being the fill, or the order within a
        repository stops following the directories."""
        root = self._store({"0001": ["repositories/z/a.md"]})
        files = [f"repositories/alpha/d{i}/f.yaml" for i in (4, 2, 0, 3, 1)]
        code, additions = self._delta(root, self._drift(root, files, []), "--chunk-size", "2")
        self.assertEqual(code, 0)
        self.assertEqual(
            additions,
            {
                "0002": ["repositories/alpha/d0/f.yaml", "repositories/alpha/d1/f.yaml"],
                "0003": ["repositories/alpha/d2/f.yaml", "repositories/alpha/d3/f.yaml"],
                "0004": ["repositories/alpha/d4/f.yaml"],
            },
        )

    def test_a_directorys_files_stay_together_when_a_sibling_sorts_between_them(self):
        """Breaks if the order is plain path order: `b-c/` sorts between `b/y` and `b/z`
        as a string, which would split directory `b` across two chunks."""
        root = self._store({"0001": ["repositories/z/a.md"]})
        files = ["repositories/a/b/z.md", "repositories/a/b-c/f.md", "repositories/a/b/y.md"]
        _, additions = self._delta(root, self._drift(root, files, []), "--chunk-size", "2")
        self.assertEqual(
            additions,
            {
                "0002": ["repositories/a/b/y.md", "repositories/a/b/z.md"],
                "0003": ["repositories/a/b-c/f.md"],
            },
        )

    def test_a_packing_that_places_a_file_twice_is_refused(self):
        """Breaks if the exactly-once check is lost: a file in two chunks is extracted
        twice and merged as duplicates. The packer is replaced here because the real
        one cannot produce the fault the check exists to stop."""
        root = self._store({"0001": ["repositories/z/a.md"]})
        delta = self._drift(root, ["repositories/a/n.md"], [])
        twice = {"0002": ["repositories/a/n.md"], "0003": ["repositories/a/n.md"]}
        original = build_chunk_plan.pack_delta
        build_chunk_plan.pack_delta = lambda *_: twice
        self.addCleanup(setattr, build_chunk_plan, "pack_delta", original)
        code, additions = self._delta(root, delta)
        self.assertEqual(code, 2)
        self.assertEqual(additions, {}, "a delta placing a file twice was written")

    def test_deleted_files_are_not_planned(self):
        """Breaks if the delta plans `stale` rather than new and changed: a deleted
        file has nothing to extract and would fail the worker that read it."""
        root = self._store({"0001": ["repositories/z/a.md"]})
        delta = self._drift(
            root, ["repositories/a/n.md"], ["repositories/a/c.md"], ["repositories/a/gone.md"]
        )
        _, additions = self._delta(root, delta)
        planned = [f for files in additions.values() for f in files]
        self.assertEqual(sorted(planned), ["repositories/a/c.md", "repositories/a/n.md"])

    def test_the_delta_never_touches_the_committed_plan(self):
        """Breaks if the mapping is appended to the plan before its chunks exist, which
        would leave the plan naming extractions nobody has written. Without --out the
        mapping is the whole of stdout."""
        root = self._store({"0001": ["repositories/z/a.md"]})
        committed = config.CHUNK_PLAN_PATH.read_bytes()
        delta = self._drift(root, ["repositories/a/n.md"], [])
        code, text, _ = self._run("--delta", str(delta))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text), {"0002": ["repositories/a/n.md"]})
        self.assertEqual(config.CHUNK_PLAN_PATH.read_bytes(), committed)

    def test_a_delta_with_no_plan_to_extend_is_refused(self):
        """Breaks if numbering silently restarts at 0001, colliding with chunks that
        already exist on disk under those numbers."""
        root = self._store()
        code, text, err = self._run("--delta", str(self._drift(root, ["repositories/a/n.md"], [])))
        self.assertEqual(code, 2)
        self.assertIn("plan", text + err)

    def test_the_drift_stages_own_output_is_accepted(self):
        """Breaks if `--delta` reads a shape `drift` does not write - the contract is
        the stage's output, not any one store's file."""
        root = self._store()
        estate = Estate(root)
        repo = estate.clone("alpha")
        (repo / "a.md").write_text("a\n", encoding="utf-8")
        first = _commit(repo, "one")
        (repo / "a.md").write_text("b\n", encoding="utf-8")
        (repo / "b.md").write_text("b\n", encoding="utf-8")
        second = _commit(repo, "two")
        estate.before["alpha"] = {"sha": first}
        estate.after["alpha"] = {"sha": second}
        before, _ = estate.write(["repositories/alpha/a.md"])
        measured = root / "measured.json"
        with contextlib.redirect_stdout(_io.StringIO()):
            self.assertEqual(drift.main(["--before", str(before), "--out", str(measured)]), 0)
        _, additions = self._delta(root, measured)
        self.assertEqual(
            additions, {"0002": ["repositories/alpha/a.md", "repositories/alpha/b.md"]}
        )

    def test_each_image_in_a_delta_gets_its_own_chunk(self):
        """Breaks if a delta packs an image in with documents, which the full plan
        never does: vision needs its own context, and a delta extracts what a full
        build would."""
        image = "repositories/alpha/d/x.png"
        root = self._store({"0001": ["repositories/z/a.md"]}, images=(image,))
        delta = self._drift(
            root, [image], ["repositories/alpha/d/y.md", "repositories/alpha/d/z.md"]
        )
        code, additions = self._delta(root, delta)
        self.assertEqual(code, 0)
        self.assertEqual(
            additions,
            {"0002": [image], "0003": ["repositories/alpha/d/y.md", "repositories/alpha/d/z.md"]},
        )

    def test_a_delta_without_a_detect_result_is_refused(self):
        """Breaks if a missing detect result is read as "no images", which packs every
        image in with documents and says nothing."""
        root = self._store({"0001": ["repositories/z/a.md"]})
        config.DETECT_PATH.unlink()
        code, additions = self._delta(root, self._drift(root, ["repositories/a/x.png"], []))
        self.assertEqual(code, 2)
        self.assertEqual(additions, {}, "a delta was planned without knowing its images")

    def test_a_missing_drift_file_is_refused(self):
        """Breaks if a mistyped path is read as an empty drift: that exits 0 saying
        nothing changed, which is the wrong answer to a typo."""
        root = self._store({"0001": ["repositories/z/a.md"]})
        out = root / "additions.json"
        code, text, _ = self._run("--delta", str(root / "no-such-drift.json"), "--out", str(out))
        self.assertEqual(code, 2)
        self.assertIn("no-such-drift.json", text)
        self.assertFalse(out.exists())

    def test_an_empty_delta_says_so_and_writes_nothing(self):
        """Breaks if an empty delta writes an empty mapping, which a resync would
        append as a no-op and read as a plan that succeeded. Exit 0: nothing changing
        is not a failure."""
        root = self._store({"0001": ["repositories/z/a.md"]})
        out = root / "additions.json"
        delta = self._drift(root, [], [], ["repositories/a/gone.md"])
        code, text, _ = self._run("--delta", str(delta), "--out", str(out))
        self.assertEqual(code, 0)
        self.assertIn("no new or changed files", text)
        self.assertIn("Nothing written", text)
        self.assertFalse(out.exists(), "an empty mapping was written")


class OnePerBatchTest(_Delta):
    def test_each_batch_holds_exactly_one_chunk_with_absolute_files(self):
        """Breaks if a batch carries more than one chunk, which puts every earlier
        chunk's reads in the context of the worker extracting the last; or if files
        reach a worker relative, which the extraction spec forbids."""
        root = self._store({"0001": ["repositories/z/a.md"]})
        files = [f"repositories/alpha/d{i}/f.md" for i in range(5)]
        batches = root / "batches"
        chunks = root / "trial-chunks"
        code, _, _ = self._run(
            "--delta",
            str(self._drift(root, files, [])),
            "--out",
            str(root / "additions.json"),
            "--chunk-size",
            "2",
            "--one-per-batch",
            str(batches),
            "--chunk-out",
            str(chunks),
        )
        self.assertEqual(code, 0)
        written = sorted(p.name for p in batches.iterdir())
        self.assertEqual(written, ["batch_0002.json", "batch_0003.json", "batch_0004.json"])
        first = json.loads((batches / "batch_0002.json").read_text(encoding="utf-8"))
        self.assertEqual(
            first,
            {
                "chunks": [
                    {
                        "n": 2,
                        "out": str(chunks / ".graphify_chunk_0002.json"),
                        "files": [str(root / f) for f in files[:2]],
                    }
                ]
            },
        )
        for name in written:
            batch = json.loads((batches / name).read_text(encoding="utf-8"))
            self.assertEqual(len(batch["chunks"]), 1, name)

    def test_out_defaults_to_the_live_chunk_directory(self):
        """Breaks if a batch's `out` stops naming where `merge-chunks` and
        `chunk-status` look, so finished chunks would be invisible to both."""
        root = self._store({"0001": ["repositories/z/a.md"]})
        batches = root / "batches"
        self._run(
            "--delta",
            str(self._drift(root, ["repositories/a/n.md"], [])),
            "--out",
            str(root / "additions.json"),
            "--one-per-batch",
            str(batches),
        )
        batch = json.loads((batches / "batch_0002.json").read_text(encoding="utf-8"))
        expected = config.CHUNK_PLAN_PATH.parent / ".graphify_chunk_0002.json"
        self.assertEqual(batch["chunks"][0]["out"], str(expected))

    def test_a_full_plan_can_be_batched_one_chunk_at_a_time(self):
        """Breaks if `--one-per-batch` is wired to the delta alone: a full rebuild is
        the case where the per-worker context cost was first measured."""
        root = self._store()
        files = [str(root / "repositories" / f"r{i}" / "doc.md") for i in range(3)]
        config.DETECT_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.DETECT_PATH.write_text(json.dumps({"files": {"document": files}}), encoding="utf-8")
        batches = root / "batches"
        code, _, _ = self._run("--one-per-batch", str(batches))
        self.assertEqual(code, 0)
        written = sorted(p.name for p in batches.iterdir())
        self.assertEqual(written, ["batch_0001.json", "batch_0002.json", "batch_0003.json"])
        batch = json.loads((batches / "batch_0003.json").read_text(encoding="utf-8"))
        self.assertEqual(batch["chunks"][0]["files"], [files[2]])


if __name__ == "__main__":
    import unittest

    unittest.main()
