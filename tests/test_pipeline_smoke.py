"""Pipeline smoke test: the seams between stages.

Builds a real fixture repository, exports its history with the real export
stage, then feeds that output to the real intent stage - verifying the
stage-to-stage contracts a package restructure is most likely to break.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import export_git_history as export  # noqa: E402
from knowledgestore import build_intent_index as intent  # noqa: E402


class ExportToIntentSeamTest(SettingsIsolated):
    def _git(self, cwd, *args):
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _make_repo(self, root: Path) -> None:
        repo = root / "repositories" / "repo-a"
        repo.mkdir(parents=True)
        self._git(repo, "init", "-q", "-b", "main")
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "Test")
        (repo / "address.pipe.ts").write_text("export const x = 1;\n")
        self._git(repo, "add", "address.pipe.ts")
        self._git(
            repo,
            "commit",
            "-q",
            "-m",
            "CRC-12016: local copy of address pipe after core broke prod",
        )
        (repo / "address.pipe.ts").write_text("export const x = 2;\n")
        self._git(repo, "add", "address.pipe.ts")
        self._git(repo, "commit", "-q", "-m", "no ticket housekeeping")

    def test_history_export_feeds_intent_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_repo(root)
            history = root / "knowledge" / "git-history"

            # stage 3: history export (real module, real git repo)
            export.export_repository(
                root,
                history,
                export.RepositoryConfig(
                    name="repo-a", clone_url="git@example.com:o/repo-a.git", default_branch="main"
                ),
            )

            # stage 4b: intent index consumes the export's output
            config.configure(HISTORY_DIR=history)
            config.configure(
                INTENT_INDEX_PATH=root / "knowledge" / "intent" / "file-tickets.json.gz"
            )
            config.configure(
                TICKET_DESCRIPTIONS_PATH=root
                / "knowledge"
                / "intent"
                / "ticket-descriptions.json.gz"
            )
            code = intent.main()

            index = json.load(gzip.open(config.INTENT_INDEX_PATH, "rt", encoding="utf-8"))
            descriptions = json.load(
                gzip.open(config.TICKET_DESCRIPTIONS_PATH, "rt", encoding="utf-8")
            )

        self.assertEqual(code, 0)
        self.assertEqual(list(index["repo-a"]["address.pipe.ts"]["tickets"]), ["CRC-12016"])
        self.assertIn(
            "local copy of address pipe after core broke prod", descriptions["CRC-12016"]["d"][0]
        )
        self.assertEqual(descriptions["CRC-12016"]["repos"], ["repo-a"])


if __name__ == "__main__":
    unittest.main()
