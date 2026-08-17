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
        self.assertEqual(found["duplicating"], {"repo-a": 1})

    def _reported(self, by_repo: dict, **extra) -> str:
        """The symlink report for a given scan result.

        Stubs the scan: the defect being pinned is in how the result is worded,
        and driving the real scan would tie a formatting test to whether an
        optional dependency happens to be installed.
        """
        self.addCleanup(setattr, status, "duplicating_symlinks", status.duplicating_symlinks)
        status.duplicating_symlinks = lambda *a, **k: {
            "checked": True,
            "duplicating": by_repo,
            "misattributing": {},
            "broken": {},
            "duplicating_files": sum(by_repo.values()),
            "misattributing_files": 0,
            "broken_files": 0,
            "files": sum(by_repo.values()),
            "excluded": 0,
            "targets": 0,
            "exclusion_checked": True,
            **extra,
        }
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status._report_symlinks()
        return out.getvalue()

    def test_a_missing_corpus_still_reports_totals(self):
        """Every exit must carry the totals the reporter reads. An earlier
        revision returned early without them and raised KeyError on any store
        with no corpus on disk - which the suite caught only with graphify
        installed, since without it the check never gets this far."""
        found = status.duplicating_symlinks(Path("/does/not/exist"), self.SUFFIXES)
        for key in ("files", "duplicating_files", "misattributing_files", "broken_files"):
            self.assertEqual(found[key], 0, key)

    def test_a_target_inside_the_corpus_is_predicted_to_duplicate(self):
        root = self._corpus("repo-a/main.tf", {"repo-a/stacks/main.tf": "repo-a/main.tf"})
        found = status.duplicating_symlinks(root, self.SUFFIXES)
        self.assertEqual(found["duplicating"], {"repo-a": 1})
        self.assertEqual(found["misattributing"], {})

    def test_a_target_outside_the_corpus_is_misattribution_not_duplication(self):
        """Nothing is duplicated, and that is the quieter, worse case: the graph
        records content at a path that is a link, and the real file is absent."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        outside = Path(tmp.name) / "elsewhere.tf"
        outside.write_text("x", encoding="utf-8")
        root = self._corpus("repo-a/main.tf", {})
        (root / "repo-a" / "linked.tf").symlink_to(outside)
        found = status.duplicating_symlinks(root, self.SUFFIXES)
        self.assertEqual(found["misattributing"], {"repo-a": 1})
        self.assertEqual(found["duplicating"], {})

    def test_a_non_extractable_target_is_misattribution(self):
        """The link has an extractable suffix, the target does not, so only the
        link is read - the content is attributed to a path that is not the file."""
        root = self._corpus(
            "repo-a/notes.unknownext", {"repo-a/main.tf": "repo-a/notes.unknownext"}
        )
        found = status.duplicating_symlinks(root, self.SUFFIXES)
        self.assertEqual(found["misattributing"], {"repo-a": 1})

    def test_a_broken_symlink_is_neither_outcome(self):
        root = self._corpus("repo-a/main.tf", {})
        (root / "repo-a" / "dangling.tf").symlink_to(root / "repo-a" / "gone.tf")
        found = status.duplicating_symlinks(root, self.SUFFIXES)
        self.assertEqual(found["broken"], {"repo-a": 1})
        self.assertEqual((found["duplicating"], found["misattributing"]), ({}, {}))

    def test_the_report_names_both_outcomes_not_only_duplication(self):
        """Which outcome a build gets is cache state, not corpus.

        Reproduced by running one extraction three times: run 1 emits two
        distinct `source_file` values, runs 2 and 3 emit one - the symlink -
        because the extraction cache keys on the resolved path and the link
        wins. Reporting only duplication would be wrong for every rebuild
        after the first, which is most of them.
        """
        text = self._reported({"repo-a": 7})
        self.assertIn("COLD build", text)
        self.assertIn(
            "vanish from the graph",
            text,
            "displacement is the quieter outcome and must be named",
        )
        self.assertNotIn(
            "warm cache",
            text,
            "an earlier version implied displacement needed a warm cache; an estate "
            "measured 11 of 12 shared files lost on a cold build",
        )

    def test_one_repository_is_reported_without_repeating_its_count(self):
        """A released version printed "60 in cpp-terraform-azurerm-idam (60)"."""
        text = self._reported({"repo-a": 60})
        self.assertIn("60 in repo-a.", text)
        self.assertNotIn("(60)", text, "the per-repository count repeats the total")

    def test_several_repositories_keep_their_per_repository_counts(self):
        """With more than one, the breakdown is the whole point."""
        text = self._reported({"repo-a": 40, "repo-b": 20})
        self.assertIn("60 in repo-a (40), repo-b (20).", text)

    def test_an_excluded_symlink_is_not_reported_as_exposed(self):
        """The check must be able to see its own mitigation.

        Reported by an operator who had excluded all 60 and verified it in the
        graph, and still got the identical message telling them to exclude them
        before a rebuild. That is the inverse of a silent zero: a permanent
        non-zero that has stopped carrying information, and it reads as
        outstanding work forever.
        """
        root = self._corpus("repo-a/main.tf", {"repo-a/stacks/main.tf": "repo-a/main.tf"})
        (root / "repo-a" / ".graphifyignore").write_text("stacks/\n", encoding="utf-8")
        found = status.duplicating_symlinks(root, self.SUFFIXES)
        if not found["exclusion_checked"]:
            self.skipTest("graphify's ignore helpers are unavailable")
        self.assertEqual(found["excluded"], 1)
        self.assertEqual(found["duplicating"], {})

    def test_a_clean_estate_says_so_rather_than_going_quiet(self):
        """Silence cannot be told apart from a check that stopped running."""
        text = self._reported({}, excluded=60, files=0, duplicating_files=0)
        self.assertIn("60 excluded", text)
        self.assertIn("none exposed", text)

    def test_unreadable_exclusions_are_admitted_not_assumed_absent(self):
        text = self._reported({"repo-a": 1}, exclusion_checked=False)
        self.assertIn("could not be read", text)

    def test_links_sharing_a_target_are_counted_once_as_a_target(self):
        """Drives the real classification, not the stubbed reporter.

        The count of links overstates the files at risk and understates how
        badly each is repeated: several links usually share one target.
        """
        root = self._corpus(
            "repo-a/main.tf",
            {
                "repo-a/one/main.tf": "repo-a/main.tf",
                "repo-a/two/main.tf": "repo-a/main.tf",
                "repo-a/three/main.tf": "repo-a/main.tf",
            },
        )
        found = status.duplicating_symlinks(root, self.SUFFIXES)
        self.assertEqual(found["duplicating"], {"repo-a": 3})
        self.assertEqual(found["targets"], 1, "three links, one file actually at risk")

    def test_the_report_names_distinct_targets_not_just_links(self):
        """60 links to 12 targets is 12 files repeated six times, not 60 files
        duplicated once - a different and more alarming shape."""
        text = self._reported({"repo-a": 60}, targets=12)
        self.assertIn("12 distinct target", text)

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
