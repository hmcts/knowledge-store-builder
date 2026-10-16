"""`check-yield`: per-file node counts of new chunk extractions against the layer they replace.

A wave can pass every chunk gate and still say far less than the layer it is about to
replace: the gate checks shape, never how much a file yielded. These tests build real
chunk files and a real baseline on disk, run the stage through its `main` and through
the `knowledgestore` entry point, and assert on the exit code and the printed report.
Every expected figure below is derived by hand in the comment beside it.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated

from knowledgestore import check_yield, cli
from knowledgestore import io as pio


def nodes(path: str, count: int) -> list[dict]:
    return [{"id": f"{Path(path).stem}_{i}", "source_file": path} for i in range(count)]


def chunk(*parts: tuple[str, int]) -> str:
    found = [node for path, count in parts for node in nodes(path, count)]
    return json.dumps({"nodes": found, "edges": [], "hyperedges": []})


class Base(SettingsIsolated):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.chunks = self.root / "chunks"
        self.chunks.mkdir()

    def write_chunk(self, number: str, *parts: tuple[str, int]) -> Path:
        path = self.chunks / f".graphify_chunk_{number}.json"
        path.write_text(chunk(*parts), encoding="utf-8")
        return path

    def write_baseline(self, *parts: tuple[str, int], name: str = "baseline.json") -> Path:
        path = self.root / name
        payload = {"nodes": [n for p, c in parts for n in nodes(p, c)], "links": []}
        if name.endswith(".gz"):
            pio.write_gzip_json(path, payload)
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def run_stage(self, baseline: Path, *extra: str) -> tuple[int, str, str]:
        out, err = _io.StringIO(), _io.StringIO()
        argv = ["--chunks", str(self.chunks), "--baseline", str(baseline), *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check_yield.main(argv)
        return code, out.getvalue(), err.getvalue()

    def three_files(self, name: str = "baseline.json") -> Path:
        # a: 1 against 10, b: 4 against 4, c: 3 against 6; new.md only in the chunks,
        # gone.md only in the baseline.
        self.write_chunk("0001", ("/r/a.md", 1), ("/r/b.md", 4), ("/r/new.md", 2))
        self.write_chunk("0002", ("/r/c.md", 3))
        return self.write_baseline(
            ("/r/a.md", 10), ("/r/b.md", 4), ("/r/c.md", 6), ("/r/gone.md", 5), name=name
        )


class RatioTests(Base):
    def test_the_overall_ratio_is_total_over_total_on_the_shared_files(self):
        """Breaks if the ratio is a mean of per-file ratios, or counts unshared files.

        Shared files a, b, c: new 1 + 4 + 3 = 8 against 10 + 4 + 6 = 20, so 0.40x and
        below the 0.5 floor. The mean of the per-file ratios is (0.1 + 1.0 + 0.5) / 3 =
        0.53x, which would pass; counting new.md and gone.md would give 10 / 25.
        """
        code, out, _ = self.run_stage(self.three_files())
        self.assertEqual(code, 1, out)
        self.assertIn("yield: 8 nodes against 20 over 3 shared files = 0.40x (floor 0.50x)", out)
        self.assertIn("files only in the new chunks: 1; only in the baseline: 1", out)
        self.assertIn("BELOW FLOOR: 0.40x is under 0.50x", out)

    def test_files_below_the_file_floor_are_counted_and_the_worst_are_listed_worst_first(self):
        """Breaks if a file exactly at the file floor is counted, or the listing is unsorted.

        a is 0.1x (below 0.5), c is exactly 0.5x (not below), b is 1.0x: one of three.
        """
        _, out, _ = self.run_stage(self.three_files())
        self.assertIn("files below 0.50x their baseline: 1 of 3", out)
        listed = [line.split()[-1] for line in out.splitlines() if line.startswith("  ")]
        self.assertEqual(listed, ["/r/a.md", "/r/c.md", "/r/b.md"])
        self.assertIn("  0.10x       1 /     10  /r/a.md", out)

    def test_a_raised_file_floor_counts_the_file_it_now_exceeds(self):
        # Break: --file-floor read but not used. At 0.6, c (0.5x) is below as well.
        _, out, _ = self.run_stage(self.three_files(), "--file-floor", "0.6")
        self.assertIn("files below 0.60x their baseline: 2 of 3", out)

    def test_only_the_twenty_worst_are_listed(self):
        # Break: the listing unbounded, which on a real wave is hundreds of lines.
        parts = [(f"/r/f{i:02d}.md", 1) for i in range(25)]
        self.write_chunk("0001", *parts)
        baseline = self.write_baseline(*[(p, 2) for p, _ in parts])
        _, out, _ = self.run_stage(baseline)
        listed = [line for line in out.splitlines() if line.startswith("  ")]
        self.assertEqual(len(listed), 20)
        self.assertIn("worst 20 of 25:", out)


class FloorTests(Base):
    def test_a_ratio_exactly_at_the_floor_passes_and_one_node_fewer_fails(self):
        """Breaks if the floor comparison is `<=` rather than `<`.

        5 against 10 is exactly 0.5x, which is not below a 0.5 floor; 4 against 10 is.
        """
        self.write_chunk("0001", ("/r/a.md", 5))
        baseline = self.write_baseline(("/r/a.md", 10))
        code, out, _ = self.run_stage(baseline, "--floor", "0.5")
        self.assertEqual(code, 0, out)
        self.assertIn("= 0.50x (floor 0.50x)", out)

        self.write_chunk("0001", ("/r/a.md", 4))
        code, out, _ = self.run_stage(baseline, "--floor", "0.5")
        self.assertEqual(code, 1, out)

    def test_the_floor_flag_moves_the_verdict(self):
        # Break: --floor parsed but the default used. 8/20 = 0.4 passes a 0.4 floor.
        code, out, _ = self.run_stage(self.three_files(), "--floor", "0.4")
        self.assertEqual(code, 0, out)


class NothingComparedTests(Base):
    def test_no_shared_file_exits_2_and_shows_a_path_from_each_side(self):
        """Breaks if a comparison over no files passes (CLAUDE.md 2.10).

        The usual cause is one side citing relative paths and the other absolute ones,
        so the report shows one of each for the operator to see the mismatch.
        """
        self.write_chunk("0001", ("/abs/r/a.md", 3))
        baseline = self.write_baseline(("r/a.md", 3))
        code, out, err = self.run_stage(baseline)
        self.assertEqual(code, 2, out)
        self.assertIn("no file is in both", err)
        self.assertIn("/abs/r/a.md", err)
        self.assertIn("r/a.md", err)

    def test_an_empty_chunk_directory_exits_2(self):
        # Break: zero chunks read reported as a ratio, or a crash.
        baseline = self.write_baseline(("/r/a.md", 3))
        code, _, err = self.run_stage(baseline)
        self.assertEqual(code, 2)
        self.assertIn("no .graphify_chunk_*.json", err)

    def test_an_unreadable_chunk_exits_2_and_names_it(self):
        # Break: a truncated chunk skipped, so the ratio describes part of the wave.
        self.write_chunk("0001", ("/r/a.md", 3))
        (self.chunks / ".graphify_chunk_0002.json").write_text('{"nodes": [', encoding="utf-8")
        baseline = self.write_baseline(("/r/a.md", 3))
        code, _, err = self.run_stage(baseline)
        self.assertEqual(code, 2)
        self.assertIn(".graphify_chunk_0002.json", err)

    def test_a_missing_or_shapeless_baseline_exits_2(self):
        # Break: an absent baseline read as an empty layer, so every file is new-only.
        self.write_chunk("0001", ("/r/a.md", 3))
        code, _, err = self.run_stage(self.root / "absent.json")
        self.assertEqual(code, 2)
        self.assertIn("absent.json", err)
        shapeless = self.root / "list.json"
        shapeless.write_text("[]", encoding="utf-8")
        code, _, err = self.run_stage(shapeless)
        self.assertEqual(code, 2)
        self.assertIn("nodes", err)

    def test_the_plan_beside_the_chunks_is_not_read_as_a_chunk(self):
        # Break: `.graphify_chunk_plan.json` matching the glob and refused as unreadable.
        self.write_chunk("0001", ("/r/a.md", 3))
        (self.chunks / ".graphify_chunk_plan.json").write_text('{"0001": []}', encoding="utf-8")
        code, out, err = self.run_stage(self.write_baseline(("/r/a.md", 3)))
        self.assertEqual(code, 0, out + err)
        self.assertIn("1 chunk files", out)


class DeterminismTests(Base):
    def test_tied_files_are_listed_by_path(self):
        """Breaks if the sort drops its path tiebreak (CLAUDE.md 1.3).

        Six files all at 0.5x, written in an order that is not sorted: only the path
        tiebreak puts them in one order, whatever the set iteration happened to be.
        """
        names = ["/r/e.md", "/r/b.md", "/r/f.md", "/r/a.md", "/r/d.md", "/r/c.md"]
        self.write_chunk("0001", *[(n, 1) for n in names])
        baseline = self.write_baseline(*[(n, 2) for n in names])
        _, first, _ = self.run_stage(baseline)
        listed = [line.split()[-1] for line in first.splitlines() if line.startswith("  ")]
        self.assertEqual(listed, sorted(names))
        _, second, _ = self.run_stage(baseline)
        self.assertEqual(first, second)


class BaselineFormatTests(Base):
    def test_a_gzipped_baseline_gives_the_same_report(self):
        """Breaks if the baseline is read with a plain JSON read that cannot open `.gz`.

        Stores commit the compressed graph, so it is often the only copy on disk.
        """
        plain = self.three_files()
        _, expected, _ = self.run_stage(plain)
        packed = self.write_baseline(
            ("/r/a.md", 10), ("/r/b.md", 4), ("/r/c.md", 6), ("/r/gone.md", 5), name="b.json.gz"
        )
        code, out, err = self.run_stage(packed)
        self.assertEqual(code, 1, err)
        self.assertEqual(out.replace(str(packed), str(plain)), expected)


class EntryPointTests(Base):
    def test_the_stage_is_reachable_through_knowledgestore_with_its_flags(self):
        """Breaks if `check-yield` is unregistered, or registered without owning its flags.

        A stage missing from `SELF_PARSING` has its arguments refused by the guard in
        `cli.main` with exit 2, which here would read as "nothing compared".
        """
        baseline = self.three_files()
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()):
            code = cli.main(
                ["check-yield", "--chunks", str(self.chunks), "--baseline", str(baseline)]
            )
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("= 0.40x", out.getvalue())


if __name__ == "__main__":
    unittest.main()
