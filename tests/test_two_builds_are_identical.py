"""Build the same store twice, in two processes, and diff the artefacts.

`CLAUDE.md` opens its ground rules with "deterministic output is a feature: two
runs on the same inputs must be byte-identical", and adds that hash
randomisation across processes has broken it before and is "invisible until
someone diffs two builds". The per-stage determinism tests do not close that:
most of them compare two calls inside one interpreter, where `PYTHONHASHSEED`
is already fixed, and each covers only the stage somebody thought to pin. A
stage added later, or the merge between two stages, is covered by nothing.

This is the diff. `tests/explorer/fixture.py` writes real inputs and runs the
real topic-brief, deep-dive and explorer stages, so what is compared here is
the pipeline's own output rather than a re-implementation of it.

Two properties, without which it proves nothing:

- **Two processes.** `PYTHONHASHSEED` is fixed at interpreter start, so a
  same-process comparison cannot see the defect this exists for. The builds
  run under *different* explicit seeds, which is the stronger claim: not "these
  two runs happened to agree" but "the ordering does not depend on the seed".
- **Bytes, not a summary.** Every file both builds wrote, compared directly, so
  a failure names the artefact and shows the bytes that moved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
FIXTURE = TESTS / "explorer" / "fixture.py"

# Two seeds, different from each other, and explicit rather than `random`: two
# runs under one seed would only catch a build that had become stateful, and a
# seed drawn at random gives a failure nobody can reproduce. Exactly two,
# because the comparison is a diff and a diff has two sides.
SEED_A, SEED_B = "0", "1"

# What the comparison must be able to see. Without these the check would pass
# over an empty pair of directories - a determinism test that compares nothing
# is green forever.
COVERS = (
    "graphify-out/explorer.html",  # the page readers get
    "knowledge/deep-dives/dives.json",  # a merged, committed artefact
    "knowledge/topics/briefs.json",
    "graphify-out/explorer-inputs.json",
)


def _build(store: Path, seed: str) -> None:
    """Run the real fixture build in its own interpreter under `seed`."""
    completed = subprocess.run(
        # `-B` because the environment below is built rather than extended, so
        # `run_suite`'s bytecode suppression does not reach this grandchild - and a
        # `.pyc` left here is one a mutated run reads back, reporting on code that
        # was never in the tree (#228). Bytecode is also per-seed-invariant, so
        # caching it would let one seed's compile serve the other's build.
        [sys.executable, "-B", str(FIXTURE), "--out", str(store)],
        capture_output=True,
        text=True,
        env={"PYTHONHASHSEED": seed, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"the fixture build failed under PYTHONHASHSEED={seed} "
            f"(exit {completed.returncode}):\n{completed.stderr}"
        )


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _difference(first: bytes, second: bytes) -> str:
    """Where the two artefacts part company, with the bytes on either side."""
    offset = next(
        (i for i, (a, b) in enumerate(zip(first, second)) if a != b),
        min(len(first), len(second)),
    )
    window = slice(max(0, offset - 60), offset + 60)
    return (
        f"{len(first)} bytes vs {len(second)}, first differing byte at offset {offset}\n"
        f"  PYTHONHASHSEED={SEED_A}: {first[window]!r}\n"
        f"  PYTHONHASHSEED={SEED_B}: {second[window]!r}"
    )


class TwoBuildsAreByteIdentical(unittest.TestCase):
    """Build twice, compare everything.

    Built once for the class: the stages are the same in all three tests, and
    the whole pair costs well under a second.
    """

    tmp: str
    build_a: Path
    build_b: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="ksb-two-builds-")
        cls.build_a = Path(cls.tmp) / f"seed-{SEED_A}"
        cls.build_b = Path(cls.tmp) / f"seed-{SEED_B}"
        _build(cls.build_a, SEED_A)
        _build(cls.build_b, SEED_B)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_both_builds_write_the_same_files(self):
        """Break: emit a file whose name carries something per-run. The bytes
        comparison below only looks at paths both builds produced, so a name
        that moves would otherwise slip past it entirely."""
        first, second = _files(self.build_a), _files(self.build_b)
        self.assertEqual(first, second)
        self.assertTrue(first, "neither build wrote anything - nothing was compared")

    def test_every_artefact_is_byte_identical(self):
        """Break: iterate a set, or a dict keyed by unordered data, without a
        tiebreak anywhere in the topic-brief, deep-dive or explorer stages. Two
        builds of an unchanged store then differ, every consumer's rebuild is a
        spurious diff, and real changes are buried in it."""
        for name in _files(self.build_a):
            with self.subTest(artefact=name):
                first = (self.build_a / name).read_bytes()
                second = (self.build_b / name).read_bytes()
                self.assertEqual(
                    first,
                    second,
                    f"{name} is not byte-identical across two builds: {_difference(first, second)}",
                )

    def test_the_comparison_reaches_the_page_and_the_merged_json(self):
        """Break: stop the fixture running a stage, or move an artefact, and the
        loop above still passes - over what is left. A gate that can only pass or
        fail cannot report that it has gone vacuous, so this names what it covers."""
        built = set(_files(self.build_a))
        for name in COVERS:
            self.assertIn(name, built)
        page = (self.build_a / "graphify-out/explorer.html").read_bytes()
        self.assertGreater(len(page), 10_000, "a stub page would compare equal and mean nothing")


if __name__ == "__main__":
    unittest.main()
