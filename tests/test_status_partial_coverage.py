"""`status` must not read green while a layer covers a third of the estate.

An operator building a 361-repository store saw "Summaries: 914/914" and
"Provenance: 361 repositories recorded", concluded the store was complete, and
found by opening `file-tickets.json.gz` by hand that the intent index held
**108 of 361**. Nothing in the report said so.

That is the most dangerous shape a knowledge store has. "No tickets touched this
file" from an unmined repository is indistinguishable, in the answer, from a file
no ticket ever touched — a confident negative built on a layer that was never
consulted.

The companion case is the same fault in miniature: "Corpus citations: 0 checked,
none dangling" paired a measurement of nothing with a clean verdict, and was read
past repeatedly. Nothing checked is not the same as nothing wrong.

Both reported by the operator of a consuming estate; neither was visible from the
maintainer's own store, where coverage happens to be even.
"""

from __future__ import annotations

import contextlib
import gzip
import io as _io
import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import status  # noqa: E402


def _write_index(path: Path, repos: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as out:
        json.dump(repos, out)


class StatusReportTest(SettingsIsolated):
    """Drives `status.main()` itself.

    An earlier version of this file re-implemented the reporting branch in the
    test and asserted against its own copy. It passed, and a mutation that
    removed the real warning did not fail it - a test that cannot fail is worse
    than no test, because it reads as protection.
    """

    def _run(self, mined: int, estate: int) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        index = root / "knowledge" / "intent" / "file-tickets.json.gz"
        _write_index(index, {f"repo-{i}": {"a.py": {"tickets": {"T-1": 1}}} for i in range(mined)})

        provenance_path = root / "provenance.json"
        provenance_path.write_text(
            json.dumps({"repositories": {f"repo-{i}": {"sha": "x"} for i in range(estate)}}),
            encoding="utf-8",
        )
        config.configure(
            ROOT=root,
            INTENT_INDEX_PATH=index,
            PROVENANCE_PATH=provenance_path,
            SUMMARIES_PATH=root / "summaries.json",
            SUMMARIES_INPUT_PATH=root / "digests.json",
            TOPICS_BRIEFS_PATH=root / "briefs.json",
            TOPICS_CONFIG_PATH=root / "topics.txt",
        )
        self.addCleanup(setattr, status, "run_git", status.run_git)
        status.run_git = lambda arguments: ""

        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()):
            status.main([])
        return out.getvalue()

    def test_partial_coverage_is_reported_with_what_it_costs(self):
        text = self._run(mined=108, estate=361)
        self.assertIn("108/361", text)
        self.assertIn(
            "no ticket evidence",
            text,
            "a ratio alone does not tell a reader that answers about the rest are unevidenced",
        )

    def test_full_coverage_is_not_nagged_about(self):
        text = self._run(mined=361, estate=361)
        self.assertIn("361/361", text)
        self.assertNotIn("no ticket evidence", text)

    def test_nothing_checked_is_not_reported_as_a_pass(self):
        text = self._run(mined=1, estate=1)
        self.assertIn("none checked", text)
        self.assertNotIn(
            "none dangling",
            text,
            "pairing a measurement of nothing with a clean verdict reads as a pass",
        )


class IntentCoverageTest(SettingsIsolated):
    def test_a_repository_with_an_empty_entry_is_not_counted_as_mined(self):
        """An empty mapping is a repository the export produced nothing for."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index = Path(tmp.name) / "file-tickets.json.gz"
        _write_index(index, {"a": {"f.py": {}}, "b": {}, "c": {}})
        config.configure(INTENT_INDEX_PATH=index)
        self.assertEqual(status.intent_coverage({"a": {}, "b": {}, "c": {}})["mined"], 1)


if __name__ == "__main__":
    unittest.main()
