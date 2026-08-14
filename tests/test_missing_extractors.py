"""A build that cannot parse a content type must say so, not just be short.

`pip install` without graphify's optional extras produces a store that succeeds
while reading whole content types as nothing. On one estate installing them took
genuine estate content from ~4,400 to ~11,000 nodes. On another — the
maintainer's own, which had been shipped and queried for weeks — 320 `.tf` files
and 1,278 `.sql` files contributed 0 and 43 nodes respectively, and no stage said
why (issue #133).

The failure is the familiar one: nothing errors, the graph is simply short, and a
reader cannot tell a thin estate from a build that could not see most of it.

The parsers belong to graphify rather than to this library, so the check probes
for the import rather than for a declared dependency of our own.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import status  # noqa: E402


class MissingExtractorTest(SettingsIsolated):
    def _corpus(self, files: list[str]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel in files:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        return root

    def _without(self, *modules: str):
        """Pretend the named parsers are not installed."""
        real = status._extractor_installed
        self.addCleanup(setattr, status, "_extractor_installed", real)
        status._extractor_installed = lambda m: m not in modules

    def test_it_counts_the_files_that_cannot_be_parsed(self):
        self._without("tree_sitter_hcl")
        root = self._corpus(["a/main.tf", "a/vars.tfvars", "b/x.hcl", "b/keep.py"])
        gaps = status.missing_extractors(root)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["files"], 3, "counts every affected suffix, not just .tf")
        self.assertEqual(gaps[0]["extra"], "terraform")

    def test_nothing_is_reported_when_the_parser_is_installed(self):
        self._without()  # everything present
        self.assertEqual(status.missing_extractors(self._corpus(["a/main.tf"])), [])

    def test_nothing_is_reported_when_the_corpus_has_none_of_that_type(self):
        """A missing parser for content nobody has is not a finding."""
        self._without("tree_sitter_hcl", "tree_sitter_sql")
        self.assertEqual(status.missing_extractors(self._corpus(["a/app.py"])), [])

    def test_each_content_type_is_reported_separately(self):
        self._without("tree_sitter_hcl", "tree_sitter_sql")
        gaps = status.missing_extractors(self._corpus(["a/main.tf", "b/q.sql", "b/r.sql"]))
        by_extra = {g["extra"]: g["files"] for g in gaps}
        self.assertEqual(by_extra, {"terraform": 1, "sql": 2})

    def test_git_metadata_is_not_counted(self):
        """A corpus clone's own history is not estate content."""
        self._without("tree_sitter_sql")
        root = self._corpus(["a/.git/objects/x.sql", "a/real.sql"])
        self.assertEqual(status.missing_extractors(root)[0]["files"], 1)

    def test_an_absent_corpus_is_not_an_error(self):
        """`status` runs before `sync` too."""
        self._without("tree_sitter_hcl")
        self.assertEqual(status.missing_extractors(Path("/nonexistent")), [])


if __name__ == "__main__":
    unittest.main()
