"""Tests for knowledgestore/export_git_history.py - the history-export contract."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import export_git_history as export  # noqa: E402


class ParseNumstatTest(SettingsIsolated):
    def test_parses_additions_deletions_and_binary(self):
        output = "3\t1\tsrc/a.ts\n-\t-\tlogo.png\n"
        files = export.parse_numstat(output)
        self.assertEqual(
            files[0],
            {
                "path": "src/a.ts",
                "additions": 3,
                "deletions": 1,
                "binary": False,
            },
        )
        self.assertTrue(files[1]["binary"])
        self.assertIsNone(files[1]["additions"])

    def test_ignores_malformed_lines(self):
        self.assertEqual(export.parse_numstat("not-a-numstat-line\n\n"), [])


class NormaliseMessageTest(SettingsIsolated):
    def test_collapses_blank_runs_and_line_endings(self):
        self.assertEqual(
            export.normalise_message("a\r\n\n\n\nb\r"),
            "a\n\nb",
        )


class ConfigTest(SettingsIsolated):
    def test_reads_valid_lines_and_skips_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "repos.txt"
            cfg.write_text(
                "# comment\n\nrepo-a|git@example.com:o/repo-a.git|main\n",
                encoding="utf-8",
            )
            repos = export.read_repository_config(cfg)
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0].name, "repo-a")
        self.assertEqual(repos[0].default_branch, "main")

    def test_rejects_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "repos.txt"
            cfg.write_text("only-a-name|missing-branch\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                export.read_repository_config(cfg)


class GetCommitsIntegrationTest(SettingsIsolated):
    """Regression guard for the \\x1f field-separator bug: commits with an
    EMPTY body were silently dropped because str.strip() treats the field
    separator as whitespace. Every commit must survive the round trip."""

    def _git(self, cwd, *args):
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_empty_body_commits_are_not_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init", "-q", "-b", "main")
            self._git(repo, "config", "user.email", "t@example.com")
            self._git(repo, "config", "user.name", "Test")
            (repo / "a.txt").write_text("one\n")
            self._git(repo, "add", "a.txt")
            # no body - the historical failure case
            self._git(repo, "commit", "-q", "-m", "ABC-1: subject only")
            (repo / "a.txt").write_text("two\n")
            self._git(repo, "add", "a.txt")
            self._git(repo, "commit", "-q", "-m", "second subject", "-m", "with a body paragraph")

            commits = list(export.get_commits(repo))

        self.assertEqual(len(commits), 2, "a commit was dropped during parsing")
        by_subject = {c["subject"]: c for c in commits}
        self.assertEqual(by_subject["ABC-1: subject only"]["body"], "")
        self.assertEqual(by_subject["second subject"]["body"], "with a body paragraph")
        self.assertEqual(by_subject["second subject"]["files"][0]["path"], "a.txt")
        self.assertFalse(by_subject["second subject"]["is_merge"])


class NumstatSinglePassTest(SettingsIsolated):
    """File statistics must come from the one `git log` pass, unchanged.

    `get_commits` used to spawn `git show --numstat` per commit — on a real
    estate that was 195,360 extra processes, which is why the stage took over an
    hour while barely using a core. The data now rides along with the single log
    pass, and this pins equivalence against the old per-commit call so the
    optimisation cannot quietly change the dataset.
    """

    def _git(self, cwd, *args):
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _repo(self, repo: Path) -> Path:
        self._git(repo, "init", "-q", "-b", "main")
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "Test")
        (repo / "a.txt").write_text("one\ntwo\nthree\n")
        self._git(repo, "add", "a.txt")
        self._git(repo, "commit", "-q", "-m", "first")
        # a body containing blank lines, which the record parsing must survive
        (repo / "a.txt").write_text("one\nchanged\nthree\nfour\n")
        self._git(repo, "add", "a.txt")
        self._git(repo, "commit", "-q", "-m", "second", "-m", "para one\n\npara two")
        # binary content: numstat reports "-\t-"
        (repo / "b.bin").write_bytes(bytes(range(256)))
        self._git(repo, "add", "b.bin")
        self._git(repo, "commit", "-q", "-m", "binary added")
        # a rename, which is why the per-commit call passed --find-renames
        self._git(repo, "mv", "a.txt", "renamed.txt")
        self._git(repo, "commit", "-q", "-m", "renamed")
        # a merge, whose numstat is empty under both implementations
        self._git(repo, "checkout", "-q", "-b", "side")
        (repo / "c.txt").write_text("side\n")
        self._git(repo, "add", "c.txt")
        self._git(repo, "commit", "-q", "-m", "on the side")
        self._git(repo, "checkout", "-q", "main")
        self._git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge side")
        return repo

    def test_file_data_matches_the_per_commit_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            commits = list(export.get_commits(repo))
            reference = {c["sha"]: export.get_commit_files(repo, c["sha"]) for c in commits}

        self.assertEqual(len(commits), 6, "every commit parsed")
        for commit in commits:
            self.assertEqual(
                commit["files"],
                reference[commit["sha"]],
                f"file data diverged for {commit['subject']!r}",
            )

    def test_totals_and_awkward_cases_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            commits = {c["subject"]: c for c in export.get_commits(self._repo(Path(tmp)))}

        binary = commits["binary added"]["files"][0]
        self.assertTrue(binary["binary"], "a binary file has no line counts")
        self.assertEqual(commits["binary added"]["additions"], 0)

        second = commits["second"]
        self.assertEqual(second["body"], "para one\n\npara two", "blank lines in a body survive")
        self.assertEqual(second["additions"], 2)
        self.assertEqual(second["deletions"], 1)
        self.assertEqual(second["file_count"], 1)

        # A merge does carry file data, and keeping it is the whole reason for
        # --diff-merges=cc: plain `git log --numstat` reports nothing for merges,
        # so the naive single-pass rewrite would have silently emptied every
        # merge commit in the dataset.
        merge = commits["merge side"]
        self.assertTrue(merge["is_merge"])
        self.assertEqual([f["path"] for f in merge["files"]], ["c.txt"])


class RenderingContractTest(SettingsIsolated):
    """The markdown/ndjson output shapes are the dataset contract."""

    def _commit(self):
        return {
            "sha": "a" * 40,
            "short_sha": "a" * 12,
            "parents": [],
            "author_date": "2024-05-01T10:00:00+00:00",
            "committer_date": "2024-05-01T10:00:00+00:00",
            "author": {"name": "Dev|One", "email": "d@example.com"},
            "committer": {"name": "Dev|One", "email": "d@example.com"},
            "refs": [],
            "subject": "DD-1: add thing",
            "body": "why it changed",
            "files": [{"path": "a.ts", "additions": 3, "deletions": 1, "binary": False}],
            "file_count": 1,
            "additions": 3,
            "deletions": 1,
            "is_merge": False,
        }

    def _repo_config(self):
        return export.RepositoryConfig(
            name="repo-a", clone_url="git@example.com:o/repo-a.git", default_branch="main"
        )

    def test_commit_markdown_includes_metadata_and_escapes_pipes(self):
        md = export.commit_markdown(self._repo_config(), self._commit())
        self.assertIn("DD-1: add thing", md)
        self.assertIn("Dev\\|One", md)  # markdown-table escaping
        self.assertIn("**Files changed:** 1", md)
        self.assertIn("+3 / -1", md)
        self.assertIn("why it changed", md)

    def test_write_ndjson_stamps_repository_fields(self):
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "commits.ndjson"
            export.write_ndjson(out, self._repo_config(), [self._commit()])
            record = _json.loads(out.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["repository"], "repo-a")
        self.assertEqual(record["repository_url"], "git@example.com:o/repo-a.git")
        self.assertEqual(record["subject"], "DD-1: add thing")

    def test_write_year_files_groups_by_author_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            counts = export.write_year_files(Path(tmp), self._repo_config(), [self._commit()])
            self.assertEqual(counts, {"2024": 1})
            year_md = (Path(tmp) / "2024.md").read_text(encoding="utf-8")
        self.assertIn("Git history for 2024", year_md)
        self.assertIn("intentionally excludes complete patches", year_md)


class ExportRepositoryEndToEndTest(SettingsIsolated):
    """Whole-stage integration over a real fixture repository."""

    def _git(self, cwd, *args):
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_export_repository_produces_dataset_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repositories" / "repo-a"
            repo.mkdir(parents=True)
            self._git(repo, "init", "-q", "-b", "main")
            self._git(repo, "config", "user.email", "t@example.com")
            self._git(repo, "config", "user.name", "Test")
            (repo / "a.txt").write_text("one\n")
            self._git(repo, "add", "a.txt")
            self._git(repo, "commit", "-q", "-m", "DD-1: first")

            out_root = root / "out"
            export.export_repository(
                root,
                out_root,
                export.RepositoryConfig(
                    name="repo-a", clone_url="git@example.com:o/repo-a.git", default_branch="main"
                ),
            )

            produced = sorted(f.name for f in (out_root / "repo-a").iterdir())
            index = (out_root / "repo-a" / "index.md").read_text(encoding="utf-8")

        self.assertIn("commits.ndjson", produced)
        self.assertIn("index.md", produced)
        self.assertTrue(
            any(name.endswith(".md") and name[:4].isdigit() for name in produced), produced
        )
        self.assertIn("**Total commits:** 1", index)
        # The fixture has no remote, so the configured branch only resolves locally.
        self.assertIn(
            "**History scope:** default branch `main`: 1 commits; tags only: 0; branches only: 0 across 0 branches",
            index,
        )


class ReachTest(SettingsIsolated):
    """Which commits landed, read against the configured default branch.

    The fixture is a real clone of a real origin: a merged branch, a squash, a
    release line reached only by a tag, an unmerged long-lived branch, and a
    remote default (`origin/HEAD`) that points at that unmerged branch, as some
    repositories' do.
    """

    def _git(self, cwd, *args):
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _commit(self, repo, name, text, message):
        (repo / name).write_text(text)
        self._git(repo, "add", name)
        self._git(repo, "commit", "-q", "-m", message)

    def _fixture(self, root: Path) -> Path:
        origin = root / "origin"
        origin.mkdir()
        g = lambda *a: self._git(origin, *a)  # noqa: E731
        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@example.com")
        g("config", "user.name", "Test")
        self._commit(origin, "a.txt", "1\n", "AA-1: base")
        g("checkout", "-q", "-b", "feature/merged")
        self._commit(origin, "b.txt", "1\n", "AA-2: merged work")
        g("checkout", "-q", "main")
        g("merge", "-q", "--no-ff", "-m", "Merge feature/merged", "feature/merged")
        g("checkout", "-q", "-b", "feature/squash")
        self._commit(origin, "c.txt", "1\n", "AA-3: squash work")
        g("checkout", "-q", "main")
        g("merge", "-q", "--squash", "feature/squash")
        g("commit", "-q", "-m", "tidy c")
        g("checkout", "-q", "-b", "release/1")
        self._commit(origin, "d.txt", "1\n", "AA-4: release fix")
        g("tag", "v1")
        g("checkout", "-q", "main")
        g("checkout", "-q", "-b", "team/proposal")
        self._commit(origin, "a.txt", "2\n", "AA-5: proposal")
        g("checkout", "-q", "main")
        work = root / "work"
        self._git(root, "clone", "-q", str(origin), str(work))
        # The remote default names the unmerged branch, as it does in some estates.
        self._git(work, "remote", "set-head", "origin", "team/proposal")
        return work

    def _by_subject(self, repo, default_branch="main"):
        return {c["subject"]: c for c in export.get_commits(repo, default_branch)}

    def test_each_commit_is_classed_against_the_configured_branch(self):
        """Catches reach read from origin/HEAD, which here names the unmerged
        branch: the proposal would read as landed and the base as a branch."""
        with tempfile.TemporaryDirectory() as tmp:
            commits = self._by_subject(self._fixture(Path(tmp)))
        self.assertEqual(commits["AA-1: base"]["reach"], "default")
        self.assertEqual(commits["AA-2: merged work"]["reach"], "default")
        self.assertEqual(commits["tidy c"]["reach"], "default")
        self.assertEqual(commits["AA-5: proposal"]["reach"], "branch")
        self.assertEqual(commits["AA-5: proposal"]["ref"], "team/proposal")
        self.assertNotIn("ref", commits["AA-1: base"])

    def test_a_release_line_reached_only_by_a_tag_is_tag(self):
        """Catches tag-only commits classed as branch: shipped work would read as
        a proposal."""
        with tempfile.TemporaryDirectory() as tmp:
            commits = self._by_subject(self._fixture(Path(tmp)))
        self.assertEqual(commits["AA-4: release fix"]["reach"], "tag")
        self.assertNotIn("ref", commits["AA-4: release fix"])

    def test_squashed_branch_commits_are_branch_and_name_their_branch(self):
        """The squash's own commits never reach the default branch; the squash
        commit does. Catches a ref left as the full refname."""
        with tempfile.TemporaryDirectory() as tmp:
            commits = self._by_subject(self._fixture(Path(tmp)))
        self.assertEqual(commits["AA-3: squash work"]["reach"], "branch")
        self.assertEqual(commits["AA-3: squash work"]["ref"], "feature/squash")

    def test_a_missing_configured_branch_falls_back_to_the_remote_default_and_says_so(self):
        """Catches a stale repositories.txt silently classing everything as branch."""
        with tempfile.TemporaryDirectory() as tmp:
            work = self._fixture(Path(tmp))
            self._git(work, "remote", "set-head", "origin", "main")
            ref, warning = export.resolve_default(work, "trunk")
            commits = self._by_subject(work, "trunk")
        self.assertEqual(ref, "refs/remotes/origin/HEAD")
        self.assertIn("trunk", warning)
        self.assertEqual(commits["AA-1: base"]["reach"], "default")

    def test_a_branch_that_exists_only_locally_still_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self._fixture(Path(tmp))
            self._git(work, "branch", "local-only", "origin/team/proposal")
            ref, warning = export.resolve_default(work, "local-only")
        self.assertEqual(ref, "refs/heads/local-only")
        self.assertIsNone(warning)

    def test_a_repository_with_no_remote_classes_everything_as_branch(self):
        """Catches a crash on a local-only clone, where nothing resolves."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init", "-q", "-b", "main")
            self._git(repo, "config", "user.email", "t@example.com")
            self._git(repo, "config", "user.name", "Test")
            self._commit(repo, "a.txt", "1\n", "AA-1: only")
            ref, warning = export.resolve_default(repo, "trunk")
            commits = list(export.get_commits(repo, "trunk"))
        self.assertIsNone(ref)
        self.assertIn("trunk", warning)
        self.assertEqual([c["reach"] for c in commits], ["branch"])

    def test_two_exports_of_one_repository_are_identical(self):
        """Catches non-determinism in which ref --source names (CLAUDE.md 1.3)."""
        with tempfile.TemporaryDirectory() as tmp:
            work = self._fixture(Path(tmp))
            first = list(export.get_commits(work, "main"))
            second = list(export.get_commits(work, "main"))
        self.assertEqual(first, second)

    def test_branch_name_strips_ref_prefixes(self):
        self.assertEqual(export.branch_name("refs/remotes/origin/team/x"), "team/x")
        self.assertEqual(export.branch_name("refs/heads/team/x"), "team/x")
        self.assertEqual(export.branch_name("refs/stash"), "refs/stash")


if __name__ == "__main__":
    unittest.main()
