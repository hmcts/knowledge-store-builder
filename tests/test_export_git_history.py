"""Tests for knowledgestore/export_git_history.py - the history-export contract."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


from knowledgestore import export_git_history as export  # noqa: E402


class ParseNumstatTest(unittest.TestCase):
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


class NormaliseMessageTest(unittest.TestCase):
    def test_collapses_blank_runs_and_line_endings(self):
        self.assertEqual(
            export.normalise_message("a\r\n\n\n\nb\r"),
            "a\n\nb",
        )


class ConfigTest(unittest.TestCase):
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


class GetCommitsIntegrationTest(unittest.TestCase):
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


class NumstatSinglePassTest(unittest.TestCase):
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


class RenderingContractTest(unittest.TestCase):
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


class ExportRepositoryEndToEndTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
