"""The knowledge-store skill's clone block runs as written, against real git.

Break it catches: a command in the skill that git refuses. The block listed file
paths to `git sparse-checkout set` in the default cone mode, which current git
rejects with "is not a directory", so an asker's first clone stopped before it
fetched anything, and no test ran the block. This runs the block exactly as the
skill prints it, with only its two placeholders filled in.

The freshness, update, reset-check and repair blocks are held the same way (#463):
each is found by its `# skill-block:` line, run under `sh` exactly as the skill
prints it with `<store-directory>` filled in, against throwaway `file://`
stores, and judged by the one line it prints - which is all the reading agent
acts on.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "skills" / "knowledge-store" / "SKILL.md"

# The files a store commits that the block must bring down, and one it must not.
WANTED = (
    "graphify-out/graph.json.gz",
    "graphify-out/GRAPH_REPORT.md",
    "graphify-out/.graphify_labels.json",
    "knowledge/repository-manifest.md",
    "docs/guide.md",
    "knowledge_context.md",
)
UNWANTED = "graphify-out/explorer.html"


def clone_block() -> str:
    text = SKILL.read_text(encoding="utf-8")
    section = text.index("**6. Cloning cheaply.**")
    match = re.search(r"```bash\n(.*?)```", text[section:], re.S)
    if match is None:
        raise AssertionError("no bash block under 'Cloning cheaply' in the skill")
    return match.group(1)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class SkillCloneBlockTest(unittest.TestCase):
    def test_the_clone_block_fetches_the_store_files_and_leaves_the_page_behind(self):
        """Breaks if git refuses any command in the block, or the sparse set
        stops covering a file an agent reads, or starts pulling in the page."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            git(store, "init", "-q", "-b", "main")
            for name in (*WANTED, UNWANTED):
                path = store / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name, encoding="utf-8")
            git(store, "add", *WANTED, UNWANTED)
            git(
                store,
                "-c",
                "user.email=t@example.com",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "-m",
                "store",
            )
            block = clone_block()
            block = block.replace("<store-repository>", store.as_uri())
            block = block.replace("<directory>", str(root / "clone"))
            result = subprocess.run(
                ["bash", "-e", "-c", block], cwd=root, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            clone = root / "clone"
            for name in WANTED:
                self.assertTrue((clone / name).is_file(), f"{name} missing from the sparse clone")
            self.assertFalse((clone / UNWANTED).exists(), "the page was checked out")


def named_block(name: str) -> str:
    """The skill's bash block whose first line is `# skill-block: <name>`."""
    text = SKILL.read_text(encoding="utf-8")
    blocks = [
        body
        for body in re.findall(r"```bash\n(.*?)```", text, re.S)
        if body.startswith(f"# skill-block: {name}\n")
    ]
    if len(blocks) != 1:
        raise AssertionError(f"expected one '{name}' block in the skill, found {len(blocks)}")
    return blocks[0]


AUTHOR = ("-c", "user.email=t@example.com", "-c", "user.name=t")


def commit(repo: Path, message: str, *names: str) -> None:
    """Write each name with content that changes per commit, and commit them."""
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{message} {name}", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, *AUTHOR, "commit", "-q", "-m", message)


def make_store(root: Path, *names: str) -> Path:
    origin = root / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "uploadpack.allowFilter", "true")
    commit(origin, "one", *names)
    return origin


def sparse_clone(origin: Path, target: Path) -> Path:
    """A clone the way the skill's step 6 makes one."""
    block = clone_block().replace("<store-repository>", origin.as_uri())
    block = block.replace("<directory>", str(target))
    subprocess.run(["bash", "-e", "-c", block], cwd=target.parent, check=True, capture_output=True)
    return target


def run_block(name: str, store: Path) -> str:
    """Run a named block as written under `sh`, and return its first stdout line."""
    block = named_block(name).replace("<store-directory>", str(store))
    result = subprocess.run(["sh", "-c", block], capture_output=True, text=True)
    lines = result.stdout.splitlines()
    return lines[0] if lines else ""


def squash(origin: Path) -> None:
    """Rewrite the store's history to one commit, as a squash of main does."""
    git(origin, "checkout", "-q", "--orphan", "squashed")
    git(origin, *AUTHOR, "commit", "-q", "-m", "squashed")
    git(origin, "branch", "-q", "-M", "squashed", "main")


class FreshnessBlockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.origin = make_store(self.root, *WANTED, UNWANTED)
        self.clone = sparse_clone(self.origin, self.root / "clone")

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_untouched_clone_is_current(self):
        """Breaks if the block reports a clone level with its remote as anything else."""
        self.assertEqual(run_block("freshness", self.clone), "FRESHNESS: current")

    def test_a_clone_ahead_of_its_remote_is_current(self):
        """Breaks if a maintainer's clone carrying unpushed work is told it is stale."""
        commit(self.clone, "mine", "knowledge/note.md")
        self.assertEqual(run_block("freshness", self.clone), "FRESHNESS: current")

    def test_a_clone_behind_its_remote_says_by_how_many_commits(self):
        """Breaks if a stale clone reads as current, which is the silence #463 ends."""
        commit(self.origin, "two", "graphify-out/graph.json.gz")
        commit(self.origin, "three", "docs/guide.md")
        self.assertEqual(run_block("freshness", self.clone), "FRESHNESS: BEHIND 2")

    def test_a_rewritten_remote_is_diverged(self):
        """Breaks if a squashed store reads as current or behind, either of which
        leads to an update that cannot fast-forward."""
        squash(self.origin)
        self.assertEqual(run_block("freshness", self.clone), "FRESHNESS: DIVERGED")

    def test_an_unreachable_remote_is_unknown_with_the_reason(self):
        """Breaks if a failed fetch is reported as current, from refs it never refreshed."""
        git(self.clone, "remote", "set-url", "origin", (self.root / "gone").as_uri())
        line = run_block("freshness", self.clone)
        self.assertTrue(line.startswith("FRESHNESS: UNKNOWN "), line)
        self.assertGreater(len(line), len("FRESHNESS: UNKNOWN "), "no reason given")

    def test_the_update_brings_a_depth_one_clone_current(self):
        """Breaks if the update cannot move the sparse depth-1 clone step 6 makes,
        or leaves the old graph on disk."""
        commit(self.origin, "two", "graphify-out/graph.json.gz")
        self.assertEqual(run_block("freshness", self.clone), "FRESHNESS: BEHIND 1")
        run_block("update", self.clone)
        self.assertEqual(run_block("freshness", self.clone), "FRESHNESS: current")
        graph = (self.clone / "graphify-out/graph.json.gz").read_text(encoding="utf-8")
        self.assertEqual(graph, "two graphify-out/graph.json.gz")

    def test_the_check_leaves_a_full_clone_unshallow(self):
        """Breaks if the fetch takes a --depth, which makes a maintainer's full
        clone shallow behind their back."""
        full = self.root / "full"
        git(self.root, "clone", "-q", self.origin.as_uri(), str(full))
        commit(self.origin, "two", "docs/guide.md")
        self.assertEqual(run_block("freshness", full), "FRESHNESS: BEHIND 1")
        shallow = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=full,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(shallow.stdout.strip(), "false")


class ResetCheckBlockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.origin = make_store(self.root, *WANTED, UNWANTED)
        self.clone = sparse_clone(self.origin, self.root / "clone")

    def tearDown(self):
        self._tmp.cleanup()

    def test_uncommitted_changes_refuse_the_reset(self):
        """Breaks if a reset that would discard an edit is reported safe."""
        (self.clone / "knowledge_context.md").write_text("edited", encoding="utf-8")
        squash(self.origin)
        run_block("freshness", self.clone)
        self.assertEqual(
            run_block("reset-check", self.clone), "RESET-CHECK: refuse uncommitted changes"
        )

    def test_local_commits_refuse_the_reset(self):
        """Breaks if a reset that would discard a commit no remote has is reported safe."""
        commit(self.clone, "mine", "knowledge/note.md")
        squash(self.origin)
        run_block("freshness", self.clone)
        self.assertEqual(
            run_block("reset-check", self.clone),
            "RESET-CHECK: refuse local commits not on any remote branch",
        )

    def test_a_clean_clone_of_a_rewritten_store_is_safe_and_the_reset_lands(self):
        """Breaks if the commits the remote used to hold count as the asker's,
        which would refuse the reset after every rewrite, for ever."""
        squash(self.origin)
        self.assertEqual(run_block("freshness", self.clone), "FRESHNESS: DIVERGED")
        self.assertEqual(run_block("reset-check", self.clone), "RESET-CHECK: safe")
        git(self.clone, "reset", "-q", "--hard", "origin/main")
        self.assertEqual(run_block("freshness", self.clone), "FRESHNESS: current")


class RepairBlockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def old_clone(self, origin: Path, *, refused: bool = True) -> Path:
        """A clone the way the cone-mode step made one: it stopped at the set.
        Cone mode refuses a file path only when the tree holds it, so a store with
        no graph is `refused=False`."""
        target = self.root / "old"
        git(
            self.root,
            "clone",
            "-q",
            "--depth",
            "1",
            "--filter=blob:none",
            "--sparse",
            origin.as_uri(),
            str(target),
        )
        cone = subprocess.run(
            ["git", "sparse-checkout", "set", "graphify-out/graph.json.gz", "knowledge", "docs"],
            cwd=target,
            capture_output=True,
        )
        if refused:
            self.assertNotEqual(cone.returncode, 0, "cone mode took a file path; stale fixture")
        return target

    def test_repair_restores_what_step_six_selects_to_a_cone_mode_clone(self):
        """Breaks if the repair goes back to cone mode, or selects less than step 6
        does, or leaves a clone at the commit it was made from."""
        origin = make_store(self.root, *WANTED, UNWANTED)
        old = self.old_clone(origin)
        self.assertFalse((old / "graphify-out/graph.json.gz").exists())
        commit(origin, "two", "graphify-out/graph.json.gz")
        self.assertEqual(run_block("repair", old), "REPAIR: done")
        for name in WANTED:
            self.assertTrue((old / name).is_file(), f"{name} missing after the repair")
        self.assertFalse((old / UNWANTED).exists(), "the repair checked out the page")
        graph = (old / "graphify-out/graph.json.gz").read_text(encoding="utf-8")
        self.assertEqual(graph, "two graphify-out/graph.json.gz")

    def test_repair_leaves_a_complete_clone_alone(self):
        """Breaks if the repair rewrites the sparse set of a clone that needs nothing."""
        origin = make_store(self.root, *WANTED, UNWANTED)
        clone = sparse_clone(origin, self.root / "clone")
        before = (clone / ".git/info/sparse-checkout").read_text(encoding="utf-8")
        self.assertEqual(run_block("repair", clone), "REPAIR: not needed")
        self.assertEqual((clone / ".git/info/sparse-checkout").read_text(encoding="utf-8"), before)

    def test_repair_fails_when_the_store_has_no_graph_to_fetch(self):
        """Breaks if the repair reports done without the graph on disk."""
        origin = make_store(self.root, "README.md", "knowledge/repository-manifest.md")
        old = self.old_clone(origin, refused=False)
        self.assertEqual(run_block("repair", old), "REPAIR: failed")


if __name__ == "__main__":
    unittest.main()
