"""The README's copy of the asker's install commands matches the guide that owns them.

CLAUDE.md 4.2 lets the README carry the three `/plugin` commands so a reader can
act on the first screen. `docs/asking-questions.md` owns the install; a README
copy that drifts from it is the defect 4.2 was first written against, when a
README kept its own install steps after the guides moved on.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)


def plugin_commands(text: str) -> list[str]:
    """Every `/plugin` or `/reload-plugins` line inside a fenced block, in order."""
    return [
        line.strip()
        for block in FENCE.findall(text)
        for line in block.splitlines()
        if line.strip().startswith(("/plugin ", "/reload-plugins"))
    ]


class ReadmeCommandsTest(unittest.TestCase):
    def test_the_readme_plugin_commands_are_the_guides(self):
        """Breaks if the README's install commands drift from docs/asking-questions.md:
        a renamed marketplace or plugin changed in one place and not the other."""
        readme = plugin_commands((ROOT / "README.md").read_text(encoding="utf-8"))
        guide = plugin_commands((ROOT / "docs" / "asking-questions.md").read_text(encoding="utf-8"))
        self.assertTrue(readme, "the README carries no /plugin commands, so nothing was compared")
        self.assertEqual([c for c in readme if c not in guide], [])

    def test_the_extraction_reads_fenced_commands_only(self):
        """Breaks if the extractor stops reading blocks, which would make the check
        above compare nothing; prose that mentions a command is not a copy of it."""
        text = "Run `/plugin install x` later.\n\n```\n/plugin install a@b\n/reload-plugins\n```\n"
        self.assertEqual(plugin_commands(text), ["/plugin install a@b", "/reload-plugins"])


if __name__ == "__main__":
    unittest.main()
