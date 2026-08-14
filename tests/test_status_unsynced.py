"""A repository can be declared, committed, and never cloned — silently.

A manifest is intent; provenance is what reached disk. Nothing compared them.
A fetch-only entry was added to `config/repository-filters.txt`, `discover`
wrote it into `config/repositories-external.txt`, the change was committed and
merged — and `sync` was never re-run. `sync` had nothing to complain about,
because it only reports on repositories it attempted. Every later `status`
reported success over an estate one repository short of its own configuration.

Found by an operator adopting a new library version, who noticed the entry had
no directory under `external/`. Nothing was broken because nothing read it yet,
which is precisely why it could have stayed that way indefinitely.
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
from knowledgestore import status  # noqa: E402


class UnsyncedTest(SettingsIsolated):
    def _manifest(self, names: list[str]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "repositories.txt"
        path.write_text(
            "".join(f"{n}|git@example.com:o/{n}.git|main\n" for n in names), encoding="utf-8"
        )
        return path

    def test_it_names_what_was_declared_but_never_recorded(self):
        manifest = self._manifest(["alpha", "beta", "gamma"])
        self.assertEqual(status.unsynced(manifest, {"alpha": {}}), ["beta", "gamma"])

    def test_a_fully_synced_manifest_reports_nothing(self):
        manifest = self._manifest(["alpha", "beta"])
        self.assertEqual(status.unsynced(manifest, {"alpha": {}, "beta": {}}), [])

    def test_an_absent_manifest_is_not_a_finding(self):
        """Most estates declare no fetch-only repositories at all."""
        self.assertEqual(status.unsynced(Path("/nonexistent/repositories.txt"), {}), [])

    def test_provenance_recording_more_than_the_manifest_is_not_flagged(self):
        """A repository removed from the estate leaves its provenance behind; that
        is a different condition and not this one's business."""
        manifest = self._manifest(["alpha"])
        self.assertEqual(status.unsynced(manifest, {"alpha": {}, "retired": {}}), [])


class ReportTest(SettingsIsolated):
    def _run(self, declared: list[str], recorded: list[str]) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        external = root / "repositories-external.txt"
        external.write_text(
            "".join(f"{n}|git@example.com:o/{n}.git|main\n" for n in declared), encoding="utf-8"
        )
        (root / "provenance.json").write_text(
            json.dumps({"repositories": {}, "external": {n: {"sha": "x"} for n in recorded}}),
            encoding="utf-8",
        )
        config.configure(
            ROOT=root,
            EXTERNAL_CONFIG=external,
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
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()):
            status.main([])
        return out.getvalue()

    def test_the_gap_is_named_with_a_count_and_the_fix(self):
        text = self._run(declared=["kept", "never-cloned"], recorded=["kept"])
        self.assertIn("1 of 2 fetch-only repositories", text)
        self.assertIn("never-cloned", text)
        self.assertIn("knowledgestore sync", text, "a report without the remedy is a puzzle")

    def test_a_complete_estate_is_not_nagged(self):
        text = self._run(declared=["kept"], recorded=["kept"])
        self.assertNotIn("Declared but never synced", text)


class FailedSyncRetentionTest(SettingsIsolated):
    """A failed sync must leave a marked record, not a hole.

    Deleting the record is worse than a stale clone: every reconciliation that
    iterates provenance is then blind to the repository, because a check keyed
    on the record cannot see a record that was removed. Reported from an estate
    where a repository with a moved tag vanished from a 163-entry manifest and a
    provenance-versus-remote check examined 164 repositories without it.
    """

    def _report(self, recorded: dict) -> str:
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            status._report_failed_syncs(recorded)
        return out.getvalue()

    def test_a_retained_record_is_reported_as_stale(self):
        text = self._report({"a": {"sha": "x"}, "b": {"sha": "y", "sync_failed": "boom"}})
        self.assertIn("b", text)
        self.assertIn("1 of 2", text)
        self.assertIn(
            "older state",
            text,
            "retention without a report trades a visible gap for an invisible staleness",
        )

    def test_a_clean_estate_is_not_nagged(self):
        self.assertEqual(self._report({"a": {"sha": "x"}}), "")


if __name__ == "__main__":
    unittest.main()
