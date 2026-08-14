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

import contextlib
import io
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

    def test_symlinks_are_not_counted_twice(self):
        """A symlink's target is almost always already in the corpus, so counting
        both reports one file as two. On a real estate that turned 260 into 320."""
        self._without("tree_sitter_hcl")
        root = self._corpus(["a/real.tf"])
        (root / "a" / "link.tf").symlink_to(root / "a" / "real.tf")
        self.assertEqual(status.missing_extractors(root)[0]["files"], 1)

    def test_git_metadata_is_not_counted(self):
        """A corpus clone's own history is not estate content."""
        self._without("tree_sitter_sql")
        root = self._corpus(["a/.git/objects/x.sql", "a/real.sql"])
        self.assertEqual(status.missing_extractors(root)[0]["files"], 1)

    def test_an_absent_corpus_is_not_an_error(self):
        """`status` runs before `sync` too."""
        self._without("tree_sitter_hcl")
        self.assertEqual(status.missing_extractors(Path("/nonexistent")), [])


class DuplicatingSymlinkTest(SettingsIsolated):
    """Extraction records the path it walked, not the link target.

    Measured through the CLI the pipeline actually invokes: one real file and one
    symlink to it produce two distinct `source_file` values and two sets of nodes
    with identical content. On an estate asked about duplication that is a wrong
    answer, not a noisy one - a shared parent resource appears once per directory
    that links to it.

    An earlier version of this finding was withdrawn because it was measured
    through the Python API with `cache_root` set to the scan root, which collapses
    the two paths. The pipeline does not do that. Test the invocation that ships.
    """

    # Scanning is tested against an explicit set. graphify is this library's
    # optional dependency, so binding these tests to its real table would make
    # them pass or fail on whether an unrelated package happens to be installed
    # - which is exactly how they first went green locally and red in CI's
    # default-install job.
    SUFFIXES = {".tf", ".py"}

    def _corpus(self, real: str, links: dict[str, str]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        target = root / real
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        for link, dest in links.items():
            path = root / link
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(root / dest)
        return root

    def test_it_counts_symlinked_extractable_files_by_repository(self):
        root = self._corpus("repo-a/main.tf", {"repo-a/env/live/main.tf": "repo-a/main.tf"})
        found = status.duplicating_symlinks(root, self.SUFFIXES)
        self.assertTrue(found["checked"])
        self.assertEqual(found["files"], 1)
        self.assertEqual(found["by_repo"], {"repo-a": 1})

    def _reported(self, by_repo: dict) -> str:
        """The symlink report for a given scan result.

        Stubs the scan: the defect being pinned is in how the result is worded,
        and driving the real scan would tie a formatting test to whether an
        optional dependency happens to be installed.
        """
        self.addCleanup(setattr, status, "duplicating_symlinks", status.duplicating_symlinks)
        status.duplicating_symlinks = lambda *a, **k: {
            "checked": True,
            "files": sum(by_repo.values()),
            "by_repo": by_repo,
        }
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status._report_symlinks()
        return out.getvalue()

    def test_one_repository_is_reported_without_repeating_its_count(self):
        """A released version printed "60 in cpp-terraform-azurerm-idam (60)"."""
        text = self._reported({"repo-a": 60})
        self.assertIn("60 in repo-a.", text)
        self.assertNotIn("(60)", text, "the per-repository count repeats the total")

    def test_several_repositories_keep_their_per_repository_counts(self):
        """With more than one, the breakdown is the whole point."""
        text = self._reported({"repo-a": 40, "repo-b": 20})
        self.assertIn("60 in repo-a (40), repo-b (20).", text)

    def test_the_target_itself_is_not_counted(self):
        """Only the link duplicates; the real file was always going to be read."""
        root = self._corpus("repo-a/main.tf", {})
        self.assertEqual(status.duplicating_symlinks(root, self.SUFFIXES)["files"], 0)

    def test_a_symlink_nothing_extracts_is_ignored(self):
        root = self._corpus(
            "repo-a/notes.unknownext", {"repo-a/copy.unknownext": "repo-a/notes.unknownext"}
        )
        self.assertEqual(status.duplicating_symlinks(root, self.SUFFIXES)["files"], 0)

    def test_the_real_dispatch_table_is_readable_when_graphify_is_installed(self):
        """The default path, which the injected sets above deliberately bypass."""
        suffixes = status.extractable_suffixes()
        if suffixes is None:
            self.skipTest("graphify is not installed; the default install cannot check")
        self.assertIn(".py", suffixes)

    def test_an_unreadable_dispatch_table_reports_cannot_check(self):
        """The failure mode that matters: a private upstream table gets renamed and
        a naive check silently reports zero, which reads as a clean estate."""
        real = status.extractable_suffixes
        self.addCleanup(setattr, status, "extractable_suffixes", real)
        status.extractable_suffixes = lambda: None
        found = status.duplicating_symlinks(Path("/anything"))
        self.assertFalse(found["checked"])
        self.assertNotIn("files", found, "a skipped check must not report a count of zero")


if __name__ == "__main__":
    unittest.main()
