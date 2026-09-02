"""The version has one source of truth: the installed distribution metadata.

The distribution version is derived from the git tag at build time
(hatch-vcs), so a hand-maintained copy in source is the defect this guards
against — two of the three copies had already drifted when this test was
written.
"""

from importlib.metadata import version

import contextlib
import io
import sys
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import cli


class VersionTest(SettingsIsolated):
    def test_dunder_version_is_the_distribution_version(self):
        import knowledgestore

        self.assertEqual(knowledgestore.__version__, version("hmcts-knowledge-store-builder"))

    def test_the_version_is_askable_from_the_command_line(self):
        """It was not, and that is what made version drift undiagnosable.

        The skills install through the plugin cache and the library through pip, so a
        user can hold instructions their install cannot execute. The first question then
        is which version is actually installed - and until this flag there was no way to
        ask short of `python -c 'import importlib.metadata'`.
        """
        for flag in ("--version", "-V"):
            with self.subTest(flag=flag):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = cli.main([flag])
                self.assertEqual(code, 0)
                self.assertIn(version("hmcts-knowledge-store-builder"), out.getvalue())

    def test_the_first_line_is_the_version_alone_and_unchanged(self):
        """Break it catches: reformatting the line consumers already parse.

        A store's gate reads this output. The interpreter and package lines were
        added after stores were already reading the first one, so the first line
        is a compatibility surface: it must stay `knowledgestore <version>` and
        nothing else, on line one.
        """
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--version"])
        first = out.getvalue().splitlines()[0]
        self.assertEqual(first, f"knowledgestore {version('hmcts-knowledge-store-builder')}")

    def test_the_output_names_the_interpreter_that_answered(self):
        """Break it catches: a version claim that cannot say whose version it is.

        A store's pre-commit hook asserted "the installed library matches the
        lock" while running under the machine `python3`, and the virtualenv that
        builds the store held a different version. It certified an environment
        that does not build the store.

        No check can fix that - a check is code some interpreter runs, so run
        under the wrong one it reports faithfully about the wrong one. What is
        available is legibility, and this is the assertion that keeps it: the
        output names the interpreter and the package directory the running code
        was actually loaded from, so a gate capturing it holds evidence of what
        it certified.
        """
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--version"])
        printed = out.getvalue()
        self.assertIn(f"interpreter {sys.executable}", printed)
        # Derived from the imported module, not from a path this test builds:
        # the claim is "where the running code lives", and re-deriving it from
        # the same expression the code uses would assert nothing.
        self.assertIn(f"package {Path(cli.__file__).resolve().parent}", printed)

    def test_the_package_line_would_expose_a_shared_environment(self):
        """The case the addition exists for, asserted rather than described.

        `__version__` comes from installed distribution metadata while the code
        runs from wherever it was imported. When a store's pinned release has
        replaced an editable install - CLAUDE.md's shared-environment trap - those
        two disagree, and this output is where the disagreement becomes visible.
        So the package line must report the imported module's location and never
        a location derived from the distribution.
        """
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--version"])
        line = [row for row in out.getvalue().splitlines() if row.startswith("package ")]
        self.assertEqual(len(line), 1, out.getvalue())
        reported = Path(line[0].removeprefix("package "))
        self.assertTrue((reported / "cli.py").is_file(), f"{reported} holds no cli.py")
        self.assertEqual(reported, Path(cli.__file__).resolve().parent)

    def test_asking_for_the_version_runs_no_stage(self):
        """The same trap as `--help`: the flag must not be the thing that acts.

        `--version` is checked before the stage lookup, so no stage module is even
        imported. Asserting on a sentinel rather than trusting the ordering, because the
        ordering is exactly what a later edit moves.
        """
        ran = []
        module = __import__("knowledgestore.sync_repositories", fromlist=["main"])
        original = module.main
        module.main = lambda: ran.append(True) or 0
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["--version"])
        finally:
            module.main = original
        self.assertEqual(code, 0)
        self.assertEqual(ran, [])
