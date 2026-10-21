"""The `index` stage and the `status` line that says whether its output is current.

Driven through `main()` against a real store layout: a graph under `graphify-out/`
in either of its two forms and the ticket layer where `config` puts it. Assertions
land on the files the stage wrote, its exit code and what it printed. `store_index`
itself is held by `test_store_index`; this module holds the stage around it.
Each test names the defect it catches.
"""

from __future__ import annotations

import contextlib
import gzip
import io as stdio
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated
from unittest import mock

from knowledgestore import build_store_index, config, status, store_index
from test_store_index import LINKS, NODES, TICKETS, node, write_graph, write_tickets


def run_stage(runner=build_store_index.main):
    out, err = stdio.StringIO(), stdio.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = runner()
    return code, out.getvalue(), err.getvalue()


def status_text(case: unittest.TestCase) -> str:
    case.addCleanup(setattr, status, "run_git", status.run_git)
    status.run_git = lambda arguments: ""
    out = stdio.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(stdio.StringIO()):
        code = status.main([])
    case.assertEqual(code, 0, "status never fails (CLAUDE.md 6.7)")
    return out.getvalue()


def index_line(case: unittest.TestCase, text: str) -> str:
    lines = [line for line in text.splitlines() if line.startswith("Query index")]
    case.assertEqual(len(lines), 1, text)
    return lines[0]


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


class TheStageWritesTheIndex(StageCase):
    def test_it_writes_both_files_and_reports_the_reconciliation_and_sizes(self):
        """Breaks if the stage stops writing the plain file or its archive, exits non-zero
        on a good graph, or stops saying what it read against what it wrote."""
        code, out, _ = run_stage()
        self.assertEqual(code, 0)
        self.assertTrue(self.sqlite.is_file())
        self.assertTrue(self.gz.is_file())
        connection = sqlite3.connect(self.sqlite.as_uri() + "?mode=ro", uri=True)
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
        run_stage()
        first = (self.sqlite.read_bytes(), self.gz.read_bytes())
        run_stage()
        self.assertEqual((self.sqlite.read_bytes(), self.gz.read_bytes()), first)

    def test_the_archive_is_read_when_both_graphs_exist(self):
        """Breaks if the stage prefers the gitignored plain graph: the index would record
        a file `status` never compares, and a builder and a peer would differ."""
        write_graph(self.archive, NODES + [node("z9", "Archive only", file="x.py")], LINKS)
        code, _, _ = run_stage()
        self.assertEqual(code, 0)
        connection = sqlite3.connect(self.sqlite.as_uri() + "?mode=ro", uri=True)
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM nodes").fetchone(), (6,))
            self.assertEqual(
                connection.execute("SELECT value FROM meta WHERE key = 'graph'").fetchone(),
                ("graphify-out/graph.json.gz",),
            )
        finally:
            connection.close()

    def test_a_builder_with_both_graphs_and_a_peer_with_only_the_archive_write_the_same_bytes(self):
        """Breaks if the bytes depend on whether the plain graph happens to be on disk:
        an unchanged store would then commit a diff on every other machine."""
        write_graph(self.archive, NODES, LINKS)
        run_stage()
        builder = (self.sqlite.read_bytes(), self.gz.read_bytes())
        self.plain.unlink()
        run_stage()
        self.assertEqual((self.sqlite.read_bytes(), self.gz.read_bytes()), builder)

    def test_the_archive_graph_is_read_when_the_plain_one_is_absent(self):
        """Breaks if the stage demands the gitignored plain graph: a fresh checkout holds
        only `graph.json.gz`, and the stage must run there."""
        self.plain.unlink()
        write_graph(self.archive, NODES, LINKS)
        code, _, _ = run_stage()
        self.assertEqual(code, 0)
        self.assertTrue(self.gz.is_file())


class TheStageRefusesCleanly(StageCase):
    def test_no_graph_exits_1_naming_both_paths_and_leaves_an_existing_index(self):
        """Breaks if a missing graph writes an empty index over a good one, or the
        message names only one of the two files the operator might supply."""
        run_stage()
        before = (self.sqlite.read_bytes(), self.gz.read_bytes())
        self.plain.unlink()
        code, out, err = run_stage()
        self.assertEqual(code, 1)
        self.assertIn(str(self.plain), out + err)
        self.assertIn(str(self.archive), out + err)
        self.assertEqual((self.sqlite.read_bytes(), self.gz.read_bytes()), before)

    def test_no_graph_writes_nothing(self):
        """Breaks if the refusal still creates the index directory or a partial file."""
        self.plain.unlink()
        run_stage()
        self.assertFalse((self.root / "knowledge" / "index").exists())

    def test_a_truncated_archive_exits_1_and_leaves_nothing_behind(self):
        """Breaks if TruncatedJson escapes as a traceback, or a partial index is left."""
        self.plain.unlink()
        write_graph(self.archive, NODES, LINKS)
        self.archive.write_bytes(self.archive.read_bytes()[:-20])
        code, _, err = run_stage()
        self.assertEqual(code, 1)
        self.assertTrue(err.strip(), "a refusal that says nothing is a puzzle")
        self.assertEqual(sorted(p.name for p in self.sqlite.parent.glob("*")), [])

    def test_an_unwritable_target_exits_2(self):
        """Breaks if a target that cannot be written is reported as a graph problem (1)
        or raised, so a caller cannot tell the two apart."""
        blocker = self.root / "knowledge" / "index"
        blocker.write_text("a file where the directory should be", encoding="utf-8")
        code, _, err = run_stage()
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
        code, _, err = run_stage()
        self.assertEqual(code, 1)
        self.assertIn(sqlite3.sqlite_version, err)
        self.assertFalse(self.sqlite.exists())


class InputErrorsAreNotOutputErrors(StageCase):
    """Exit 2 is for the target. A graph or ticket layer that cannot be read is exit 1."""

    def written(self):
        return sorted(p.name for p in self.sqlite.parent.glob("*"))

    def assert_refused_as_input(self, expected: str):
        code, _, err = run_stage()
        self.assertEqual(code, 1, err)
        self.assertIn(expected, err)
        self.assertEqual(self.written(), [])

    def test_a_graph_archive_that_is_not_gzip_exits_1(self):
        """Breaks if BadGzipFile, an OSError, is reported as an unwritable target (2)."""
        self.plain.unlink()
        self.archive.write_bytes(b"this is not a gzip stream")
        self.assert_refused_as_input(str(self.archive))

    def test_a_plain_graph_that_is_not_json_exits_1(self):
        """Breaks if a JSON error, a ValueError, falls into the branch for a target that
        climbs upward and exits 2."""
        self.plain.write_text('{"nodes": [{"id": "a1"}, not json', encoding="utf-8")
        code, _, err = run_stage()
        self.assertEqual(code, 1, err)
        self.assertEqual(self.written(), [])

    def test_a_ticket_layer_that_is_not_gzip_exits_1(self):
        """Breaks if a corrupt `file-tickets.json.gz` exits 2, or escapes as a traceback."""
        config.INTENT_INDEX_PATH.write_bytes(b"not gzip")
        self.assert_refused_as_input(str(config.INTENT_INDEX_PATH))

    def test_a_ticket_layer_that_is_not_json_exits_1(self):
        """Breaks if valid gzip around invalid JSON exits 2."""
        with gzip.open(config.INTENT_INDEX_PATH, "wt", encoding="utf-8") as handle:
            handle.write("{broken")
        self.assert_refused_as_input(str(config.INTENT_INDEX_PATH))

    def test_an_unreadable_graph_exits_1(self):
        """Breaks if a permission error on the input is reported as an output error: the
        path in the error is the graph's, not the target's."""
        self.plain.chmod(0)
        self.addCleanup(self.plain.chmod, 0o644)
        self.assert_refused_as_input(str(self.plain))

    def test_an_edge_the_build_loses_exits_1_with_its_message(self):
        """Breaks if the reconciliation failure escapes as a traceback: it is the build
        refusing to write an index whose counts do not add up."""
        lossy = store_index.DDL + (
            "CREATE TRIGGER lose BEFORE INSERT ON edges WHEN NEW.source = 1"
            " BEGIN SELECT RAISE(IGNORE); END;"
        )
        with mock.patch.object(store_index, "DDL", lossy):
            code, _, err = run_stage()
        self.assertEqual(code, 1)
        self.assertIn("an edge was lost", err)
        self.assertFalse(self.sqlite.exists())


class TheSizeWarning(StageCase):
    def test_the_warning_appears_over_the_threshold_and_the_exit_stays_0(self):
        """Breaks if an archive over the limit is committed without a word, or if the
        warning is promoted to a failure (the store decides whether to commit)."""
        config.configure(INDEX_WARN_MB=0)
        code, out, err = run_stage()
        self.assertEqual(code, 0)
        self.assertIn("100 MB", err)
        self.assertIn("knowledge/index/store.sqlite", err)
        self.assertNotIn("100 MB", out, "the report on stdout is what a script parses")

    def test_no_warning_at_the_default_for_a_small_archive(self):
        """Breaks if the warning fires for every store, which teaches readers to ignore it."""
        code, out, err = run_stage()
        self.assertEqual(code, 0)
        self.assertNotIn("100 MB", out + err)

    def test_the_threshold_is_exclusive_of_an_archive_exactly_at_it(self):
        """Breaks if `>` becomes `>=`: an archive of exactly the limit is not over it.
        Whole numbers, so no rounding decides the boundary."""
        self.assertFalse(build_store_index.over_warning(2_000_000, 2))
        self.assertTrue(build_store_index.over_warning(2_000_001, 2))

    def test_a_threshold_given_as_a_string_through_configure_is_a_number(self):
        """Breaks if `configure(INDEX_WARN_MB="0")` stores the string: the comparison
        then raises a TypeError after a full build, instead of warning."""
        config.configure(INDEX_WARN_MB="0")
        self.assertEqual(config.INDEX_WARN_MB, 0)
        code, _, err = run_stage()
        self.assertEqual(code, 0)
        self.assertIn("100 MB", err)

    def test_the_threshold_is_read_from_the_environment(self):
        """Breaks if `KSB_INDEX_WARN_MB` is misspelt or ignored: a fresh interpreter,
        because the value is read when `config` is first imported."""
        src = Path(__file__).resolve().parent.parent / "src"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from knowledgestore import config; print(config.INDEX_WARN_MB)",
            ],
            env={**os.environ, "KSB_INDEX_WARN_MB": "7", "PYTHONPATH": str(src)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.stdout.strip(), "7", result.stderr)

    def test_the_default_is_90_mb(self):
        """Breaks if the default moves toward GitHub's refusal point, leaving no room
        to decide before the push fails."""
        self.assertEqual(config.INDEX_WARN_MB, 90)


class StatusReportsTheIndex(StageCase):
    def build_from_archive(self):
        """The case `status` can judge: the index recorded the committed `.gz`."""
        self.plain.unlink()
        write_graph(self.archive, NODES, LINKS)
        code, _, _ = run_stage()
        self.assertEqual(code, 0)

    def unreadable(self, path: Path) -> None:
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)

    def test_no_index_says_how_to_build_one(self):
        """Breaks if status is silent about the index, so a store never learns it lacks one."""
        line = index_line(self, status_text(self))
        self.assertIn("none", line)
        self.assertIn("knowledgestore index", line)

    def test_a_fresh_index_reports_its_archive_size_and_that_it_is_current(self):
        """Breaks if a current index is reported stale, or the archive size is dropped."""
        self.build_from_archive()
        line = index_line(self, status_text(self))
        self.assertIn("current with the graph", line)
        self.assertNotIn("stale", line)
        self.assertIn(f"{self.gz.stat().st_size:,} bytes", line)

    def test_a_changed_graph_makes_it_stale(self):
        """Breaks if the comparison reads the wrong file or never compares: a reader
        would open an index describing a graph that is gone."""
        self.build_from_archive()
        write_graph(self.archive, NODES + [node("z9", "One more", file="x.py")], LINKS)
        line = index_line(self, status_text(self))
        self.assertIn("stale", line)
        self.assertIn("knowledgestore index", line)

    def test_a_changed_ticket_layer_makes_it_stale(self):
        """Breaks if only the graph is compared: the tickets table is built from the
        ticket layer, and a store that re-ran `intent` would read current over
        tickets the index does not hold."""
        self.build_from_archive()
        write_tickets(config.INTENT_INDEX_PATH, {**TICKETS, "svc-b": {}})
        self.assertIn("stale", index_line(self, status_text(self)))

    def test_the_file_the_index_names_is_the_one_compared(self):
        """Breaks if status compares a fixed graph path instead of `meta.graph`: an
        index built from a gzip elsewhere in the store is current against that file,
        and stale when that file changes."""
        other = self.root / "knowledge" / "graph.json.gz"
        write_graph(other, NODES, LINKS)
        store_index.build(self.root, other, config.INTENT_INDEX_PATH, self.sqlite)
        self.assertIn("current with the graph", index_line(self, status_text(self)))
        write_graph(other, NODES + [node("z9", "Elsewhere", file="x.py")], LINKS)
        self.assertIn("stale", index_line(self, status_text(self)))

    def test_an_index_built_from_the_archive_ignores_a_plain_graph_that_appears_later(self):
        """Breaks if status follows whichever graph exists now instead of the one the
        index recorded in `meta.graph`."""
        self.build_from_archive()
        write_graph(self.plain, NODES + [node("z9", "Newer", file="x.py")], LINKS)
        self.assertIn("current with the graph", index_line(self, status_text(self)))

    def test_with_both_graphs_present_status_is_current_then_stale_after_the_archive_changes(self):
        """Breaks if the documented sequence (gzip, then index, with graph.json still on
        disk) leaves status unable to judge: it must compare against the archive."""
        write_graph(self.archive, NODES, LINKS)
        self.assertEqual(run_stage()[0], 0)
        self.assertTrue(self.plain.is_file())
        self.assertIn("current with the graph", index_line(self, status_text(self)))
        write_graph(self.archive, NODES + [node("z9", "One more", file="x.py")], LINKS)
        self.assertIn("stale", index_line(self, status_text(self)))

    def test_an_index_built_from_the_plain_graph_is_never_hashed(self):
        """Breaks if status reads the plain graph (CLAUDE.md 6.6): it can be gigabytes.
        The file is made unreadable, so a read would fail rather than answer, and the
        line must still say why freshness was not checked."""
        run_stage()  # no archive exists, so meta.graph is graph.json
        self.unreadable(self.plain)
        line = index_line(self, status_text(self))
        self.assertIn("freshness not checked: built from the plain graph", line)
        self.assertNotIn("stale", line)

    def test_a_plain_file_that_is_not_a_database_is_reported_unreadable(self):
        """Breaks (CLAUDE.md 6.7) if a damaged index makes status raise, or reads as
        current."""
        self.build_from_archive()
        self.sqlite.write_bytes(b"not a database")
        line = index_line(self, status_text(self))
        self.assertIn("could not read its meta", line)
        self.assertNotIn("current with", line)

    def test_an_unreadable_graph_is_not_checked_and_does_not_raise(self):
        """Breaks (CLAUDE.md 6.7) if a permission error while hashing escapes status."""
        self.build_from_archive()
        self.unreadable(self.archive)
        line = index_line(self, status_text(self))
        self.assertIn("freshness not checked", line)
        self.assertNotIn("current with", line)

    def test_a_graph_named_outside_the_store_is_not_checked(self):
        """Breaks if `meta.graph` can point status at any file on the machine: an
        absolute path outside the root is not hashed, even when its bytes match."""
        self.build_from_archive()
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        copy = Path(elsewhere.name) / "graph.json.gz"
        copy.write_bytes(self.archive.read_bytes())
        connection = sqlite3.connect(self.sqlite)
        try:
            connection.execute("UPDATE meta SET value = ? WHERE key = 'graph'", (str(copy),))
            connection.commit()
        finally:
            connection.close()
        line = index_line(self, status_text(self))
        self.assertIn("absent or outside the store", line)
        self.assertNotIn("current with", line)

    def test_only_the_archive_present_is_reported_without_a_freshness_claim(self):
        """Breaks if status claims currency it cannot have checked: `meta` is read from
        the plain file only, so with it absent the comparison is not made."""
        self.build_from_archive()
        self.sqlite.unlink()
        line = index_line(self, status_text(self))
        self.assertNotIn("current with the graph", line)
        self.assertNotIn("stale", line)
        self.assertIn(f"{self.gz.stat().st_size:,} bytes", line)
