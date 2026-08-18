"""The version has one source of truth: the installed distribution metadata.

The distribution version is derived from the git tag at build time
(hatch-vcs), so a hand-maintained copy in source is the defect this guards
against — two of the three copies had already drifted when this test was
written.
"""

from importlib.metadata import version

import contextlib
import io

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
