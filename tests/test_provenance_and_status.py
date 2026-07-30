"""Provenance recording and the status stage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from knowledgestore import config, io, provenance


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


class LayerCoverageTest(unittest.TestCase):
    def test_counts_summaries_briefs_and_topics(self):
        from knowledgestore import status

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for attr, value in {
                "SUMMARIES_PATH": root / "communities.json",
                "SUMMARIES_INPUT_PATH": root / "communities-input.json",
                "TOPICS_BRIEFS_PATH": root / "briefs.json",
                "TOPICS_CONFIG_PATH": root / "topics.txt",
            }.items():
                self.addCleanup(setattr, status, attr, getattr(status, attr))
                setattr(status, attr, value)
            io.write_json(root / "communities.json", {"1": "x", "2": "y"})
            io.write_json(root / "communities-input.json", [{"id": 1}, {"id": 2}, {"id": 3}])
            io.write_json(root / "briefs.json", {"welsh": {}})
            (root / "topics.txt").write_text(
                "welsh | Welsh | welsh\naddr | Addresses | address\n", encoding="utf-8"
            )
            got = status.layer_coverage()
        self.assertEqual(
            got,
            {
                "summaries_written": 2,
                "summaries_expected": 3,
                "briefs_written": 1,
                "topics_configured": 2,
            },
        )


class CorpusCitationsTest(unittest.TestCase):
    def test_reports_nodes_citing_missing_files(self):
        from knowledgestore import status

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "present.md").write_text("x", encoding="utf-8")
            corpus = root / "graphify-out"
            corpus.mkdir()
            io.write_json(
                corpus / "graph-knowledge-corpus.json",
                {
                    "nodes": [
                        {"id": "a", "source_file": "present.md"},
                        {"id": "b", "source_file": "gone/away.sh"},
                        {"id": "c"},
                    ]
                },
            )
            got = status.corpus_citations(root)
        self.assertEqual(got, {"checked": 2, "dangling": ["gone/away.sh"]})


class FreshnessTest(unittest.TestCase):
    def test_flags_explorer_older_than_layers(self):
        from knowledgestore import status

        def fake_git(arguments):
            self.assertEqual(arguments[0], "-C")
            self.assertTrue(arguments[2] in ("log", "-1"))
            path = arguments[-1]
            return (
                "2026-07-01T00:00:00+00:00\n"
                if "explorer" in path
                else "2026-07-20T00:00:00+00:00\n"
            )

        got = status.artefact_freshness(run=fake_git)
        self.assertTrue(got["explorer_stale"])

    def test_empty_outside_a_git_repository(self):
        from knowledgestore import status

        def failing_git(arguments):
            self.assertEqual(arguments[0], "-C")
            raise RuntimeError("not a git repository")

        self.assertEqual(status.artefact_freshness(run=failing_git), {})


if __name__ == "__main__":
    unittest.main()
