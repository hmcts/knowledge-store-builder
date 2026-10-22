"""Every documented append to the locations file is the skill's step-7 block.

The knowledge-store server resolves a store's clone through
`~/.config/knowledge-store/locations`, so a reader who registers a clone by hand
runs whichever copy of the append their page carries. The transition page once
carried a copy without `mkdir -p`, which exits 1 with "No such file or directory"
on a machine that has never had the file - exactly the machine following that
page. The skill's step 7 is the canonical block; every other fenced block that
appends to the file must contain it verbatim, so a copy cannot drift from it.

This reads fenced blocks only. A prose instruction to append to the file is not
checked (CLAUDE.md 2.10).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "knowledge-store" / "SKILL.md"
APPEND = ">> ~/.config/knowledge-store/locations"


def fenced_blocks(text: str) -> list[str]:
    """Each fenced block's body, dedented so a block inside a list item compares
    equal to the same block at the margin."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append(textwrap.dedent("\n".join(current)).strip("\n"))
                current = None
        elif current is not None:
            current.append(line)
    return blocks


def appending_blocks(text: str) -> list[str]:
    return [block for block in fenced_blocks(text) if APPEND in block]


def canonical_block() -> str:
    found = appending_blocks(SKILL.read_text(encoding="utf-8"))
    assert len(found) == 1, f"the skill holds {len(found)} blocks appending to locations"
    return found[0]


def shipped_documents() -> list[Path]:
    paths = [ROOT / "README.md", ROOT / "CHEATSHEET.md"]
    for folder in ("docs", "skills"):
        paths += sorted((ROOT / folder).rglob("*.md"))
    return [p for p in paths if p.exists() and "superpowers" not in p.parts and p != SKILL]


def drifted_copies(canonical: str, documents: dict[str, str]) -> tuple[list[str], int]:
    """Names of documents holding an appending block without the canonical block,
    and how many appending blocks were read."""
    drifted: list[str] = []
    seen = 0
    for name, text in documents.items():
        for block in appending_blocks(text):
            seen += 1
            if canonical not in block:
                drifted.append(name)
    return drifted, seen


class LocationsBlockTest(unittest.TestCase):
    def setUp(self):
        self.canonical = canonical_block()
        self.documents = {
            str(p.relative_to(ROOT)): p.read_text(encoding="utf-8") for p in shipped_documents()
        }

    def test_every_copy_is_the_skill_block_verbatim(self):
        """Breaks if a page's append to the locations file drops `mkdir -p`, the
        duplicate check or the error suppression the skill's block carries."""
        drifted, seen = drifted_copies(self.canonical, self.documents)
        self.assertGreater(seen, 0, "no document outside the skill appends to locations")
        self.assertEqual(drifted, [], f"copies that differ from the skill's step 7: {drifted}")

    def test_the_skill_block_registers_a_clone_on_a_clean_machine(self):
        """Breaks if the canonical block cannot write the file where none exists,
        or appends the same clone twice."""
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as clone:
            env = {**os.environ, "HOME": home}
            for _ in range(2):
                subprocess.run(["sh", "-c", self.canonical], cwd=clone, env=env, check=True)
            written = Path(home, ".config", "knowledge-store", "locations").read_text()
            self.assertEqual(written.splitlines(), [str(Path(clone).resolve())])


class LocationsBlockCanFailTest(unittest.TestCase):
    """CLAUDE.md 2.8: the comparison above is shown, in the same run, to notice a
    copy that has lost a line of the skill's block."""

    def test_a_copy_without_mkdir_is_noticed(self):
        canonical = canonical_block()
        self.assertIn("mkdir -p", canonical, "precondition: the skill's block creates the folder")
        broken = "\n".join(line for line in canonical.splitlines() if "mkdir -p" not in line)
        page = f"```bash\ncd <store-clone>\n{broken}\n```\n"
        self.assertEqual(drifted_copies(canonical, {"page.md": page}), (["page.md"], 1))

    def test_an_indented_verbatim_copy_passes(self):
        canonical = canonical_block()
        page = "1. Register it:\n\n   ```bash\n" + textwrap.indent(canonical, "   ") + "\n   ```\n"
        self.assertEqual(drifted_copies(canonical, {"page.md": page}), ([], 1))


if __name__ == "__main__":
    unittest.main()
