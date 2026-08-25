"""The merge reads a glob; the store declares a manifest; nothing compared them.

Reported by a store operator, measured rather than inferred: a refresh found a
few more per-repository graphs on disk than the store's own
`config/repositories.txt` declared. A repository had been discovered, cloned and
extracted, the refresh aborted, and the configuration change naming it was
discarded — leaving the clone and its graph. `graphify merge-graphs` takes a
shell glob, so that graph merges, and `knowledge/provenance.json` has no entry
to date it. Answers would cite nodes the store neither declares nor can name a
commit for.

**Every test here that matters puts a graph on disk that the declaration omits.**
That is the shape of the defect and the shape of the trap: a check iterating the
declaration cannot see the one input that is not in it, and would report clean.
So the fixtures are built declaration-light on purpose, and the tests assert the
undeclared repository is *named*, not merely counted.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import merge_inputs  # noqa: E402
from knowledgestore import status  # noqa: E402


def build_store(
    root: Path,
    *,
    extracted: list[str],
    declared: list[str] | None,
    recorded: list[str] | None = None,
    archived: list[str] | None = None,
    cloned_only: list[str] | None = None,
) -> None:
    """A store root on disk: clones, per-repository graphs, declaration, provenance.

    Real files rather than stubs, because the thing under test is a filesystem
    walk. `declared=None` writes no `config/repositories.txt` at all, which is
    the state a store is in before its first `discover`.
    """
    for name in extracted:
        out = root / "repositories" / name / "graphify-out"
        out.mkdir(parents=True)
        (out / "graph.json").write_text('{"nodes": [], "links": []}', encoding="utf-8")
    for name in archived or []:
        out = root / "repositories" / name / "graphify-out"
        out.mkdir(parents=True)
        (out / "graph.json.gz").write_bytes(b"\x1f\x8b")
    for name in cloned_only or []:
        (root / "repositories" / name).mkdir(parents=True)
    if declared is not None:
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "repositories.txt").write_text(
            "".join(f"{n}|git@example.com:example/{n}.git|main\n" for n in declared),
            encoding="utf-8",
        )
    names = extracted if recorded is None else recorded
    (root / "knowledge").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "provenance.json").write_text(
        json.dumps({"repositories": {n: {"sha": "0" * 40} for n in names}}),
        encoding="utf-8",
    )


class StoreFixture(SettingsIsolated):
    def store(self, **kwargs) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        build_store(root, **kwargs)
        config.configure(root)
        # configure() resolves the root; on a platform where the temporary
        # directory is reached through a symlink the two spellings differ.
        return config.ROOT


class DiscoveryWalksTheGlob(StoreFixture):
    """The walk must be the merge's, not the declaration's.

    Break it catches: re-deriving the input list from `config/repositories.txt`
    — the single change that makes this whole module blind to the defect it
    exists for, and which would leave every other test here passing.
    """

    def test_a_graph_no_declaration_names_is_still_found(self):
        self.store(extracted=["orchard-api", "orchard-web"], declared=["orchard-api"])
        found, _ = merge_inputs.discovered()
        self.assertEqual(
            [merge_inputs.repository_of(p) for p in found],
            ["orchard-api", "orchard-web"],
        )

    def test_it_finds_nothing_when_no_repository_has_been_extracted(self):
        """A glob matching nothing must be visible, not read as a clean walk."""
        self.store(extracted=[], declared=["orchard-api"], cloned_only=["orchard-api"])
        self.assertEqual(merge_inputs.discovered(), ([], []))

    def test_an_absent_repositories_directory_is_not_a_crash(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config.configure(Path(tmp.name))
        self.assertEqual(merge_inputs.discovered(), ([], []))

    def test_inputs_come_back_sorted_whatever_order_the_filesystem_yields(self):
        """Break it catches: dropping the sort. `iterdir()` is in directory order,
        so two runs over the same tree could name the inputs differently and the
        merge command built from them would not be byte-identical."""
        self.store(
            extracted=["zephyr", "alpha", "meadow"],
            declared=["zephyr", "alpha", "meadow"],
        )
        found, _ = merge_inputs.discovered()
        self.assertEqual(
            [merge_inputs.repository_of(p) for p in found], ["alpha", "meadow", "zephyr"]
        )

    def test_a_repository_holding_only_the_archive_is_named_not_merged(self):
        """Break it catches: counting `graph.json.gz` as a merge input. The
        documented merge glob names `graph.json`, so the archive will not be
        read - and a repository that was extracted and is silently omitted is
        the mirror of the defect this module reports."""
        self.store(
            extracted=["orchard-api"],
            archived=["tundra-infra"],
            declared=["orchard-api", "tundra-infra"],
        )
        found, compressed_only = merge_inputs.discovered()
        self.assertEqual([merge_inputs.repository_of(p) for p in found], ["orchard-api"])
        self.assertEqual(compressed_only, ["tundra-infra"])

    def test_a_loose_file_under_repositories_is_not_a_repository(self):
        root = self.store(extracted=["orchard-api"], declared=["orchard-api"])
        (root / "repositories" / "NOTES.md").write_text("x", encoding="utf-8")
        found, _ = merge_inputs.discovered()
        self.assertEqual([merge_inputs.repository_of(p) for p in found], ["orchard-api"])


class ReconciliationNamesEachDivergence(StoreFixture):
    def test_an_undeclared_input_is_named(self):
        """The reported defect. Break it catches: computing `undeclared` from the
        declaration's side, which can only ever be empty."""
        self.store(
            extracted=["orchard-api", "orchard-web"],
            declared=["orchard-api"],
            recorded=["orchard-api", "orchard-web"],
        )
        report = merge_inputs.reconcile()
        self.assertEqual(report.undeclared, ("orchard-web",))
        self.assertFalse(report.closed)

    def test_an_input_provenance_cannot_date_is_named(self):
        """The closure statement: the merge reads it, provenance has no entry, so
        no answer citing it can name the commit it was read at."""
        self.store(
            extracted=["orchard-api", "orchard-web"],
            declared=["orchard-api", "orchard-web"],
            recorded=["orchard-api"],
        )
        report = merge_inputs.reconcile()
        self.assertEqual(report.ungrounded, ("orchard-web",))
        self.assertEqual(report.undeclared, ())
        self.assertFalse(report.closed)

    def test_a_declared_repository_with_no_graph_is_named(self):
        """The documented removal direction seen from the merge's side: the merge
        will omit it, and nothing else says so."""
        self.store(
            extracted=["orchard-api"],
            declared=["orchard-api", "tundra-infra"],
            recorded=["orchard-api", "tundra-infra"],
        )
        report = merge_inputs.reconcile()
        self.assertEqual(report.missing, ("tundra-infra",))
        self.assertEqual(report.undeclared, ())

    def test_a_store_whose_inputs_are_all_declared_and_dated_is_closed(self):
        self.store(
            extracted=["alpha", "meadow"],
            declared=["alpha", "meadow"],
            recorded=["alpha", "meadow"],
        )
        report = merge_inputs.reconcile()
        self.assertTrue(report.closed)
        self.assertEqual((report.undeclared, report.ungrounded, report.missing), ((), (), ()))

    def test_an_empty_input_set_is_not_closed(self):
        """Break it catches: `closed` defined as "no divergences found". A store
        with nothing extracted has no divergences and is not closed over
        anything - reporting it as closed is the vacuous pass this exists to
        stop."""
        self.store(extracted=[], declared=["orchard-api"])
        self.assertFalse(merge_inputs.reconcile().closed)

    def test_an_unwritten_declaration_is_reported_as_unreadable_not_as_empty(self):
        """Break it catches: treating a missing `config/repositories.txt` as
        "nothing declared", which would name every repository in the estate as
        undeclared and bury the real finding."""
        self.store(extracted=["orchard-api"], declared=None)
        report = merge_inputs.reconcile()
        self.assertIsNone(report.declared)
        self.assertEqual(report.undeclared, ())
        self.assertEqual(report.missing, ())
        self.assertFalse(report.closed)

    def test_a_malformed_declaration_is_reported_as_unreadable(self):
        root = self.store(extracted=["orchard-api"], declared=["orchard-api"])
        (root / "config" / "repositories.txt").write_text("orchard-api|main\n", encoding="utf-8")
        self.assertIsNone(merge_inputs.reconcile().declared)

    def test_provenance_recording_more_than_is_on_disk_is_not_a_divergence(self):
        """A repository removed from the estate leaves its provenance entry
        behind. That is a different condition and not this one's business;
        flagging it would make every pruned estate read as broken."""
        self.store(
            extracted=["orchard-api"],
            declared=["orchard-api"],
            recorded=["orchard-api", "retired-service"],
        )
        report = merge_inputs.reconcile()
        self.assertEqual(report.ungrounded, ())
        self.assertTrue(report.closed)


class TheReportNamesRatherThanCounts(StoreFixture):
    def test_the_undeclared_repository_appears_in_the_text(self):
        """Break it catches: a line reporting "1 undeclared input" with no name.
        An operator cannot act on a count."""
        self.store(
            extracted=["orchard-api", "orchard-web"],
            declared=["orchard-api"],
            recorded=["orchard-api", "orchard-web"],
        )
        text = "\n".join(merge_inputs.lines(merge_inputs.reconcile()))
        self.assertIn("Undeclared merge input", text)
        self.assertIn("orchard-web", text)

    def test_a_clean_store_says_every_input_is_declared_and_dated(self):
        self.store(extracted=["alpha"], declared=["alpha"], recorded=["alpha"])
        text = "\n".join(merge_inputs.lines(merge_inputs.reconcile()))
        self.assertIn("every input is declared and dated", text)

    def test_no_inputs_does_not_read_as_a_clean_result(self):
        """The anti-vacuity assertion. Break it catches: falling through to the
        summary line, which over an empty glob would print "0 graph(s) ... every
        input is declared and dated" - a green report on nothing."""
        self.store(extracted=[], declared=["orchard-api"])
        text = "\n".join(merge_inputs.lines(merge_inputs.reconcile()))
        self.assertIn("none found", text)
        self.assertIn("not a clean result", text)
        self.assertNotIn("every input is declared and dated", text)

    def test_an_unreadable_declaration_does_not_read_as_a_clean_result(self):
        self.store(extracted=["orchard-api"], declared=None)
        text = "\n".join(merge_inputs.lines(merge_inputs.reconcile()))
        self.assertIn("could not be read", text)
        self.assertNotIn("every input is declared and dated", text)

    def test_all_four_divergences_are_reported_together(self):
        """Break it catches: an `elif` chain that reports only the first
        divergence. A mid-refresh tree routinely holds more than one."""
        self.store(
            extracted=["orchard-api", "orchard-web"],
            archived=["orchard-docs"],
            declared=["orchard-api", "tundra-infra", "orchard-docs"],
            recorded=["orchard-api", "tundra-infra", "orchard-docs"],
        )
        text = "\n".join(merge_inputs.lines(merge_inputs.reconcile()))
        for expected in (
            "Undeclared merge input",
            "Ungrounded merge input",
            "Declared but not extracted",
            "Extracted but not merged",
        ):
            self.assertIn(expected, text)
        for name in ("orchard-web", "tundra-infra", "orchard-docs"):
            self.assertIn(name, text)

    def test_the_stage_names_every_divergence_and_the_dashboard_caps_them(self):
        """Break it catches: one shared cap. `status` is a dashboard and a
        hundred names in it is unreadable; the stage's whole job is naming them,
        and a cap there would reintroduce the count this replaces."""
        many = [f"repo-{index:02d}" for index in range(8)]
        self.store(extracted=["alpha", *many], declared=["alpha"], recorded=["alpha", *many])
        report = merge_inputs.reconcile()
        uncapped = "\n".join(merge_inputs.lines(report))
        capped = "\n".join(merge_inputs.lines(report, limit=5))
        for name in many:
            self.assertIn(name, uncapped)
        self.assertIn("and 3 more", capped)
        self.assertNotIn("repo-07", capped)


class TheStageIsWired(StoreFixture):
    """Drive `main()`, not the helpers.

    A reconciliation nothing calls is the escape this library keeps meeting:
    the behaviour was tested and the call site was not.
    """

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = _io.StringIO(), _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = merge_inputs.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_it_reports_an_undeclared_input_and_still_exits_zero(self):
        """Report rather than refuse by default: a tree caught mid-refresh is a
        normal state, and a stage failing on it would fail on the normal case."""
        self.store(
            extracted=["orchard-api", "orchard-web"],
            declared=["orchard-api"],
            recorded=["orchard-api", "orchard-web"],
        )
        code, out, _ = self._run([])
        self.assertEqual(code, 0)
        self.assertIn("orchard-web", out)

    def test_strict_fails_on_an_undeclared_input(self):
        """The gate a store can put in CI. Break it catches: `--strict` accepted
        and ignored, which reads as a passing gate."""
        self.store(
            extracted=["orchard-api", "orchard-web"],
            declared=["orchard-api"],
            recorded=["orchard-api", "orchard-web"],
        )
        self.assertEqual(self._run(["--strict"])[0], 1)

    def test_strict_fails_on_an_input_provenance_cannot_date(self):
        self.store(
            extracted=["orchard-api", "orchard-web"],
            declared=["orchard-api", "orchard-web"],
            recorded=["orchard-api"],
        )
        self.assertEqual(self._run(["--strict"])[0], 1)

    def test_strict_passes_a_closed_store(self):
        self.store(extracted=["alpha"], declared=["alpha"], recorded=["alpha"])
        self.assertEqual(self._run(["--strict"])[0], 0)

    def test_strict_ignores_a_repository_the_merge_will_only_omit(self):
        """Break it catches: folding `missing` into `closed`. Every store between
        `discover` and the end of extraction has declared repositories with no
        graph yet, so a gate failing on that fails continuously and gets
        removed."""
        self.store(
            extracted=["alpha"],
            declared=["alpha", "not-yet-extracted"],
            recorded=["alpha", "not-yet-extracted"],
        )
        self.assertEqual(self._run(["--strict"])[0], 0)

    def test_an_empty_glob_fails_without_strict(self):
        """Anti-vacuity, at the exit code. Break it catches: exiting 0 over a
        store with nothing extracted, which would let a CI gate pass on a build
        that produced no graphs at all."""
        self.store(extracted=[], declared=["orchard-api"])
        code, out, _ = self._run([])
        self.assertEqual(code, 1)
        self.assertIn("none found", out)

    def test_an_unreadable_declaration_fails_without_strict(self):
        self.store(extracted=["orchard-api"], declared=None)
        self.assertEqual(self._run([])[0], 1)

    def test_paths_writes_the_inputs_to_stdout_and_the_report_to_stderr(self):
        """The route that closes the loop: name every input to the merge instead
        of handing it a glob. Break it catches: the report printed to stdout,
        which would corrupt the argument list the operator pipes into
        `merge-graphs`."""
        root = self.store(
            extracted=["orchard-api", "orchard-web"],
            declared=["orchard-api"],
            recorded=["orchard-api", "orchard-web"],
        )
        code, out, err = self._run(["--paths"])
        self.assertEqual(code, 0)
        self.assertEqual(
            out.split(),
            [
                str(root / "repositories" / "orchard-api" / "graphify-out" / "graph.json"),
                str(root / "repositories" / "orchard-web" / "graphify-out" / "graph.json"),
            ],
        )
        self.assertIn("Undeclared merge input", err)
        self.assertNotIn("Undeclared", out)


class StatusReportsItAndStillSucceeds(StoreFixture):
    """`status` must say so, and must never return non-zero.

    Break it catches: the reconciliation existing as a stage nobody runs. The
    operator who found this was reading `status`, which reported an entirely
    healthy store over a graph it could not account for.
    """

    def _status(self) -> tuple[int, str]:
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()):
            code = status.main([])
        return code, out.getvalue()

    def test_status_names_the_undeclared_input(self):
        self.store(
            extracted=["orchard-api", "orchard-web"],
            declared=["orchard-api"],
            recorded=["orchard-api", "orchard-web"],
        )
        code, out = self._status()
        self.assertEqual(code, 0)
        self.assertIn("Undeclared merge input", out)
        self.assertIn("orchard-web", out)

    def test_status_still_returns_zero_when_the_store_is_wide_open(self):
        """Drift and gaps are normal operating conditions; the stage reports and
        humans decide. Break it catches: propagating the stage's exit code."""
        self.store(extracted=["orchard-api"], declared=None)
        self.assertEqual(self._status()[0], 0)

    def test_status_caps_the_names_it_prints(self):
        many = [f"repo-{index:02d}" for index in range(8)]
        self.store(extracted=["alpha", *many], declared=["alpha"], recorded=["alpha", *many])
        _, out = self._status()
        self.assertIn("and 3 more", out)


class TheStageIsReachableFromTheCli(SettingsIsolated):
    def test_merge_inputs_is_a_stage_that_parses_its_own_arguments(self):
        """Break it catches: a module with no CLI entry. `--help` would fall
        through to the generic text and the flags would be undiscoverable."""
        from knowledgestore import cli

        self.assertEqual(cli.STAGES["merge-inputs"][0], "merge_inputs")
        self.assertIn("merge-inputs", cli.SELF_PARSING)


if __name__ == "__main__":
    unittest.main()
