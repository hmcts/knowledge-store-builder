"""An estate must be able to declare its boundary, and a store must surface it.

A store answers "there is no evidence of X" when what it can honestly say is
"there is no evidence of X in the repositories I hold". Nothing in its output
distinguished the two, and that has already produced a published finding drawn
honestly from what was indexed and false: a payload schema was reported to have
no readable source because its references did not resolve, when they resolved
against a repository the estate did not hold.

Every test below names the production change that should make it fail. The
recurring shapes are the two this repository keeps meeting: a declaration that
parses and is never rendered (the unwired check), and a declaration that is
rendered while quietly meaning something else (the wrong quantity).

Repository names here are invented. This repository is public.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import boundary, config, status  # noqa: E402
from knowledgestore import build_knowledge_context as ctx  # noqa: E402


DECLARATION = """
# Comments and blank lines are ignored.
searched the `example-org` GitHub organisation
unsearched an internal forge that no build machine can reach

active payments-api
not-used legacy-reporting
decommissioned old-batch-runner

alias payments.api payments-api
snapshot payments-api 2026-01-15
"""


class BoundaryTestCase(SettingsIsolated):
    """A store root per test, so nothing reads the developer's own store."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(_remove, self.tmp)
        config.configure(root=self.tmp, GITHUB_ORG="example-org")
        config.BOUNDARY_PATH.parent.mkdir(parents=True, exist_ok=True)

    def declare(self, text: str) -> None:
        config.BOUNDARY_PATH.write_text(text, encoding="utf-8")

    def status_output(self, recorded: dict) -> str:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            status._report_boundary(recorded)
        return captured.getvalue()


def _remove(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


class ParsingTest(BoundaryTestCase):
    def test_it_reads_every_kind_of_declaration(self):
        """The floor under every other test here: a parse that silently produced an
        empty Boundary would let the rendering tests pass over nothing."""
        self.declare(DECLARATION)
        declared = boundary.read()
        assert declared is not None
        self.assertEqual(declared.searched, ("the `example-org` GitHub organisation",))
        self.assertEqual(
            declared.unsearched, ("an internal forge that no build machine can reach",)
        )
        self.assertEqual(
            declared.rulings,
            {
                "legacy-reporting": "not-used",
                "old-batch-runner": "decommissioned",
                "payments-api": "active",
            },
        )
        self.assertEqual(declared.aliases, {"payments.api": "payments-api"})
        self.assertEqual(declared.snapshots, {"payments-api": "2026-01-15"})

    def test_an_absent_file_is_not_an_error(self):
        """Declaring a boundary is optional. Raising here would make every store
        that has not written one fail its `context` build."""
        self.assertIsNone(boundary.read())

    def test_a_ruling_under_an_off_host_name_lands_on_the_repository_held(self):
        """Break: drop the alias resolution in `_resolve`, so a ruling written under
        the off-host name keys itself under a name the store never holds. `status`
        would then report a held repository as absent - the false absence this whole
        module exists to remove - and the manifest would list it twice."""
        self.declare("alias orders.service orders-service\nactive orders.service\n")
        declared = boundary.read()
        assert declared is not None
        self.assertEqual(declared.rulings, {"orders-service": "active"})
        self.assertEqual(
            boundary.reconciliation(declared, {"orders-service"})["active_absent"],
            [],
            "the store holds this repository under its estate name",
        )

    def test_two_rulings_for_one_repository_are_refused(self):
        """Break: keep the last ruling parsed instead of raising. The manifest would
        then publish a decision nobody made, chosen by line order, and an alias
        pointing two names at one repository is exactly how that happens."""
        with self.assertRaises(ValueError) as raised:
            boundary.parse(
                "alias orders.service orders-service\n"
                "active orders-service\n"
                "decommissioned orders.service\n",
                Path("d.txt"),
            )
        self.assertIn("orders-service", str(raised.exception))
        self.assertIn("one ruling", str(raised.exception))

    def test_an_alias_chain_is_refused(self):
        """Break: accept `a -> b` and `b -> c`. Resolution then depends on which end
        is read first, so the same file gives two different estates."""
        with self.assertRaises(ValueError) as raised:
            boundary.parse("alias a b\nalias b c\n", Path("d.txt"))
        self.assertIn("chain", str(raised.exception))

    def test_an_alias_to_itself_is_refused(self):
        """Break: accept it. It reads as a declaration and resolves nothing, which is
        indistinguishable from a typo in the estate name."""
        with self.assertRaises(ValueError):
            boundary.parse("alias same-name same-name\n", Path("d.txt"))

    def test_a_trailing_comment_cannot_become_a_repository_name(self):
        """Break: take the rest of the line as the value, the way the filter file has
        to. `active payments-api # still live` would then rule a repository called
        `payments-api # still live`: matched by nothing, reported by nothing, and the
        real repository left unruled. The filter file cannot detect this; here a
        repository name has no spaces, so it can."""
        with self.assertRaises(ValueError) as raised:
            boundary.parse("active payments-api # still live\n", Path("d.txt"))
        self.assertIn("comment", str(raised.exception))

    def test_an_unparseable_snapshot_date_is_refused(self):
        """Break: keep the text as written. A date nothing can compare tells a reader
        no more than no date at all, and the point of recording when a hand-taken
        copy was taken is that fresh can be told from frozen."""
        with self.assertRaises(ValueError) as raised:
            boundary.parse("snapshot payments-api last-summer\n", Path("d.txt"))
        self.assertIn("YYYY-MM-DD", str(raised.exception))

    def test_an_unknown_declaration_is_refused(self):
        """Break: skip lines it does not understand. `retired payments-api` would then
        declare nothing and say nothing - the estate believes it has ruled a
        repository out and no artefact anywhere disagrees."""
        with self.assertRaises(ValueError) as raised:
            boundary.parse("retired payments-api\n", Path("d.txt"))
        self.assertIn("unknown declaration", str(raised.exception))

    def test_a_declaration_holding_nothing_is_refused(self):
        """Break: return an empty Boundary for an empty or all-comment file. `status`
        would report a declared boundary, the manifest would render the declared-
        boundary heading, and neither would be describing anything. A check whose
        input is empty reports compliance for something it never read."""
        with self.assertRaises(ValueError) as raised:
            boundary.parse("# nothing decided yet\n\n", Path("d.txt"))
        self.assertIn("no declarations", str(raised.exception))


class ManifestTest(BoundaryTestCase):
    def test_the_declaration_reaches_the_written_manifest(self):
        """Break: drop the `manifest_section` call from `scope_statement`. Everything
        above still passes - the parse works, the rendering works - and the committed
        artefact a reader actually opens says none of it. This library has shipped an
        unwired check twice."""
        self.declare(DECLARATION)
        ctx.build_manifest([])
        written = config.MANIFEST_PATH.read_text(encoding="utf-8")
        self.assertIn("### Declared boundary", written)
        self.assertIn("an internal forge that no build machine can reach", written)
        self.assertIn("| `payments-api` | active |", written)
        self.assertIn("| `old-batch-runner` | decommissioned |", written)
        self.assertIn("`payments.api`", written)
        self.assertIn("2026-01-15 (refreshed by hand)", written)

    def test_the_manifest_says_when_no_boundary_is_declared(self):
        """Break: render nothing when the file is absent. A reader then cannot tell a
        repository that is outside the estate by decision from one nobody has looked
        for, which is the distinction the whole change exists to make."""
        ctx.build_manifest([])
        written = config.MANIFEST_PATH.read_text(encoding="utf-8")
        self.assertIn("No boundary is declared", written)
        self.assertIn("config/estate-boundary.txt", written)

    def test_completeness_is_never_claimed_either_way(self):
        """Break: render the completeness disclaimer only when a declaration exists,
        or drop it entirely. A declaration that reads as "this is all of it" is a new
        false claim replacing the old silent one; an estate with no declaration at
        all needs the sentence more, not less."""
        ctx.build_manifest([])
        undeclared = config.MANIFEST_PATH.read_text(encoding="utf-8")
        self.declare(DECLARATION)
        ctx.build_manifest([])
        declared = config.MANIFEST_PATH.read_text(encoding="utf-8")
        for written in (undeclared, declared):
            self.assertIn("Completeness is not claimed", written)

    def test_a_malformed_declaration_stops_the_manifest_build(self):
        """Break: swallow the parse error here. The manifest is a committed artefact
        that a reader trusts, and a declaration that silently renders as "no boundary
        declared" is worse than a build that stops: the estate believes it has said
        something and the store says the opposite."""
        self.declare("retired payments-api\n")
        with self.assertRaises(ValueError):
            ctx.build_manifest([])


class StatusTest(BoundaryTestCase):
    def test_it_reports_that_no_boundary_is_declared(self):
        """Break: report only when a declaration exists. Silence is the state every
        store starts in, so a check that speaks only for the configured case never
        reaches the stores that need it."""
        output = self.status_output({"one-service": {}, "two-service": {}})
        self.assertIn("Estate boundary: not declared", output)
        self.assertIn("2 repositories", output)

    def test_it_names_a_repository_declared_active_and_not_held(self):
        """Break: render the declaration without reconciling it against provenance.
        A repository the estate calls live and the store does not hold is the exact
        shape of the published false finding, and a declaration nothing checks is a
        second artefact that can be quietly wrong."""
        self.declare(DECLARATION)
        output = self.status_output({"legacy-reporting": {}})
        self.assertIn("declared active and not held", output)
        self.assertIn("payments-api", output)

    def test_it_names_a_repository_held_and_ruled_out(self):
        """Break: reconcile only in the absent direction. A decommissioned repository
        still in the graph is cited as current, which reads as evidence rather than
        as a gap - the harder of the two errors to notice."""
        self.declare(DECLARATION)
        output = self.status_output({"old-batch-runner": {}})
        self.assertIn("ruled not-used or decommissioned", output)
        self.assertIn("old-batch-runner", output)

    def test_it_is_silent_where_the_declaration_and_disk_agree(self):
        """The sensitivity check on the two tests above: a reconciliation that
        reported everything unconditionally would pass both of them while telling an
        operator nothing. Here every ruling matches what is held, so only the summary
        line may appear."""
        self.declare(
            "searched the `example-org` GitHub organisation\n"
            "active payments-api\n"
            "decommissioned old-batch-runner\n"
        )
        output = self.status_output({"payments-api": {}})
        self.assertIn("Estate boundary: 1 source declared searched", output)
        self.assertNotIn("Boundary:", output)

    def test_it_survives_a_malformed_declaration(self):
        """Break: let the parse error out of `_report_boundary`. `status` never
        returns non-zero and must report every other check even when one input is
        broken; a traceback here would take the whole report down."""
        self.declare("retired payments-api\n")
        output = self.status_output({"payments-api": {}})
        self.assertIn("cannot be read", output)
        self.assertIn("config/estate-boundary.txt", output)

    def test_the_report_is_wired_into_the_status_run(self):
        """Break: drop the `_report_boundary(recorded)` call from `status.main`. Every
        other test in this class drives the reporter directly and stays green, so the
        stage would go silent about the boundary with the suite passing - the unwired-
        check class this library has shipped twice. The mutation gate found exactly
        this surviving before this test existed."""
        self.declare(DECLARATION)
        self.addCleanup(setattr, status, "run_git", status.run_git)
        status.run_git = lambda arguments: ""
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(status.main([]), 0, "`status` never returns non-zero")
        self.assertIn("Estate boundary:", captured.getvalue())

    def test_the_summary_line_counts_what_it_says_it_counts(self):
        """Break: count the declaration's lines, or the alias keys, in place of the
        rulings. Every wrong measurement this library has shipped was correct code
        answering a neighbouring question, so the quantities are pinned by hand
        against a declaration counted by eye: three rulings, one alias, one copy."""
        self.declare(DECLARATION)
        declared = boundary.read()
        assert declared is not None
        line = boundary.summary_line(declared)
        self.assertIn("3 repositories ruled (1 active, 1 not-used, 1 decommissioned)", line)
        self.assertIn("1 alias", line)
        self.assertIn("1 hand-taken copy (oldest 2026-01-15)", line)
        self.assertIn("completeness not claimed", line)


class DeterminismTest(BoundaryTestCase):
    def test_the_rendered_section_does_not_move_with_the_hash_seed(self):
        """Break: render `searched`, `unsearched` or the rulings straight from the set
        or dict they were parsed into. Two builds of the same store would then differ
        by line order across processes - invisible until someone diffs them, which is
        how this has broken here before.

        Run in subprocesses on purpose: PYTHONHASHSEED is fixed at interpreter start,
        so a same-process comparison cannot see this defect at all.
        """
        self.declare(
            "searched zebra-source\nsearched alpha-source\n"
            "unsearched zulu-forge\nunsearched alpha-forge\n"
            "active zebra-service\nactive alpha-service\nnot-used middle-service\n"
        )
        renders = {seed: _render_in_subprocess(config.BOUNDARY_PATH, seed) for seed in ("0", "1")}
        self.assertIn("alpha-service", renders["0"], "the render must not be empty")
        self.assertEqual(renders["0"], renders["1"])
        self.assertLess(
            renders["0"].index("alpha-source"),
            renders["0"].index("zebra-source"),
            "sorted, not insertion order",
        )


_RENDER = (
    "import sys, pathlib;"
    "sys.path.insert(0, {src!r});"
    "from knowledgestore import boundary;"
    "p = pathlib.Path(sys.argv[1]);"
    "print(chr(10).join(boundary.manifest_section("
    "boundary.parse(p.read_text(encoding='utf-8'), p))))"
)


def _render_in_subprocess(path: Path, seed: str) -> str:
    src = str(Path(__file__).resolve().parent.parent / "src")
    completed = subprocess.run(
        [sys.executable, "-c", _RENDER.format(src=src), str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
    )
    return completed.stdout


if __name__ == "__main__":
    unittest.main()
