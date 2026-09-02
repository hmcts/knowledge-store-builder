"""The record a build compares itself against, and the stages that write it.

Issue #154: `status` and the build stages printed join cardinality, layer sizes
and node counts and then threw them away, so nothing could notice a number
moving. Every silent failure found across two estates was a plausible number in
isolation and an implausible one beside its predecessor.

Two halves are tested here and they fail for different reasons. The mechanism
tests own the comparison itself - reading before overwriting, naming the previous
value, refusing a rate. The wiring tests run the real stages and assert on the
artefact they leave behind, because a mechanism nothing calls is this
repository's most repeated escape: three of its mutation-gate entries are
behaviour that was tested through its function while nothing drove it.
"""

from __future__ import annotations

import contextlib
import gzip
import io as stdio
import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config, io, telemetry


def _capture(measurements: dict) -> tuple[list, str, str]:
    """Record, returning the movements and everything printed on both streams."""
    out, err = stdio.StringIO(), stdio.StringIO()
    moved = telemetry.record(measurements, out=out, err=err)
    return moved, out.getvalue(), err.getvalue()


class TelemetryRecordTest(SettingsIsolated):
    """The comparison mechanism, over a real file in a temporary directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        config.configure(TELEMETRY_PATH=Path(self._tmp.name) / "telemetry.json")

    def test_the_second_record_reports_what_moved_since_the_first(self):
        """Breaks if a build overwrites the record without reading it first.

        This is the defect the issue names: every number existed only in one
        build's scrollback, so a build could not compare itself with the last
        one. Overwrite-then-report would pass a test that only checked the file's
        contents, so the assertion is on the movement.
        """
        _capture({"explorer.rows_with_tickets": 5568})

        moved, out, _ = _capture({"explorer.rows_with_tickets": 1200})

        self.assertEqual(
            [(m.metric, m.previous, m.current) for m in moved],
            [("explorer.rows_with_tickets", 5568, 1200)],
        )
        self.assertIn("5,568 -> 1,200", out)
        self.assertIn("-78.4%", out)

    def test_a_first_record_is_not_reported_as_growth_from_zero(self):
        """Breaks if an absent predecessor is read as 0.

        An absent measurement reading as a clean result is a defect this
        repository has shipped twice; here it would make every first build look
        like a movement, and a report that cries wolf on a new store is one
        nobody reads by the third refresh.
        """
        moved, out, err = _capture({"layers.ast_nodes": 19353})

        self.assertTrue(moved[0].first)
        self.assertIsNone(moved[0].previous)
        self.assertIn("first recorded", out)
        self.assertIn("nothing to compare against yet", out)
        self.assertEqual(err, "")

    def test_a_measurement_that_fell_to_zero_is_a_warning_not_a_statistic(self):
        """Breaks if a collapsed population is printed among the healthy numbers.

        A join that matched nothing was green on one store across 70,655 nodes.
        Zero is the one condition that needs no knowledge of the estate, and it
        goes to stderr for the same reason `report_join_cardinality` sends it
        there: a defect and a measurement are different kinds of output.
        """
        _capture({"explorer.rows_with_tickets": 5568})

        _, out, err = _capture({"explorer.rows_with_tickets": 0})

        self.assertIn("fell to zero", err)
        self.assertIn("5,568 -> 0", err)
        self.assertNotIn("fell to zero", out)

    def test_a_measurement_that_merely_fell_stays_a_statistic(self):
        """Breaks if the warning fires on any decrease.

        The discriminating half of the test above: a gate that fires on every
        intentional change gets suppressed, and an intentional content cut is
        *supposed* to shrink these numbers. Without this, routing everything to
        stderr would pass.
        """
        _capture({"layers.ast_nodes": 544171})

        _, out, err = _capture({"layers.ast_nodes": 19353})

        self.assertEqual(err, "")
        self.assertIn("544,171 -> 19,353", out)
        self.assertIn("-96.4%", out)

    def test_a_metric_that_was_zero_and_is_not_reads_as_recovery(self):
        """Breaks if `0 -> 12,732` divides by zero or reports an infinite rise.

        The half-fixed join is the real case: a store whose join was dead
        recovers to a real number, and the arithmetic for a percentage change has
        no denominator.
        """
        _capture({"explorer.rows_with_tickets": 0})

        _, out, err = _capture({"explorer.rows_with_tickets": 12732})

        self.assertIn("0 -> 12,732 (was zero)", out)
        self.assertEqual(err, "")

    def test_a_rate_cannot_be_recorded(self):
        """Breaks if a percentage may be stored instead of its two counts.

        A stored `17.6` cannot be re-derived, so a later build cannot tell a
        shrinking numerator from a growing denominator - correct code answering a
        neighbouring question, which is the shape of every wrong measurement this
        codebase has shipped.
        """
        with self.assertRaises(ValueError) as caught:
            telemetry.record({"explorer.join_rate": 17.6})

        self.assertIn("numerator", str(caught.exception))
        self.assertFalse(config.TELEMETRY_PATH.exists(), "refused after writing is not refused")

    def test_an_unqualified_metric_name_is_refused(self):
        """Breaks if two stages can collide on one key.

        Stages record into one shared document, so an unqualified `nodes` from
        the explorer and from the layer merge is a single metric, and each build
        would compare one stage's count against another's.
        """
        with self.assertRaises(ValueError) as caught:
            telemetry.record({"nodes": 10})

        self.assertIn("stage.measurement", str(caught.exception))

    def test_another_stage_s_measurements_survive_this_stage_s_record(self):
        """Breaks if a stage's record replaces the document instead of merging.

        Four stages write this file in one refresh. A replace would leave each
        stage's record surviving only until the next stage ran, so every
        comparison would be against nothing - a mechanism that reads green and
        measures nothing.
        """
        _capture({"layers.ast_nodes": 19353})

        _capture({"explorer.rows_indexed": 28093})

        self.assertEqual(
            telemetry.read(),
            {"layers.ast_nodes": 19353, "explorer.rows_indexed": 28093},
        )

    def test_two_records_of_the_same_measurements_are_byte_identical(self):
        """Breaks if the committed artefact churns, or diffs unreadably.

        It is committed so the diff is the review surface, and a file whose key
        order follows a caller's dict or a set iteration would diff on every
        refresh. Recorded in opposite insertion orders on purpose.
        """
        _capture({"layers.ast_nodes": 1, "layers.semantic_nodes": 2})
        first = config.TELEMETRY_PATH.read_bytes()

        _capture({"layers.semantic_nodes": 2, "layers.ast_nodes": 1})

        self.assertEqual(config.TELEMETRY_PATH.read_bytes(), first)
        self.assertNotIn(b"20", first, "no wall-clock timestamp belongs in a committed record")

    def test_a_hand_edited_non_integer_is_not_trusted(self):
        """Breaks if a string where a count belongs reaches the arithmetic.

        The artefact is committed, so it gets hand-edited and merge-resolved.
        A `"12,732"` must read as no predecessor rather than crash the stage that
        was only trying to report.
        """
        io.write_json(
            config.TELEMETRY_PATH,
            {"measurements": {"explorer.rows_indexed": "12,732", "layers.ast_nodes": 7}},
        )

        self.assertEqual(telemetry.read(), {"layers.ast_nodes": 7})
        moved, _, _ = _capture({"explorer.rows_indexed": 100})
        self.assertTrue(moved[0].first)

    def test_nothing_recorded_reports_nothing_rather_than_a_heading(self):
        """Breaks if `status` can print a Telemetry heading over an empty record.

        "0 checked, none dangling" already taught this repository that a
        measurement of nothing paired with a clean presentation reads as a pass.
        """
        self.assertEqual(telemetry.recorded_lines(), [])
        self.assertEqual(telemetry.read(), {})


class LayerMergeWiringTest(SettingsIsolated):
    """`merge-layers` records the two layer sizes issue #116 turns on."""

    def _layer(self, nodes, edges=()):
        return {"nodes": list(nodes), "edges": list(edges)}

    def _run(self, ast_nodes: int, semantic_nodes: int) -> dict:
        from knowledgestore import merge_layers

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config.configure(TELEMETRY_PATH=directory / "telemetry.json")
            (directory / "ast.json").write_text(
                json.dumps(
                    self._layer(
                        [
                            {"id": f"a{i}", "label": f"Ast{i}", "source_file": f"f{i}.tf"}
                            for i in range(ast_nodes)
                        ]
                    )
                ),
                encoding="utf-8",
            )
            (directory / "sem.json").write_text(
                json.dumps(
                    self._layer(
                        [
                            {"id": f"s{i}", "label": f"Concept{i}", "source_file": f"d{i}.md"}
                            for i in range(semantic_nodes)
                        ]
                    )
                ),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(stdio.StringIO()):
                code = merge_layers.main(
                    [
                        "--ast",
                        str(directory / "ast.json"),
                        "--semantic",
                        str(directory / "sem.json"),
                        "--out",
                        str(directory / "out.json"),
                    ]
                )
            self.assertEqual(code, 0)
            return telemetry.read()

    def test_the_stage_records_both_layer_sizes(self):
        """Breaks if the AST and semantic counts are printed and discarded.

        #116 has to be decided on the ratio between these two, and two estates
        measured that ratio a hundredfold apart - so no shipped constant means
        anything and the only usable comparison is against this store's own last
        build. That comparison needs both counts recorded, which is why this
        asserts on the record rather than on the printed report.
        """
        recorded = self._run(ast_nodes=8, semantic_nodes=2)

        self.assertEqual(recorded["layers.ast_nodes"], 8)
        self.assertEqual(recorded["layers.semantic_nodes"], 2)
        self.assertEqual(recorded["layers.merged_nodes"], 10)

    def test_the_recorded_counts_recover_the_previous_ratio(self):
        """Breaks if a ratio is recorded in place of its two counts.

        The check the issue's design turns on: with both builds' numerators and
        denominators the previous ratio is recoverable, so "the AST layer went
        from 4:1 to 40:1 of the semantic layer" is answerable. A recorded ratio
        would leave a reader unable to tell a doubled AST layer from a halved
        semantic one.
        """
        first = self._run(ast_nodes=8, semantic_nodes=2)

        self.assertEqual(first["layers.ast_nodes"] / first["layers.semantic_nodes"], 4.0)


class ExplorerWiringTest(SettingsIsolated):
    """`explorer` records the file-to-ticket join at the surface readers meet."""

    INDEX = {"repo-a": {f"f{i}.py": {"tickets": {"T-1": 1}} for i in range(6)}}

    def _graph(self, prefix: str) -> dict:
        nodes = [
            {
                "id": f"n{i}",
                "label": f"ServiceComponent{i}",
                "repo": "repo-a",
                "source_file": f"{prefix}f{i}.py",
            }
            for i in range(6)
        ]
        links = [
            {"source": f"n{i}", "target": f"n{j}"} for i in range(6) for j in range(6) if i != j
        ]
        return {"nodes": nodes, "links": links}

    def _run(self, directory: Path, prefix: str) -> str:
        """Build the real page from a graph whose join works, or does not."""
        from knowledgestore import build_explorer

        # A whole store root rather than individual paths: the page records
        # digests for the layers `status` re-hashes, and those are resolved
        # against ROOT. Every embedded layer is simply absent here.
        config.configure(root=directory)
        io.write_json(config.GRAPH_PATH, self._graph(prefix))
        io.write_json(config.LABELS_PATH, {})
        io.write_gzip_json(config.INTENT_INDEX_PATH, self.INDEX)
        err = stdio.StringIO()
        with contextlib.redirect_stdout(stdio.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(build_explorer.main(), 0)
        return err.getvalue()

    def test_the_page_records_the_join_it_shipped(self):
        """Breaks if the join cardinality is printed and discarded.

        `report_join_cardinality` refuses only a join that matched nothing, and
        said so deliberately: a non-zero floor is a guess about estate shape.
        Recording the two counts is what makes the half-dead case - one estate's
        AST half fixed and semantic half skipping every record - visible without
        anyone guessing what "enough" is.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._run(Path(tmp), "")
            recorded = telemetry.read()

        self.assertEqual(recorded["explorer.rows_indexed"], 6)
        self.assertEqual(recorded["explorer.rows_with_tickets"], 6)
        self.assertEqual(recorded["explorer.graph_nodes"], 6)

    def test_the_recorded_join_counts_the_column_the_page_ships(self):
        """Breaks if the recorded count and the page's own ticket column diverge.

        Two expressions for one quantity is how two numbers describing the same
        thing start disagreeing, and a recorded number nobody can reconcile with
        the artefact is worse than none. Re-derived here from the built page's
        embedded rows rather than from the code that recorded it.
        """
        from knowledgestore import build_explorer

        with tempfile.TemporaryDirectory() as tmp:
            self._run(Path(tmp), "")
            page = config.EXPLORER_PATH.read_text(encoding="utf-8")
            recorded = telemetry.read()

        block = page.split('<script id="data" type="application/json">')[1].split("</script>")[0]
        # Decoded first. The page interns a column wherever the table plus the
        # indices cost fewer bytes than the values (#245), so the embedded rows
        # are not the rows the stage counted - and the ticket column of an
        # encoded row is a count of something else. Through the shipped decoder,
        # because a second one here would be a second chance to be wrong.
        dicts = page.split('<script id="dicts" type="application/json">')[1].split("</script>")[0]
        rows = build_explorer.decode_rows(json.loads(block), json.loads(dicts))
        self.assertEqual(recorded["explorer.rows_indexed"], len(rows))
        self.assertEqual(recorded["explorer.rows_with_tickets"], sum(1 for r in rows if r[7]))

    def test_a_join_that_died_between_builds_is_warned_about(self):
        """Breaks if a join that worked last build and matches nothing now is
        reported as an ordinary statistic.

        The end-to-end case the issue is about, through the real stage twice: the
        same six nodes and the same index, differing only in the `repositories/`
        prefix that puts the two sides in different key spaces. The build stays
        green either way, which is exactly what happened on a real store.
        """
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._run(directory, "")
            err = self._run(directory, "repositories/repo-a/")
            recorded = telemetry.read()

        self.assertEqual(recorded["explorer.rows_with_tickets"], 0)
        self.assertIn("explorer.rows_with_tickets fell to zero", err)
        self.assertIn("6 -> 0", err)


class IntentWiringTest(SettingsIsolated):
    """`intent` records the inventory whose collapse is otherwise invisible."""

    def _run(self, directory: Path, files: list[str]) -> dict:
        from knowledgestore import build_intent_index

        history = directory / "history" / "repo-a"
        history.mkdir(parents=True, exist_ok=True)
        commits = [
            {
                "repository": "repo-a",
                "subject": "PROJ-1: change the thing",
                "is_merge": False,
                "author_date": "2024-05-01T10:00:00+00:00",
                "body": "",
                "author": {"name": "A Person", "email": "a.person@example.example"},
                "committer": {"name": "A Person", "email": "a.person@example.example"},
                "files": [{"path": path} for path in files],
            }
        ]
        (history / "commits.ndjson").write_text(
            "\n".join(json.dumps(c) for c in commits) + "\n", encoding="utf-8"
        )
        config.configure(
            HISTORY_DIR=directory / "history",
            INTENT_INDEX_PATH=directory / "file-tickets.json.gz",
            TICKET_DESCRIPTIONS_PATH=directory / "ticket-descriptions.json.gz",
            TELEMETRY_PATH=directory / "telemetry.json",
        )
        with contextlib.redirect_stdout(stdio.StringIO()):
            self.assertEqual(build_intent_index.main(), 0)
        return telemetry.read()

    def test_the_stage_records_the_inventory_it_indexed(self):
        """Breaks if the indexed file and ticket counts are printed and discarded.

        A corpus inventory that collapsed - every repository's readme reducing to
        one entry - reported a plausible smaller number and read as a smaller
        estate. Nothing but its predecessor contradicts it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            recorded = self._run(directory, ["a.py", "b.py", "c.py"])
            index = json.load(gzip.open(config.INTENT_INDEX_PATH, "rt", encoding="utf-8"))

        self.assertEqual(recorded["intent.files_indexed"], 3)
        self.assertEqual(recorded["intent.files_indexed"], len(index["repo-a"]))
        self.assertEqual(recorded["intent.repositories_mined"], 1)
        self.assertEqual(recorded["intent.tickets_distinct"], 1)

    def test_a_collapsed_inventory_is_warned_about_on_the_next_build(self):
        """Breaks if an inventory that lost its files reads as a thinner estate.

        Two real runs of the real stage over the same repository, the second
        having lost every ticketed file. The stage exits 0 both times.
        """
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._run(directory, ["a.py", "b.py", "c.py"])
            recorded = telemetry.read()
            self.assertEqual(recorded["intent.files_indexed"], 3)

            out, err = stdio.StringIO(), stdio.StringIO()
            moved = telemetry.record({"intent.files_indexed": 0}, out=out, err=err)

        self.assertTrue(moved[0].collapsed)
        self.assertIn("intent.files_indexed fell to zero", err.getvalue())


class StatusReportsTheRecordTest(SettingsIsolated):
    """`status` surfaces the record without claiming a comparison."""

    def test_status_prints_every_recorded_measurement(self):
        """Breaks if the record exists and no stage shows it to an operator.

        Reporting through a function while nothing drives the CLI is the most
        repeated escape in this repository - `status` alone accounts for three
        entries in its mutation gate.
        """
        from knowledgestore import status

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config.configure(root=root)
            io.write_json(
                config.TELEMETRY_PATH,
                {"measurements": {"explorer.rows_with_tickets": 5568, "layers.ast_nodes": 19353}},
            )
            out = stdio.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(stdio.StringIO()):
                code = status.main([])
            text = out.getvalue()

        self.assertEqual(code, 0, "status never returns non-zero")
        self.assertIn("explorer.rows_with_tickets", text)
        self.assertIn("5,568", text)
        self.assertIn("layers.ast_nodes", text)

    def test_status_says_when_nothing_has_been_recorded(self):
        """Breaks if a heading is printed over an empty record.

        A measurement of nothing beside a clean presentation reads as a pass;
        this is the same correction "Corpus citations: none checked" already
        carries.
        """
        from knowledgestore import status

        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=Path(tmp))
            out = stdio.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(stdio.StringIO()):
                status.main([])
            text = out.getvalue()

        self.assertIn("Telemetry: nothing recorded yet", text)


if __name__ == "__main__":
    unittest.main()
