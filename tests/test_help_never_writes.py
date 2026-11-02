"""Asking any stage for `--help` prints usage and changes nothing on disk.

A stage that ignores `--help` and runs instead is a destructive surprise: one
store asked `summaries snapshot --help` and got its committed membership
snapshot overwritten, which a following `summaries remap` needed to read.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from knowledgestore import cli  # noqa: E402

SUMMARIES = ("extract", "merge", "snapshot", "verify", "adrift", "remap")


def tree_digest(root: Path) -> dict[str, str]:
    """Every file under `root`, by relative path, to the sha256 of its bytes."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def invocations() -> list[list[str]]:
    calls = [[stage, "--help"] for stage in sorted(cli.STAGES) if stage != "summaries"]
    calls += [["summaries", sub, "--help"] for sub in SUMMARIES]
    return calls


class HelpNeverWritesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Path(tempfile.mkdtemp())
        # Files a careless stage would rewrite: the committed graph and snapshots.
        for rel in (
            "graphify-out/graph.json",
            "knowledge/summaries/membership-snapshot.json.gz",
            "knowledge/summaries/membership-files.json.gz",
            "config/repositories.txt",
        ):
            path = self.store / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'{"nodes": [], "links": []}\n' if rel.endswith(".json") else b"x\n")

    def test_help_on_every_stage_exits_zero_and_writes_nothing(self):
        """Breaks if any stage, or a summaries subcommand, runs its work when asked
        for `--help`: the store's files change, or the call exits non-zero."""
        before = tree_digest(self.store)
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        for call in invocations():
            with self.subTest(call=" ".join(call)):
                done = subprocess.run(
                    [sys.executable, "-m", "knowledgestore.cli", "--root", str(self.store), *call],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=120,
                )
                self.assertEqual(done.returncode, 0, done.stderr[-500:])
                self.assertEqual(tree_digest(self.store), before, "files changed")


if __name__ == "__main__":
    unittest.main()
