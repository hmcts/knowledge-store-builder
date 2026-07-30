"""Provenance recording and the status stage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from knowledgestore import config, provenance


class HeadInfoTest(unittest.TestCase):
    def test_reads_sha_and_commit_date_from_git(self):
        calls = []

        def fake_git(arguments):
            calls.append(arguments)
            if "rev-parse" in arguments:
                return "a" * 40 + "\n"
            return "2026-07-30T09:14:02+01:00\n"

        info = provenance.head_info(Path("/tmp/x"), "main", run=fake_git)
        self.assertEqual(
            info,
            {
                "sha": "a" * 40,
                "branch": "main",
                "committed": "2026-07-30T09:14:02+01:00",
            },
        )
        self.assertTrue(all("-C" in c for c in calls))


class WriteReadTest(unittest.TestCase):
    def test_round_trip_sorted_and_missing_file_is_empty(self):
        original = config.PROVENANCE_PATH
        self.addCleanup(config.configure, None, PROVENANCE_PATH=original)
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(PROVENANCE_PATH=Path(tmp) / "provenance.json")
            self.addCleanup(setattr, provenance, "PROVENANCE_PATH", provenance.PROVENANCE_PATH)
            provenance.PROVENANCE_PATH = config.PROVENANCE_PATH
            self.assertEqual(provenance.read(), {})
            provenance.write({"zeta": {"sha": "z"}, "alpha": {"sha": "a"}})
            self.assertEqual(list(provenance.read()), ["alpha", "zeta"])


if __name__ == "__main__":
    unittest.main()
