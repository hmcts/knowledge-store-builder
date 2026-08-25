"""The documented ask route must decompress a stale graph, not skip it (#199).

Stores commit `graphify-out/graph.json.gz` and gitignore `graph.json`. The route
these tests guard used to decompress only when the plain file was **absent**, so
it ran once and every later question was answered from that first copy however old
it had become. `git pull` made it worse rather than better: it moves the archive
and leaves the stale plain file, so the step taken to obtain current data
guaranteed stale data.

The reader on that route is the persona with the least ability to detect it — no
graph, no CLI, nothing to compare an answer against. So the check is executable
rather than a reading: **the command is extracted from the shipped skill and run**,
against the three states that matter. A test asserting a copy of the command would
pass while the skill said something else, which is the failure mode this file
exists to prevent.

Each test names the break it catches.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "knowledge-store" / "SKILL.md"
GUIDE = ROOT / "docs" / "asking-questions.md"

# The form that caused the defect. Pinned negatively: removing the precondition
# for an error is not the same as detecting its return.
SUPERSEDED = "[ -f graphify-out/graph.json ] ||"


def decompress_command(text: str) -> str:
    """The multi-line decompress command as the document ships it.

    Extracted rather than restated. A test carrying its own copy would pass while
    the skill said something different, and the skill is what an agent executes.
    """
    match = re.search(
        r"\[ -f graphify-out/graph\.json\.gz \][^\n]*\n(?:\s+&&[^\n]*\n)+",
        text,
    )
    return match.group(0) if match else ""


class AskRouteDecompressTest(unittest.TestCase):
    def setUp(self):
        self.skill = SKILL.read_text(encoding="utf-8")
        self.guide = GUIDE.read_text(encoding="utf-8")
        self.command = decompress_command(self.skill)

    # --- the instruction itself ---------------------------------------------

    def test_the_skill_still_carries_a_staleness_test(self):
        """Breaks if the command is reformatted such that this gate reads nothing.

        Without this the executable tests below would run an empty string and pass,
        which is the vacuous case a parse-based gate fails into.
        """
        self.assertTrue(self.command, f"no decompress command found in {SKILL.name}")
        self.assertIn("-nt", self.command, "the command no longer tests staleness")
        self.assertIn("gunzip -kf", self.command, "without -f gunzip refuses to overwrite")

    def test_neither_document_reintroduces_the_existence_test(self):
        """Breaks if the superseded form returns to either file.

        The defect was not a missing feature but a wrong condition, so the check
        that makes the correction durable has to fail when that condition comes
        back — in the skill an agent executes and in the guide a human follows.
        """
        for name, text in (("skill", self.skill), ("guide", self.guide)):
            with self.subTest(document=name):
                self.assertNotIn(SUPERSEDED, text)

    def test_the_guide_and_the_skill_agree(self):
        """Breaks if one is corrected and the other is not.

        A documented route stated twice can disagree, and the reader following the
        prose has no way to know the skill says otherwise.
        """
        self.assertIn("-nt", self.guide)
        self.assertIn("gunzip -kf", self.guide)

    # --- running the shipped command ----------------------------------------

    def _run(self, directory: Path):
        subprocess.run(
            ["bash", "-c", self.command], cwd=directory, check=False, capture_output=True
        )

    def _store(self, plain: dict | None, packed: dict) -> Path:
        directory = Path(self._tmp.name)
        (directory / "graphify-out").mkdir(parents=True, exist_ok=True)
        with gzip.open(directory / "graphify-out" / "graph.json.gz", "wt", encoding="utf-8") as h:
            json.dump(packed, h)
        if plain is not None:
            (directory / "graphify-out" / "graph.json").write_text(
                json.dumps(plain), encoding="utf-8"
            )
        return directory

    def setUpTmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_a_stale_plain_file_is_replaced(self):
        """Breaks if the defect returns. This is the state a previous ask leaves
        behind once the archive has moved on, and the state a `git pull` creates."""
        self.setUpTmp()
        directory = self._store(plain={"nodes": ["old"]}, packed={"nodes": ["new"]})
        plain = directory / "graphify-out" / "graph.json"
        os.utime(plain, (1_600_000_000, 1_600_000_000))

        self._run(directory)

        self.assertEqual(
            json.loads(plain.read_text(encoding="utf-8")),
            {"nodes": ["new"]},
            "the stale plain file was queried instead of being refreshed",
        )

    def test_a_current_plain_file_is_left_alone(self):
        """Breaks if the command decompresses unconditionally.

        Doing so is correct but pays the cost on every ask, which on a large graph
        is seconds each time. The cheap case has to stay cheap or the guard gets
        removed.
        """
        self.setUpTmp()
        directory = self._store(plain={"nodes": ["same"]}, packed={"nodes": ["same"]})
        plain = directory / "graphify-out" / "graph.json"
        packed = directory / "graphify-out" / "graph.json.gz"
        # The plain file must be NEWER than the archive for this to be the current
        # case. An earlier version aged the plain file while the archive was written
        # now, which is the stale case wearing the wrong name - and it failed, which
        # is the command working rather than the command being wrong.
        os.utime(packed, (1_600_000_000, 1_600_000_000))
        os.utime(plain, (1_700_000_000, 1_700_000_000))
        before = plain.stat().st_mtime

        self._run(directory)

        self.assertEqual(plain.stat().st_mtime, before, "a current file was rewritten")

    def test_an_absent_plain_file_is_created(self):
        """Breaks if the first ask on a fresh clone is left with no graph — the
        one case the superseded form got right, so it must not regress."""
        self.setUpTmp()
        directory = self._store(plain=None, packed={"nodes": ["fresh"]})

        self._run(directory)

        plain = directory / "graphify-out" / "graph.json"
        self.assertTrue(plain.is_file(), "no graph was produced for the first question")
        self.assertEqual(json.loads(plain.read_text(encoding="utf-8")), {"nodes": ["fresh"]})

    def test_this_gate_notices_the_superseded_command(self):
        """The sensitivity check, in the same run.

        Runs the *old* command against the stale case and asserts it does the wrong
        thing. If it did the right thing, these tests would be measuring nothing —
        and a gate that cannot fail is decoration.
        """
        self.setUpTmp()
        directory = self._store(plain={"nodes": ["old"]}, packed={"nodes": ["new"]})
        plain = directory / "graphify-out" / "graph.json"

        subprocess.run(
            [
                "bash",
                "-c",
                "[ -f graphify-out/graph.json ] || gunzip -k graphify-out/graph.json.gz",
            ],
            cwd=directory,
            check=False,
            capture_output=True,
        )

        self.assertEqual(
            json.loads(plain.read_text(encoding="utf-8")),
            {"nodes": ["old"]},
            "the superseded command no longer reproduces the defect, so this gate is vacuous",
        )


if __name__ == "__main__":
    unittest.main()
