"""Asking a subcommand what it does must never be the thing that does it.

Most stages build no argument parser, so `--help` used to fall straight through
to the stage's default action. For `sync` that fetches and resets every
repository in the estate - triggered by the one flag someone reaches for when
they are deliberately trying not to do anything yet (issue #106).

The interesting test is not that help prints. It is that the stage never runs:
these assert the stage's main is not called at all, which is the property that
made the original behaviour dangerous rather than merely unhelpful.
"""

from __future__ import annotations

import io
import contextlib
import unittest

from knowledgestore import cli


class HelpNeverRunsTheStage(unittest.TestCase):
    def _help(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_help_on_a_stage_that_parses_nothing_does_not_run_it(self):
        """The whole point: `sync --help` must not sync."""
        ran = []
        module = __import__("knowledgestore.sync_repositories", fromlist=["main"])
        original = module.main
        module.main = lambda: ran.append(True) or 0
        try:
            code, text = self._help("sync", "--help")
        finally:
            module.main = original
        self.assertEqual(code, 0)
        self.assertEqual(ran, [], "sync ran despite being asked only for help")
        self.assertIn("clone or update every configured repository", text)

    def test_every_non_parsing_stage_answers_help_without_running(self):
        """One stage was reported; twelve had the same hole. Cover them all."""
        for stage in sorted(set(cli.STAGES) - cli.SELF_PARSING):
            with self.subTest(stage=stage):
                module_name = cli.STAGES[stage][0]
                module = __import__(f"knowledgestore.{module_name}", fromlist=["main"])
                original = getattr(module, "main", None)
                if original is None:
                    continue
                ran = []
                module.main = lambda: ran.append(True) or 0
                try:
                    code, text = self._help(stage, "--help")
                finally:
                    module.main = original
                self.assertEqual(code, 0, f"{stage} --help did not exit cleanly")
                self.assertEqual(ran, [], f"{stage} ran despite being asked only for help")
                self.assertIn(stage, text)

    def test_short_form_is_handled_too(self):
        ran = []
        module = __import__("knowledgestore.build_explorer", fromlist=["main"])
        original = module.main
        module.main = lambda: ran.append(True) or 0
        try:
            code, _ = self._help("explorer", "-h")
        finally:
            module.main = original
        self.assertEqual(code, 0)
        self.assertEqual(ran, [])


class SelfParsingStaysHonest(unittest.TestCase):
    """SELF_PARSING is a hand-maintained list, so it is the thing most likely to
    drift. An entry naming a stage that does not actually parse arguments would
    hand --help back to a stage that ignores it and runs - reintroducing exactly
    the bug this fixes, silently. So it is checked against the source."""

    def _module_source(self, stage: str) -> str:
        import inspect

        module = __import__(f"knowledgestore.{cli.STAGES[stage][0]}", fromlist=["main"])
        return inspect.getsource(module)

    def test_every_self_parsing_stage_really_builds_a_parser(self):
        for stage in sorted(cli.SELF_PARSING):
            with self.subTest(stage=stage):
                self.assertIn(stage, cli.STAGES, f"{stage} is not a stage at all")
                self.assertIn(
                    "ArgumentParser",
                    self._module_source(stage),
                    f"{stage} is listed as self-parsing but builds no parser, so --help "
                    "would fall through to the stage and run it",
                )

    def test_no_stage_outside_the_list_builds_a_parser(self):
        """The other direction: a stage that grew a parser should be added, or its
        own richer help is shadowed by the generic text."""
        for stage in sorted(set(cli.STAGES) - cli.SELF_PARSING):
            with self.subTest(stage=stage):
                self.assertNotIn(
                    "ArgumentParser",
                    self._module_source(stage),
                    f"{stage} builds a parser but is not in SELF_PARSING, so its own "
                    "help is being shadowed",
                )


if __name__ == "__main__":
    unittest.main()
