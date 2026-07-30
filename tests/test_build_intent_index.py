"""Tests for knowledgestore/build_intent_index.py - intent index + ticket descriptions."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path


from knowledgestore import build_intent_index as intent  # noqa: E402


def make_descriptions():
    return defaultdict(
        lambda: {
            "descriptions": defaultdict(int),
            "repos": set(),
            "first": None,
            "last": None,
            "count": 0,
        }
    )


def make_files():
    return defaultdict(lambda: {"tickets": defaultdict(int), "first": None, "last": None})


class CleanDescriptionTest(unittest.TestCase):
    def test_strips_single_ticket_prefix(self):
        self.assertEqual(
            intent.clean_description("DD-24302: address field length changed to 35"),
            "address field length changed to 35",
        )

    def test_strips_multiple_ticket_prefixes_and_brackets(self):
        self.assertEqual(
            intent.clean_description("[DD-1] CCT-2 - do the thing"),
            "do the thing",
        )

    def test_leaves_ticketless_subjects_alone(self):
        self.assertEqual(intent.clean_description("plain subject."), "plain subject")


class JunkFilterTest(unittest.TestCase):
    def test_junk_descriptions_match(self):
        for junk in ("wip", "Fixed", "addressed PR comments", "update", "refactoring"):
            self.assertIsNotNone(intent.JUNK_DESCRIPTION.match(junk), junk)

    def test_real_descriptions_do_not_match(self):
        self.assertIsNone(
            intent.JUNK_DESCRIPTION.match("Increase Validation on Address Entry Fields")
        )


class ApplyCommitTest(unittest.TestCase):
    def _commit(self, subject, merge=False, date="2024-05-01T10:00:00+00:00"):
        return {
            "repository": "repo-a",
            "subject": subject,
            "is_merge": merge,
            "author_date": date,
            "files": [{"path": "src/x.ts"}],
        }

    def test_merge_and_ticketless_commits_are_skipped(self):
        files, descriptions = make_files(), make_descriptions()
        self.assertFalse(
            intent.apply_commit(self._commit("DD-1: x", merge=True), files, descriptions)
        )
        self.assertFalse(intent.apply_commit(self._commit("no ticket here"), files, descriptions))
        self.assertEqual(len(files), 0)

    def test_ticketed_commit_updates_files_and_descriptions(self):
        files, descriptions = make_files(), make_descriptions()
        self.assertTrue(
            intent.apply_commit(
                self._commit("DD-9: introduce address validation rules"), files, descriptions
            )
        )
        self.assertEqual(files["src/x.ts"]["tickets"]["DD-9"], 1)
        self.assertEqual(files["src/x.ts"]["first"], "2024-05-01")
        info = descriptions["DD-9"]
        self.assertEqual(info["count"], 1)
        self.assertIn("introduce address validation rules", info["descriptions"])

    def test_junk_description_counts_commit_but_keeps_no_text(self):
        files, descriptions = make_files(), make_descriptions()
        intent.apply_commit(self._commit("DD-9: wip"), files, descriptions)
        self.assertEqual(descriptions["DD-9"]["count"], 1)
        self.assertEqual(len(descriptions["DD-9"]["descriptions"]), 0)


class IndexRepositoryTest(unittest.TestCase):
    def test_end_to_end_over_ndjson(self):
        commits = [
            {
                "repository": "repo-a",
                "subject": "DD-1: add address form",
                "is_merge": False,
                "author_date": "2024-01-02T09:00:00+00:00",
                "files": [{"path": "a.ts"}, {"path": "b.ts"}],
            },
            {
                "repository": "repo-a",
                "subject": "DD-1: add address form",
                "is_merge": False,
                "author_date": "2024-02-03T09:00:00+00:00",
                "files": [{"path": "a.ts"}],
            },
            {
                "repository": "repo-a",
                "subject": "merge branch",
                "is_merge": True,
                "author_date": "2024-02-04T09:00:00+00:00",
                "files": [{"path": "a.ts"}],
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ndjson = Path(tmp) / "commits.ndjson"
            ndjson.write_text("\n".join(json.dumps(c) for c in commits) + "\n", encoding="utf-8")
            descriptions = make_descriptions()
            files, seen = intent.index_repository(ndjson, descriptions)

        self.assertEqual(seen, 2)
        self.assertEqual(files["a.ts"]["tickets"], {"DD-1": 2})
        self.assertEqual(files["a.ts"]["first"], "2024-01-02")
        self.assertEqual(files["a.ts"]["last"], "2024-02-03")
        self.assertEqual(descriptions["DD-1"]["descriptions"]["add address form"], 2)


if __name__ == "__main__":
    unittest.main()
