"""Git calls read the repository they name, even when run from inside a git hook.

A hook runs with `GIT_DIR`, `GIT_INDEX_FILE` and their kin exported, and those
outrank `git -C <path>`. A store's pre-commit hook running a library stage would
then point every `git -C <clone>` at the store's own repository: a fixture commit
landed on a store's real branch that way.

The fixtures are real repositories, because the claim is about what git does with
the environment it is handed - a stubbed runner would only prove the stub was
called. Each case sets `GIT_DIR` (and the index and work tree) to a decoy
repository, then asks a stage's own git call about a different one.
"""

from __future__ import annotations

import ast
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import (
    check_citations,
    drift,
    export_git_history,
    git_version,
    provenance,
    status,
    sync_repositories,
)
from knowledgestore.git_env import BINDING, KEPT, git_env

SOURCE = Path(__file__).resolve().parent.parent / "src"


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
        # The fixtures' own git runs outside any binding, independently of the
        # helper under test, so an assertion cannot inherit its defect.
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )
    return completed.stdout.strip()


def _repository(root: Path, name: str, filename: str) -> Path:
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / filename).write_text(name + "\n", encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", f"{name} first")
    return repo


class AHookBindingDoesNotRedirectAStage(SettingsIsolated):
    """Each stage's git call, run under a decoy `GIT_DIR`, answers about its clone."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.clone = _repository(root, "clone", "clone.txt")
        self.first = _git(self.clone, "rev-parse", "HEAD")
        (self.clone / "second.txt").write_text("two\n", encoding="utf-8")
        _git(self.clone, "add", "second.txt")
        _git(self.clone, "commit", "-q", "-m", "clone second")
        self.head = _git(self.clone, "rev-parse", "HEAD")
        self.decoy = _repository(root, "decoy", "decoy.txt")
        self.decoy_head = _git(self.decoy, "rev-parse", "HEAD")
        hooked = {
            "GIT_DIR": str(self.decoy / ".git"),
            "GIT_WORK_TREE": str(self.decoy),
            "GIT_INDEX_FILE": str(self.decoy / ".git" / "index"),
        }
        patcher = mock.patch.dict(os.environ, hooked)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_decoy_binding_really_redirects_an_unguarded_git(self):
        # The premise, asserted rather than assumed: without the stripped
        # environment, `git -C clone` answers with the decoy's commit.
        seen = subprocess.run(
            ["git", "-C", str(self.clone), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(seen, self.decoy_head)

    def test_export_history_reads_the_clone(self):
        self.assertEqual(
            export_git_history.run_git(self.clone, "rev-parse", "HEAD").strip(), self.head
        )

    def test_export_history_verifies_a_ref_against_the_clone(self):
        self.assertTrue(export_git_history._verified(self.clone, self.first))

    def test_sync_reads_the_clone(self):
        self.assertEqual(
            sync_repositories.run_git(["-C", str(self.clone), "rev-parse", "HEAD"]).strip(),
            self.head,
        )
        held = sync_repositories.run_git(
            ["-C", str(self.clone), "rev-parse", "HEAD"], capture_stderr=True
        )
        self.assertEqual(held.strip(), self.head)

    def test_a_write_through_sync_lands_in_the_clone_and_not_the_hooked_repository(self):
        sync_repositories.run_git(
            [
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "-C",
                str(self.clone),
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "fixture",
            ]
        )
        self.assertEqual(_git(self.decoy, "rev-parse", "HEAD"), self.decoy_head)
        self.assertEqual(_git(self.clone, "log", "-1", "--format=%s"), "fixture")

    def test_provenance_records_the_clone_head(self):
        self.assertEqual(provenance.head_info(self.clone, "main")["sha"], self.head)

    def test_drift_diffs_the_clone(self):
        changes, complaint = drift.name_status(self.clone, self.first, self.head)
        self.assertEqual((changes, complaint), ([("A", "second.txt")], ""))

    def test_status_reads_the_clone(self):
        self.assertEqual(
            status.run_git(["-C", str(self.clone), "rev-parse", "HEAD"]).strip(), self.head
        )

    def test_git_version_runs_against_the_clone(self):
        self.assertEqual(
            git_version.run_git(["-C", str(self.clone), "rev-parse", "HEAD"]).strip(), self.head
        )

    def test_check_citations_asks_the_clone(self):
        vocabulary = check_citations.Vocabulary(clones=self.clone.parent)
        self.assertEqual(
            vocabulary.clone_holds("clone", self.head, "second.txt"), check_citations.HELD
        )
        self.assertEqual(
            vocabulary.clone_holds("clone", self.head, "decoy.txt"), check_citations.ABSENT
        )


class TheStrippedEnvironment(unittest.TestCase):
    def test_binding_variables_go_and_configuration_and_credentials_stay(self):
        kept = {
            "GIT_SSH_COMMAND": "ssh -i key",
            "GIT_ASKPASS": "/bin/askpass",
            "GIT_CONFIG_PARAMETERS": "'core.x'='y'",
            "GIT_CONFIG_COUNT": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": "/home/someone",
        }
        bound = {name: "/somewhere" for name in BINDING}
        with mock.patch.dict(os.environ, {**kept, **bound}):
            env = git_env()
            self.assertEqual({name for name in BINDING if name in env}, set())
            self.assertEqual({k: env.get(k) for k in kept}, kept)
            # A copy: the process's own environment is untouched.
            self.assertEqual(os.environ["GIT_DIR"], "/somewhere")

    def test_every_variable_git_calls_local_is_stripped_or_deliberately_kept(self):
        # `--local-env-vars` is git's own list of what binds a repository. A
        # newer git adding one fails this test rather than passing it through.
        listed = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True
        ).stdout.split()
        self.assertGreater(len(listed), 5, "git printed no list; the check would be vacuous")
        self.assertEqual(sorted(set(listed) - set(BINDING) - set(KEPT)), [])
        self.assertEqual(set(BINDING) & set(KEPT), set())


SUBPROCESS_CALLS = frozenset({"run", "Popen", "call", "check_call", "check_output"})


def _is_git_argv(node: ast.expr, assigned: dict[str, ast.expr]) -> bool:
    if isinstance(node, ast.Name) and node.id in assigned:
        node = assigned[node.id]
    return (
        isinstance(node, ast.List)
        and bool(node.elts)
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value == "git"
    )


def _passes_git_env(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "env" and isinstance(keyword.value, ast.Call):
            func = keyword.value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            return name == "git_env"
    return False


def git_calls(source: str) -> list[tuple[int, bool]]:
    """(line, passes `env=git_env()`) for every subprocess call that runs git."""
    tree = ast.parse(source)
    found: list[tuple[int, bool]] = []
    scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in [tree, *scopes]:
        assigned = {
            target.id: node.value
            for node in ast.walk(scope)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        body = scope.body if isinstance(scope, ast.Module) else [scope]
        for node in (n for part in body for n in ast.walk(part)):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr in SUBPROCESS_CALLS
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ):
                continue
            if _is_git_argv(node.args[0], assigned):
                found.append((node.lineno, _passes_git_env(node)))
    return sorted(set(found))


class EveryGitCallPassesTheStrippedEnvironment(unittest.TestCase):
    """The structural half: a new call site cannot forget `env=git_env()`."""

    def test_the_walk_tells_a_guarded_call_from_an_unguarded_one(self):
        # Sensitivity, in the same run (CLAUDE.md 2.8): both argv shapes the
        # library uses, with and without the environment.
        source = (
            "import subprocess\n"
            "def a(p):\n"
            "    command = ['git', '-C', p]\n"
            "    subprocess.run(command, check=True)\n"
            "def b(p):\n"
            "    subprocess.run(['git', '-C', p], env=git_env())\n"
            "def c(p):\n"
            "    subprocess.run(['gh', 'auth'])\n"
        )
        self.assertEqual(git_calls(source), [(4, False), (6, True)])

    def test_no_git_call_in_the_library_inherits_the_callers_binding(self):
        found = {}
        for path in sorted(SOURCE.rglob("*.py")):
            for line, guarded in git_calls(path.read_text(encoding="utf-8")):
                found[f"{path.relative_to(SOURCE)}:{line}"] = guarded
        # Seven call sites today; fewer means the walk stopped seeing them.
        self.assertGreaterEqual(len(found), 7, sorted(found))
        self.assertEqual(sorted(site for site, guarded in found.items() if not guarded), [])


if __name__ == "__main__":
    unittest.main()
