"""What `sync` destroys and what it preserves, asserted rather than described.

`docs/building-a-knowledge-store.md` makes two claims about one line of `sync`,
and they are opposite consequences of the same flag:

- a `.graphifyignore` placed at a cloned repository's root **does not survive**,
  so a per-repository exclusion is a build step to re-apply and never the source
  of truth;
- `graphify-out/` **does** survive, which is why anything extracted once - a
  secret-bearing file included - is durable by design and cannot be recalled by
  filtering the published graph.

An existing test asserts the flag appears in the command. That is not the same
claim: it would still pass if the flag were spelled in a way git treats
differently. These run the real thing against a real repository and assert the
outcome, because the guide advises operators on the basis of the outcome.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledgestore import sync_repositories as sync  # noqa: E402


class CleanSideEffectsTest(unittest.TestCase):
    def _cleaned_repo(self) -> Path:
        """A repository carrying untracked files, after sync's own clean."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "repo-a"
        (repo / "graphify-out").mkdir(parents=True)
        (repo / "src").mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=t@e",
                "-c",
                "user.name=t",
                "commit",
                "-qm",
                "init",
            ],
            check=True,
        )
        (repo / ".graphifyignore").write_text("stacks/\n", encoding="utf-8")
        (repo / "graphify-out" / "graph.json").write_text("{}", encoding="utf-8")
        (repo / "src" / "scratch.py").write_text("x = 1\n", encoding="utf-8")

        # The exact invocation sync ends with.
        sync.run_git(["-C", str(repo), "clean", "-fd", "-e", "graphify-out"])
        return repo

    def test_a_repository_root_graphifyignore_does_not_survive(self):
        """Guards: "a `.graphifyignore` inside a cloned repository does not
        survive `sync`" - the placement the guide recommends for excluding
        symlinked sources, which one estate is relying on."""
        repo = self._cleaned_repo()
        self.assertFalse(
            (repo / ".graphifyignore").exists(),
            "if this survives, the guide's re-apply-every-sync advice is wrong",
        )

    def test_the_per_repository_graph_does_survive(self):
        """Guards: "`graphify-out/` is the one directory a sync deliberately
        preserves" - the reason extracted content is durable, and the reason
        filtering a secret out of the published graph is too late."""
        repo = self._cleaned_repo()
        self.assertTrue((repo / "graphify-out" / "graph.json").exists())

    def test_other_untracked_files_are_removed(self):
        """The exemption is specific: without this, "graphify-out is the one
        directory preserved" would be true only vacuously."""
        repo = self._cleaned_repo()
        self.assertFalse((repo / "src" / "scratch.py").exists())

    def test_tracked_files_are_untouched(self):
        repo = self._cleaned_repo()
        self.assertTrue((repo / "tracked.txt").exists())


if __name__ == "__main__":
    unittest.main()
