"""The knowledge-store skill's clone block runs as written, against real git.

Break it catches: a command in the skill that git refuses. The block listed file
paths to `git sparse-checkout set` in the default cone mode, which current git
rejects with "is not a directory", so an asker's first clone stopped before it
fetched anything, and no test ran the block. This runs the block exactly as the
skill prints it, with only its two placeholders filled in.
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


if __name__ == "__main__":
    unittest.main()
