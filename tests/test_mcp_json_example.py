"""The builder guide's `.mcp.json` example launches the server at the declared tag.

A store plugin copies this block. If it names a different tag from
`docs/mcp-server.txt`, or stops launching through `uvx`, every asker of a store
built from the guide runs a server nobody chose, or none.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs/creating-a-store.md"
DECLARED = ROOT / "docs/mcp-server.txt"
FENCE = re.compile(r"```json\n(.*?)```", re.DOTALL)
PIN = re.compile(r"knowledge-store-mcp@(\S+?)(?=[\"'\s)\],]|$)")


def declared_tag() -> str:
    first = DECLARED.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("tag "), first
    return first.split(None, 1)[1].strip()


def guide_examples() -> list[dict]:
    blocks = [b for b in FENCE.findall(GUIDE.read_text(encoding="utf-8")) if "mcpServers" in b]
    return [json.loads(b.replace("<id>", "example-store")) for b in blocks]


class McpJsonExample(unittest.TestCase):
    def test_the_example_parses_and_launches_with_uvx(self):
        """Catches an example that is not valid JSON or launches through another command."""
        examples = guide_examples()
        self.assertTrue(examples, "creating-a-store.md carries no mcpServers example")
        for example in examples:
            self.assertEqual(example["mcpServers"]["knowledge-store"]["command"], "uvx")

    def test_the_guide_shows_both_declaration_forms(self):
        """Catches the guide dropping the plugin.json form, which is the one a
        repository-rooted plugin needs, leaving only the `.mcp.json` a root plugin
        must not create."""
        examples = guide_examples()
        self.assertTrue(any("name" in e for e in examples), "no plugin.json-shaped example")
        self.assertTrue(any("name" not in e for e in examples), "no .mcp.json-shaped example")

    def test_the_example_pins_the_declared_tag(self):
        """Catches a tag bump in docs/mcp-server.txt that the example did not follow."""
        examples = guide_examples()
        self.assertTrue(examples, "creating-a-store.md carries no mcpServers example")
        for example in examples:
            args = example["mcpServers"]["knowledge-store"]["args"]
            source = args[args.index("--from") + 1]
            self.assertTrue(source.endswith("@" + declared_tag()), source)

    def test_the_example_runs_the_server_for_the_store_id(self):
        """Catches an example that drops the entry point or `--store-id`.

        Without `--store-id` the server cannot resolve a clone through the
        locations file, so the plugin launches a server that serves nothing.
        """
        examples = guide_examples()
        self.assertTrue(examples, "creating-a-store.md carries no mcpServers example")
        for example in examples:
            args = example["mcpServers"]["knowledge-store"]["args"]
            self.assertIn("knowledge-store-mcp", args)
            self.assertIn("--store-id", args)
            self.assertEqual(args[args.index("--store-id") + 1], "example-store")

    def test_every_mcp_json_example_in_the_docs_pins_the_declared_tag(self):
        """Catches a stale tag in any shipped document, not only the builder guide."""
        paths = [ROOT / "README.md", ROOT / "CHEATSHEET.md"]
        for folder in ("docs", "skills"):
            paths += sorted((ROOT / folder).rglob("*.md"))
        paths = [p for p in paths if "superpowers" not in p.parts]
        seen = 0
        for path in paths:
            for tag in PIN.findall(path.read_text(encoding="utf-8")):
                seen += 1
                self.assertEqual(tag, declared_tag(), f"{path.relative_to(ROOT)} pins {tag}")
        self.assertGreater(seen, 0, "no document names a pinned tag")


if __name__ == "__main__":
    unittest.main()
