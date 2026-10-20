"""The `index` stage and the `status` line that says whether its output is current.

Driven through `main()` against a real store layout: a graph under `graphify-out/`
in either of its two forms and the ticket layer where `config` puts it. Assertions
land on the files the stage wrote, its exit code and what it printed. `store_index`
itself is held by `test_store_index`; this module holds the stage around it.
Each test names the defect it catches.
"""

from __future__ import annotations

import contextlib
import io as stdio
import sqlite3
import tempfile
from pathlib import Path

from settings_isolation import SettingsIsolated
from knowledgestore import build_store_index, config, status
from test_store_index import LINKS, NODES, TICKETS, node, write_graph, write_tickets


class StageCase(SettingsIsolated):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        config.configure(root=self.root)
        (self.root / "graphify-out").mkdir()
        (self.root / "knowledge" / "intent").mkdir(parents=True)
        self.plain = self.root / "graphify-out" / "graph.json"
        self.archive = self.root / "graphify-out" / "graph.json.gz"
        write_graph(self.plain, NODES, LINKS)
        write_tickets(config.INTENT_INDEX_PATH, TICKETS)
        self.sqlite = self.root / "knowledge" / "index" / "store.sqlite"
        self.gz = self.sqlite.with_name("store.sqlite.gz")

    def run_stage(self, runner=build_store_index.main):
        out, err = stdio.StringIO(), stdio.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runner()
        return code, out.getvalue(), err.getvalue()

    def status_text(self) -> str:
        self.addCleanup(setattr, status, "run_git", status.run_git)
        status.run_git = lambda arguments: ""
        out = stdio.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(stdio.StringIO()):
            self.assertEqual(status.main([]), 0, "status never fails (CLAUDE.md 6.7)")
        return out.getvalue()

    def index_line(self, text: str) -> str:
        lines = [line for line in text.splitlines() if line.startswith("Query index")]
        self.assertEqual(len(lines), 1, text)
        return lines[0]


class TheStageWritesTheIndex(StageCase):
    def test_it_writes_both_files_and_reports_the_reconciliation_and_sizes(self):
        """Breaks if the stage stops writing the plain file or its archive, exits non-zero
        on a good graph, or stops saying what it read against what it wrote."""
        code, out, _ = self.run_stage()
        self.assertEqual(code, 0)
        self.assertTrue(self.sqlite.is_file())
        self.assertTrue(self.gz.is_file())
        connection = sqlite3.connect(f"file:{self.sqlite}?mode=ro", uri=True)
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM nodes").fetchone(), (5,))
        finally:
            connection.close()
        # 6 nodes read = 5 + 1 duplicate; 5 links read = 4 + 1 dangling + 0 duplicate.
        self.assertIn("6 nodes read = 5 nodes + 1 duplicate", out)
        self.assertIn("5 links read = 4 edges + 1 dangling + 0 duplicate", out)
        self.assertIn(f"{self.sqlite.stat().st_size:,} bytes", out)
        self.assertIn(f"{self.gz.stat().st_size:,} bytes", out)

    def test_a_second_run_over_unchanged_inputs_is_byte_identical(self):
        """Breaks if anything time- or order-dependent reaches either file: the stage's
        outputs are committed, so a rebuild that differs is a diff nobody made."""
        self.run_stage()
        first = (self.sqlite.read_bytes(), self.gz.read_bytes())
        self.run_stage()
        self.assertEqual((self.sqlite.read_bytes(), self.gz.read_bytes()), first)

    def test_the_archive_graph_is_read_when_the_plain_one_is_absent(self):
        """Breaks if the stage demands the gitignored plain graph: a fresh checkout holds
        only `graph.json.gz`, and the stage must run there."""
        self.plain.unlink()
        write_graph(self.archive, NODES, LINKS)
        code, _, _ = self.run_stage()
        self.assertEqual(code, 0)
        self.assertTrue(self.gz.is_file())


class TheStageRefusesCleanly(StageCase):
    def test_no_graph_exits_1_naming_both_paths_and_leaves_an_existing_index(self):
        """Breaks if a missing graph writes an empty index over a good one, or the
        message names only one of the two files the operator might supply."""
        self.run_stage()
        before = (self.sqlite.read_bytes(), self.gz.read_bytes())
        self.plain.unlink()
        code, out, err = self.run_stage()
        self.assertEqual(code, 1)
        self.assertIn(str(self.plain), out + err)
        self.assertIn(str(self.archive), out + err)
        self.assertEqual((self.sqlite.read_bytes(), self.gz.read_bytes()), before)

    def test_no_graph_writes_nothing(self):
        """Breaks if the refusal still creates the index directory or a partial file."""
        self.plain.unlink()
        self.run_stage()
        self.assertFalse((self.root / "knowledge" / "index").exists())

    def test_a_truncated_archive_exits_1_and_leaves_nothing_behind(self):
        """Breaks if TruncatedJson escapes as a traceback, or a partial index is left."""
        self.plain.unlink()
        write_graph(self.archive, NODES, LINKS)
        self.archive.write_bytes(self.archive.read_bytes()[:-20])
        code, _, err = self.run_stage()
        self.assertEqual(code, 1)
        self.assertTrue(err.strip(), "a refusal that says nothing is a puzzle")
        self.assertEqual(sorted(p.name for p in self.sqlite.parent.glob("*")), [])

    def test_an_unwritable_target_exits_2(self):
        """Breaks if a target that cannot be written is reported as a graph problem (1)
        or raised, so a caller cannot tell the two apart."""
        blocker = self.root / "knowledge" / "index"
        blocker.write_text("a file where the directory should be", encoding="utf-8")
        code, _, err = self.run_stage()
        self.assertEqual(code, 2)
        self.assertIn(str(self.sqlite), err)

    def test_missing_fts5_exits_1_naming_the_sqlite_version(self):
        """Breaks if a SQLite without FTS5 surfaces as a traceback, or the message does
        not say which SQLite this is."""
        self.addCleanup(
            setattr,
            build_store_index.store_index,
            "fts5_available",
            build_store_index.store_index.fts5_available,
        )
        build_store_index.store_index.fts5_available = lambda: False
        code, _, err = self.run_stage()
        self.assertEqual(code, 1)
        self.assertIn(sqlite3.sqlite_version, err)
        self.assertFalse(self.sqlite.exists())


class TheSizeWarning(StageCase):
    def test_the_warning_appears_over_the_threshold_and_the_exit_stays_0(self):
        """Breaks if an archive over the limit is committed without a word, or if the
        warning is promoted to a failure (the store decides whether to commit)."""
        config.configure(INDEX_WARN_MB=0)
        code, out, err = self.run_stage()
        self.assertEqual(code, 0)
        self.assertIn("100 MB", out + err)
        self.assertIn("knowledge/index/store.sqlite", out + err)

    def test_no_warning_at_the_default_for_a_small_archive(self):
        """Breaks if the warning fires for every store, which teaches readers to ignore it."""
        code, out, err = self.run_stage()
        self.assertEqual(code, 0)
        self.assertNotIn("100 MB", out + err)

    def test_the_threshold_is_exclusive_of_an_archive_exactly_at_it(self):
        """Breaks if `>` becomes `>=`: an archive of exactly the limit is not over it."""
        self.run_stage()
        size = self.gz.stat().st_size
        self.assertGreater(size, 0)
        # INDEX_WARN_MB is whole megabytes, so test the boundary through the helper.
        self.assertFalse(build_store_index.over_warning(size, size / 1_000_000))
        self.assertTrue(build_store_index.over_warning(size + 1, size / 1_000_000))

    def test_the_default_is_90_mb(self):
        """Breaks if the default moves toward GitHub's refusal point, leaving no room
        to decide before the push fails."""
        self.assertEqual(config.INDEX_WARN_MB, 90)


class StatusReportsTheIndex(StageCase):
    def test_no_index_says_how_to_build_one(self):
        """Breaks if status is silent about the index, so a store never learns it lacks one."""
        line = self.index_line(self.status_text())
        self.assertIn("none", line)
        self.assertIn("knowledgestore index", line)

    def test_a_fresh_index_reports_its_archive_size_and_that_it_is_current(self):
        """Breaks if a current index is reported stale, or the archive size is dropped."""
        self.run_stage()
        line = self.index_line(self.status_text())
        self.assertIn("current with the graph", line)
        self.assertNotIn("stale", line)
        self.assertIn(f"{self.gz.stat().st_size:,} bytes", line)

    def test_a_changed_graph_makes_it_stale(self):
        """Breaks if the comparison reads the wrong file or never compares: a reader
        would open an index describing a graph that is gone."""
        self.run_stage()
        write_graph(self.plain, NODES + [node("z9", "One more", file="x.py")], LINKS)
        line = self.index_line(self.status_text())
        self.assertIn("stale", line)
        self.assertIn("knowledgestore index", line)

    def test_an_index_built_from_the_plain_graph_is_not_judged_against_the_archive(self):
        """Breaks if status hashes a fixed `graph.json.gz` instead of the file
        `meta.graph` names: an index built from the plain graph would read as stale
        against the archive beside it, and the reverse."""
        write_graph(self.archive, NODES + [node("z9", "Archive only", file="x.py")], LINKS)
        self.run_stage()  # the plain graph wins, so meta.graph is graph.json
        self.assertIn("current with the graph", self.index_line(self.status_text()))

    def test_an_index_built_from_the_archive_reads_current_after_the_plain_file_is_added(self):
        """Breaks if status follows whichever graph exists now instead of the one the
        index recorded."""
        self.plain.unlink()
        write_graph(self.archive, NODES, LINKS)
        self.run_stage()
        write_graph(self.plain, NODES + [node("z9", "Newer", file="x.py")], LINKS)
        self.assertIn("current with the graph", self.index_line(self.status_text()))
        write_graph(self.archive, NODES + [node("z9", "Newer", file="x.py")], LINKS)
        self.assertIn("stale", self.index_line(self.status_text()))

    def test_status_never_decompresses_the_archive_or_fails_on_a_damaged_index(self):
        """Breaks (CLAUDE.md 6.7) if a plain file that is not a database makes status raise."""
        self.run_stage()
        self.sqlite.write_bytes(b"not a database")
        self.assertIn("Query index", self.status_text())

    def test_only_the_archive_present_is_reported_without_a_freshness_claim(self):
        """Breaks if status claims currency it cannot have checked: `meta` is read from
        the plain file only, so with it absent the comparison is not made."""
        self.run_stage()
        self.sqlite.unlink()
        line = self.index_line(self.status_text())
        self.assertNotIn("current with the graph", line)
        self.assertNotIn("stale", line)
        self.assertIn(f"{self.gz.stat().st_size:,} bytes", line)
