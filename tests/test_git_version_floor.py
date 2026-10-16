"""Git 2.48 or newer is a prerequisite for every stage that runs git.

`sync` converts a clone to reftable refs when its remote holds branches differing
only in case (#437), and an older git cannot read a converted clone - on the
machine that ran `sync` or on any CI runner that reads the store's clones. So the
stages that run git refuse below the floor before any git work, `status` reports
the same finding without failing (CLAUDE.md 6.7), and `--version` prints it.

Stubbed at the IO boundary only: `git_version.run_git`, the one function that
runs `git --version`. Everything from there to the exit code is real.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import cli  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import git_version  # noqa: E402
from knowledgestore import status  # noqa: E402

GIT_STAGES = ("sync", "drift", "export-history", "check-citations")


def saying(output: str):
    return lambda arguments: output


class GitIs(SettingsIsolated):
    """Runs the CLI with `git --version` answering `output`, in an empty store."""

    def _cli(self, output: str, *argv: str) -> tuple[int, str, str]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config.configure(root=Path(tmp.name))
        self.addCleanup(setattr, git_version, "run_git", git_version.run_git)
        git_version.run_git = saying(output)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class StagesRefuseAnOldGit(GitIs):
    def test_git_2_47_is_refused_naming_the_version_and_the_floor(self):
        """Breaks if the floor is not enforced: sync would convert clones that
        this machine's own git, or a CI runner's, then cannot read."""
        code, _, err = self._cli("git version 2.47.1 (Apple Git-150)", "sync")
        self.assertEqual(code, 1)
        self.assertIn("2.47", err)
        self.assertIn("2.48", err)
        self.assertIn("Upgrade git", err)
        # The stage itself never started: in an empty store it would have said so.
        self.assertNotIn("Repository configuration not found", err)

    def test_every_git_using_stage_refuses(self):
        """Breaks if a stage that runs git is left out of the gate."""
        for stage in GIT_STAGES:
            with self.subTest(stage=stage):
                code, _, err = self._cli("git version 2.47.1", stage)
                self.assertEqual(code, 1)
                self.assertIn("older than 2.48", err)

    def test_unreadable_version_output_is_refused_clearly(self):
        """Breaks if output that names no version is read as one, or as a pass."""
        for output in ("", "hub version 2.14.2", "git version two"):
            with self.subTest(output=output):
                code, _, err = self._cli(output, "sync")
                self.assertEqual(code, 1)
                self.assertIn("Cannot tell which git this is", err)
                self.assertIn("2.48 or newer", err)


class StagesAcceptACurrentGit(GitIs):
    def test_git_2_48_runs_the_stage(self):
        """Breaks if the comparison is off by one at the floor itself."""
        code, _, err = self._cli("git version 2.48.0", "sync")
        self.assertIn("Repository configuration not found", err, "the stage ran")
        self.assertNotIn("2.48 or newer", err)
        self.assertEqual(code, 1)  # the empty store's own answer, not a refusal

    def test_git_2_55_runs_the_stage(self):
        """Breaks if a newer minor is compared as text rather than as a number."""
        _, _, err = self._cli("git version 2.55.0", "sync")
        self.assertIn("Repository configuration not found", err, "the stage ran")
        self.assertNotIn("older than", err)

    def test_a_major_version_above_two_runs_the_stage(self):
        """Breaks if only the minor number is compared."""
        _, _, err = self._cli("git version 3.0.0", "sync")
        self.assertIn("Repository configuration not found", err)


class VersionFlagReportsGit(GitIs):
    def test_version_names_git_and_the_floor(self):
        """Breaks if `--version` stops answering which git a stage would run."""
        code, out, _ = self._cli("git version 2.47.1", "--version")
        self.assertEqual(code, 0)
        self.assertIn("git 2.47 is below the 2.48 floor", out)
        code, out, _ = self._cli("git version 2.55.0", "--version")
        self.assertIn("git 2.55 meets the 2.48 floor", out)


class StatusReportsAnOldGit(SettingsIsolated):
    def test_status_reports_an_old_git_and_still_exits_zero(self):
        """Breaks if `status` refuses on an old git (it never fails, CLAUDE.md
        6.7) or stops mentioning it."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "provenance.json").write_text(
            json.dumps({"repositories": {}, "external": {}}), encoding="utf-8"
        )
        config.configure(
            ROOT=root,
            EXTERNAL_CONFIG=root / "absent-external.txt",
            REPOSITORIES_CONFIG=root / "absent.txt",
            PROVENANCE_PATH=root / "provenance.json",
            INTENT_INDEX_PATH=root / "intent.json.gz",
            SUMMARIES_PATH=root / "s.json",
            SUMMARIES_INPUT_PATH=root / "d.json",
            TOPICS_BRIEFS_PATH=root / "b.json",
            TOPICS_CONFIG_PATH=root / "t.txt",
        )
        self.addCleanup(setattr, status, "run_git", status.run_git)
        status.run_git = lambda arguments: ""
        self.addCleanup(setattr, git_version, "run_git", git_version.run_git)
        git_version.run_git = saying("git version 2.47.1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = status.main([])
        self.assertEqual(code, 0)
        self.assertIn("git 2.47 is below the 2.48 floor", out.getvalue())


class ParseTest(unittest.TestCase):
    def test_reads_the_forms_git_prints(self):
        """Breaks if a platform's suffix stops the version being read."""
        self.assertEqual(git_version.parse("git version 2.47.1 (Apple Git-150)"), (2, 47))
        self.assertEqual(git_version.parse("git version 2.45.1.windows.1"), (2, 45))
        self.assertEqual(git_version.parse("git version 2.55.0\n"), (2, 55))
        self.assertIsNone(git_version.parse("git version"))


if __name__ == "__main__":
    unittest.main()
