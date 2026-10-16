"""Branches differing only in case must not cost a repository its sync (#437).

On a case-insensitive filesystem - the macOS default - the `files` ref backend
keeps one file per ref, so `origin/abc-1` and `origin/ABC-1` are the same file.
The full-refspec fetch then fails with "unable to update local ref" and the
repository is reported as failed. Its default branch is fine, but every branch
that never arrives is missing from the history export and from ticket reach.

The `reftable` backend stores refs in a table, so casing stops mattering. `sync`
now recognises the symptom, confirms it against the remote's own branch list,
migrates that one clone and retries the fetch once. Where it cannot - git too old,
or a clone already on reftable - it fails naming the colliding branches.

The decision logic is tested through the `run=` seam against a fake git that
models the one fact that matters: a `files` clone cannot hold both branches, a
`reftable` one can. The integration test uses real git throughout.
"""

from __future__ import annotations

import contextlib
import functools
import io
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import export_git_history as export  # noqa: E402
from knowledgestore import sync_repositories as sync  # noqa: E402

# What git 2.55 prints on macOS for this fetch, abridged to the line `sync` reads.
COLLISION_STDERR = (
    "From example.invalid:o/repo-a\n"
    " ! da0b37a..f9d126e  abc-1      -> origin/abc-1  (unable to update local ref)\n"
)
REMOTE_BRANCHES = (
    "1111111111111111111111111111111111111111\trefs/heads/ABC-1\n"
    "2222222222222222222222222222222222222222\trefs/heads/abc-1\n"
    "3333333333333333333333333333333333333333\trefs/heads/main\n"
)


class FakeGit:
    """git as `sync_repository` sees it, with a ref store that has a format.

    The fetch fails while the clone stores refs as `files` and the remote holds
    branches differing only in case, and succeeds once the clone is `reftable` -
    which is the behaviour the fix depends on, observed on a real clone.
    """

    def __init__(
        self,
        version: str = "git version 2.55.0",
        ref_format: str = "files",
        fetch_stderr: str = COLLISION_STDERR,
        remote_branches: str = REMOTE_BRANCHES,
        reftable_fixes_it: bool = True,
    ) -> None:
        self.version = version
        self.ref_format = ref_format
        self.fetch_stderr = fetch_stderr
        self.remote_branches = remote_branches
        self.reftable_fixes_it = reftable_fixes_it
        self.fetched = False

    def __call__(self, args: list[str], capture_stderr: bool = False) -> str:
        if args == ["--version"]:
            return self.version + "\n"
        command = args[2:] if args[:1] == ["-C"] else args
        if command[0] == "fetch":
            if self.ref_format == "reftable" and self.reftable_fixes_it:
                self.fetched = True
                return ""
            raise subprocess.CalledProcessError(1, args, "", self.fetch_stderr)
        if command[:2] == ["refs", "migrate"]:
            self.ref_format = "reftable"
            return ""
        if command == ["rev-parse", "--show-ref-format"]:
            return self.ref_format + "\n"
        if command[:2] == ["ls-remote", "--heads"]:
            return self.remote_branches
        if command[-2:] == ["--all", "--count"]:
            return "3\n"
        return ""


class CaseCollisionDecisionTest(unittest.TestCase):
    def _sync(self, git: FakeGit) -> tuple[int, str]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "repo-a" / ".git").mkdir(parents=True)
        repo = export.RepositoryConfig(
            name="repo-a", clone_url="git@example.invalid:o/repo-a.git", default_branch="main"
        )
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            count = sync.sync_repository(repo, Path(tmp.name), run=git)
        return count, printed.getvalue()

    def test_a_collision_on_current_git_migrates_the_clone_and_syncs(self):
        """Breaks if the symptom is not recognised, the migration is not run, or
        the fetch is not retried: the repository then fails as it did before."""
        git = FakeGit()
        count, printed = self._sync(git)
        self.assertEqual(count, 3)
        self.assertEqual(git.ref_format, "reftable")
        self.assertTrue(git.fetched, "the fetch after the migration must have run")
        self.assertIn("repo-a", printed, "the migration is announced, naming the repository")
        self.assertIn("reftable", printed)

    def test_a_cannot_lock_ref_symptom_is_recognised_too(self):
        """Breaks if only one of git's two wordings for the collision is read."""
        git = FakeGit(
            fetch_stderr="error: cannot lock ref 'refs/remotes/origin/abc-1': "
            "reference already exists\n"
        )
        self._sync(git)
        self.assertEqual(git.ref_format, "reftable")

    def test_git_too_old_to_migrate_names_the_branches_and_the_command(self):
        """Breaks if an old git attempts the migration - which refuses any clone
        holding reflogs before 2.48 - or if the message leaves the operator
        without the branches or the fix."""
        git = FakeGit(version="git version 2.47.1 (Apple Git-150)")
        with self.assertRaises(RuntimeError) as caught:
            self._sync(git)
        message = str(caught.exception)
        self.assertIn("abc-1", message)
        self.assertIn("ABC-1", message)
        self.assertIn("refs migrate --ref-format=reftable", message)
        self.assertNotIn("main", message.split("(")[1].split(")")[0])
        self.assertEqual(git.ref_format, "files", "an old git must not be asked to migrate")

    def test_a_clone_already_on_reftable_is_not_migrated_again(self):
        """Breaks if the clone is re-migrated in a loop rather than reported."""
        git = FakeGit(ref_format="reftable", reftable_fixes_it=False)
        with self.assertRaises(RuntimeError) as caught:
            self._sync(git)
        message = str(caught.exception)
        self.assertIn("abc-1", message)
        self.assertIn("ABC-1", message)
        self.assertIn("already", message)

    def test_an_unrelated_fetch_failure_behaves_as_before(self):
        """Breaks if any fetch failure triggers a migration: a remote that cannot
        be reached must fail exactly as it always did, clone untouched."""
        git = FakeGit(fetch_stderr="fatal: Could not read from remote repository.\n")
        with self.assertRaises(subprocess.CalledProcessError):
            self._sync(git)
        self.assertEqual(git.ref_format, "files")

    def test_a_lock_failure_without_a_case_collision_is_not_migrated(self):
        """Breaks if the stderr wording alone decides. `cannot lock ref` is also
        what a branch named like a directory of another one produces, and the
        migration would rewrite a clone without fixing it."""
        git = FakeGit(
            fetch_stderr="error: cannot lock ref 'refs/remotes/origin/team/x': "
            "'refs/remotes/origin/team' exists\n",
            remote_branches=(
                "1111111111111111111111111111111111111111\trefs/heads/team/x\n"
                "3333333333333333333333333333333333333333\trefs/heads/main\n"
            ),
        )
        with self.assertRaises(subprocess.CalledProcessError):
            self._sync(git)
        self.assertEqual(git.ref_format, "files")

    def test_a_retry_that_still_fails_fails_the_repository(self):
        """Breaks if a failed retry is swallowed, or retried without end."""
        git = FakeGit(reftable_fixes_it=False)
        with self.assertRaises(subprocess.CalledProcessError):
            self._sync(git)
        self.assertEqual(git.ref_format, "reftable")
        self.assertFalse(git.fetched)


class FailedRetryIsReportedTest(SettingsIsolated):
    def test_a_failed_retry_is_listed_among_the_failed_repositories(self):
        """Breaks if the retry's failure escapes the per-repository isolation in
        `main`, or is reported as success."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "repositories.txt"
            manifest.write_text("repo-a|git@example.invalid:o/repo-a.git|main\n", encoding="utf-8")
            (root / "repositories" / "repo-a" / ".git").mkdir(parents=True)
            config.configure(
                REPOSITORIES_CONFIG=manifest,
                REPOSITORIES_DIR=root / "repositories",
                PROVENANCE_PATH=root / "provenance.json",
                EXTERNAL_CONFIG=root / "absent.txt",
            )
            self.addCleanup(setattr, sync, "sync_repository", sync.sync_repository)
            sync.sync_repository = functools.partial(
                sync.sync_repository, run=FakeGit(reftable_fixes_it=False)
            )
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(io.StringIO()):
                code = sync.main()
        self.assertEqual(code, 1)
        self.assertIn("1 of 1 repositories failed to sync", printed.getvalue())
        self.assertIn("  repo-a:", printed.getvalue())


class CollidingBranchesTest(unittest.TestCase):
    def test_groups_names_equal_once_case_folded_in_a_stable_order(self):
        """Breaks if a group is missed, a lone branch is reported, or the order
        depends on the remote's listing order."""
        listing = (
            "a\trefs/heads/Zed\n"
            "b\trefs/heads/main\n"
            "c\trefs/heads/zed\n"
            "d\trefs/heads/abc-1\n"
            "e\trefs/heads/ABC-1\n"
            "f\trefs/heads/Abc-1\n"
        )
        self.assertEqual(
            sync.case_colliding_branches(listing),
            [["ABC-1", "Abc-1", "abc-1"], ["Zed", "zed"]],
        )


def _git_version() -> tuple[int, int]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match[1]), int(match[2])) if match else (0, 0)


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("", encoding="utf-8")
    return (directory / "caseprobe").exists()


@unittest.skipIf(_git_version() < (2, 45), "a reftable remote needs git 2.45 or later")
class RealGitCaseCollisionTest(unittest.TestCase):
    """The defect end to end, against real git and the real filesystem.

    On a case-insensitive filesystem the second sync reproduces the failure, and
    only the migration lets it through. On a case-sensitive one - every Linux
    runner - the collision cannot happen, so the same assertions hold with the
    clone left on `files`. Either way both branches must arrive.

    Nothing here asserts on what `sync` prints, and nothing may: whatever this
    test observes only where case folds would name different observers on macOS
    and Linux in the mutation table. The announcement belongs to the seam tests.
    """

    def _git(self, *args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def _commit(self, seed: Path, message: str) -> None:
        self._git("-C", str(seed), "commit", "-q", "--allow-empty", "-m", message)

    def test_both_branches_arrive_and_the_graph_survives(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        seed, remote = root / "seed", root / "remote.git"
        # reftable, so the remote can hold both names on any filesystem.
        self._git("init", "-q", "--ref-format=reftable", "-b", "main", str(seed))
        self._commit(seed, "one")
        self._git("-C", str(seed), "branch", "abc-1")
        self._git("-C", str(seed), "branch", "ABC-1")
        self._git("clone", "-q", "--bare", "--ref-format=reftable", str(seed), str(remote))

        repo = export.RepositoryConfig(name="repo-a", clone_url=str(remote), default_branch="main")
        repositories = root / "repositories"
        quiet = contextlib.redirect_stdout(io.StringIO())
        with quiet, contextlib.redirect_stderr(io.StringIO()):
            # The first sync clones, which writes refs packed and does not collide.
            sync.sync_repository(repo, repositories)
        clone = repositories / "repo-a"
        (clone / "graphify-out").mkdir()
        (clone / "graphify-out" / "graph.json").write_text("{}", encoding="utf-8")

        # Both branches move upstream, so the next fetch writes them as loose refs.
        self._commit(seed, "two")
        self._git("-C", str(seed), "branch", "-f", "abc-1")
        self._commit(seed, "three")
        self._git("-C", str(seed), "branch", "-f", "ABC-1")
        self._git(
            "-C", str(seed), "push", "-q", "--force", str(remote), "refs/heads/*:refs/heads/*"
        )

        collides = _case_insensitive(root)
        if collides and _git_version() < sync.REFTABLE_MIGRATE_SINCE:
            self.skipTest("this filesystem collides and this git cannot migrate a clone")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            sync.sync_repository(repo, repositories)

        for branch in ("abc-1", "ABC-1"):
            upstream = self._git("-C", str(remote), "rev-parse", f"refs/heads/{branch}")
            local = self._git("-C", str(clone), "rev-parse", f"refs/remotes/origin/{branch}")
            self.assertEqual(local, upstream, f"origin/{branch} did not arrive")
        self.assertNotEqual(
            self._git("-C", str(clone), "rev-parse", "refs/remotes/origin/abc-1"),
            self._git("-C", str(clone), "rev-parse", "refs/remotes/origin/ABC-1"),
        )
        self.assertEqual((clone / "graphify-out" / "graph.json").read_text(encoding="utf-8"), "{}")

        ref_format = self._git("-C", str(clone), "rev-parse", "--show-ref-format").strip()
        if collides:
            self.assertEqual(ref_format, "reftable", "the collision is only fixed by migrating")
        else:
            self.assertEqual(ref_format, "files", "nothing collided, so nothing is migrated")


if __name__ == "__main__":
    unittest.main()
