"""Tests for knowledgestore/build_knowledge_context.py - previously untested stage."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


from knowledgestore import build_knowledge_context as context  # noqa: E402


def make_history(tmp: Path) -> Path:
    """A minimal knowledge/git-history layout with one repository."""
    repo_dir = tmp / "git-history" / "repo-a"
    repo_dir.mkdir(parents=True)
    record = {"repository": "repo-a", "repository_url": "git@example.com:o/repo-a.git", "sha": "x"}
    (repo_dir / "commits.ndjson").write_text(
        json.dumps(record) + "\n" + json.dumps(record) + "\n", encoding="utf-8"
    )
    (repo_dir / "index.md").write_text("# repo-a\n", encoding="utf-8")
    return repo_dir


class HelpersTest(unittest.TestCase):
    def test_read_first_record_and_count_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = make_history(Path(tmp))
            ndjson = repo_dir / "commits.ndjson"
            first = context.read_first_record(ndjson)
            self.assertEqual(first["repository_url"], "git@example.com:o/repo-a.git")
            self.assertEqual(context.count_lines(ndjson), 2)

    def test_read_first_record_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "commits.ndjson"
            empty.write_text("", encoding="utf-8")
            self.assertEqual(context.read_first_record(empty), {})


class BuildOutputsTest(unittest.TestCase):
    def _patch_paths(self, tmp: Path):
        context.HISTORY_DIR = tmp / "git-history"
        context.MANIFEST_PATH = tmp / "repository-manifest.md"
        context.CONTEXT_PATH = tmp / "knowledge_context.md"

    def test_manifest_lists_repository_with_count_and_source(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo_dir = make_history(tmp)
            self._patch_paths(tmp)
            context.build_manifest([repo_dir])
            manifest = context.MANIFEST_PATH.read_text(encoding="utf-8")
        self.assertIn("`repo-a`", manifest)
        self.assertIn("| 2 |", manifest)  # commit count
        self.assertIn("git@example.com:o/repo-a.git", manifest)
        self.assertIn("git-history/repo-a/index.md", manifest)

    def test_context_names_repositories_and_core_rules(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo_dir = make_history(tmp)
            self._patch_paths(tmp)
            context.build_context([repo_dir])
            content = context.CONTEXT_PATH.read_text(encoding="utf-8")
        self.assertIn("- `repo-a`", content)
        self.assertIn("source of truth", content)
        self.assertIn("commits.ndjson", content)


if __name__ == "__main__":
    unittest.main()
