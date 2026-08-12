"""An interrupted sync must leave a truthful partial record, not nothing.

Provenance used to be written once, after every repository had been attempted.
A run that was interrupted - Ctrl-C, a dropped connection, or simply run in
stages across a session - therefore left no `provenance.json` at all, and
`status`, the repository manifest and the explorer all read it (issue #107).

The failure is quiet: the file is simply absent, which looks the same as an
estate that was never synced. On the estate that reported this it went unnoticed
for hours because the build notes said the step had been done.

Note the distinction from `SyncFailureIsolationTest`: a repository *failing* was
already handled - the loop continues and provenance covers the rest. This is the
case where the loop never reaches its end at all.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import sync_repositories as sync  # noqa: E402


class SyncInterruptedTest(SettingsIsolated):
    def _setup(self, root: Path, stop_at: str):
        from knowledgestore import provenance

        config_path = root / "repositories.txt"
        config_path.write_text(
            "".join(f"repo-{n}|git@example.com:o/repo-{n}.git|main\n" for n in "abcde"),
            encoding="utf-8",
        )
        config.configure(REPOSITORIES_CONFIG=config_path)
        config.configure(REPOSITORIES_DIR=root / "repositories")
        config.configure(PROVENANCE_PATH=root / "provenance.json")

        self.attempted: list[str] = []

        def fake_sync(repo, repositories_dir, run=None):
            self.attempted.append(repo.name)
            if repo.name == stop_at:
                raise KeyboardInterrupt
            return 5

        self.addCleanup(setattr, sync, "sync_repository", sync.sync_repository)
        sync.sync_repository = fake_sync

        self.addCleanup(setattr, provenance, "head_info", provenance.head_info)
        provenance.head_info = lambda repo_dir, branch, run=None: {
            "sha": (repo_dir.name * 40)[:40],
            "branch": branch,
            "committed": "2026-07-01T00:00:00+00:00",
        }
        return provenance

    def test_an_interrupted_sync_still_records_what_succeeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._setup(root, stop_at="repo-c")

            with self.assertRaises(KeyboardInterrupt):
                sync.main()

            recorded = root / "provenance.json"
            self.assertTrue(
                recorded.is_file(),
                "an interrupted sync left no provenance at all, which reads as "
                "'never synced' rather than 'synced up to here'",
            )
            entries = json.loads(recorded.read_text(encoding="utf-8"))["repositories"]
            self.assertEqual(
                sorted(entries),
                ["repo-a", "repo-b"],
                "the partial record should name exactly the repositories that succeeded",
            )
            self.assertEqual(self.attempted, ["repo-a", "repo-b", "repo-c"])

    def test_the_record_is_truthful_rather_than_merely_present(self):
        """A partial record that claimed repositories it had not synced would be
        worse than none, so check the contents, not just the file."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._setup(root, stop_at="repo-b")
            with self.assertRaises(KeyboardInterrupt):
                sync.main()
            entries = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(entries["repositories"]), ["repo-a"])
            self.assertEqual(entries["repositories"]["repo-a"]["branch"], "main")

    def test_a_complete_run_is_unchanged(self):
        """The incremental write must not alter the finished result."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._setup(root, stop_at="never")
            code = sync.main()
            self.assertEqual(code, 0)
            entries = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(entries["repositories"]),
                ["repo-a", "repo-b", "repo-c", "repo-d", "repo-e"],
            )


if __name__ == "__main__":
    unittest.main()
