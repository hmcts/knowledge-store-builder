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

from knowledgestore import check_plugin as gate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs/creating-a-store.md"
DECLARED = ROOT / "docs/mcp-server.txt"
FENCE = re.compile(r"```json\n(.*?)```", re.DOTALL)
PIN = re.compile(r"knowledge-store-builder@(\S+?)(?=[\"'\s)\],]|$)")
FEED_PIN = re.compile(r"knowledge-store-builder\[server\]==(\d+\.\d+\.\d+)")


def declared_tag() -> str:
    first = DECLARED.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("tag "), first
    return first.split(None, 1)[1].strip()


def declared_index() -> str:
    second = DECLARED.read_text(encoding="utf-8").splitlines()[1]
    assert second.startswith("index "), second
    return second.split(None, 1)[1].strip()


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

    def test_the_declared_index_is_the_one_check_plugin_requires(self):
        """Catches docs/mcp-server.txt and the stage naming different feeds, so the
        docs would teach a launch the stage refuses."""
        self.assertEqual(declared_index(), gate.FEED_INDEX)

    def test_the_example_launches_the_declared_version_from_the_feed(self):
        """Catches a version bump in docs/mcp-server.txt that the example did not
        follow, and an example that drops the feed index, which would let the name
        resolve from PyPI, where it is not published and anyone can register it."""
        examples = guide_examples()
        self.assertTrue(examples, "creating-a-store.md carries no mcpServers example")
        for example in examples:
            args = example["mcpServers"]["knowledge-store"]["args"]
            self.assertEqual(args[args.index("--index") + 1], gate.FEED_INDEX)
            source = args[args.index("--from") + 1]
            self.assertEqual(source, "hmcts-knowledge-store-builder[server]==" + declared_tag()[1:])

    def test_the_example_runs_the_server_for_the_store_id(self):
        """Catches an example that drops `knowledgestore serve` or `--store-id`.

        Without `--store-id` the server cannot resolve a clone through the
        locations file, so the plugin launches a server that serves nothing.
        """
        examples = guide_examples()
        self.assertTrue(examples, "creating-a-store.md carries no mcpServers example")
        for example in examples:
            args = example["mcpServers"]["knowledge-store"]["args"]
            self.assertEqual(args[args.index("knowledgestore") + 1], "serve")
            self.assertIn("--store-id", args)
            self.assertEqual(args[args.index("--store-id") + 1], "example-store")

    def test_every_launch_in_the_docs_names_the_declared_version_and_feed(self):
        """Catches a stale version, or a feed launch without `--index`, in any
        shipped document, not only the builder guide. The git form may appear, but
        only at the declared tag."""
        paths = [ROOT / "README.md", ROOT / "CHEATSHEET.md"]
        for folder in ("docs", "skills"):
            paths += sorted((ROOT / folder).rglob("*.md"))
        paths = [p for p in paths if "superpowers" not in p.parts]
        seen = 0
        for path in paths:
            text = path.read_text(encoding="utf-8")
            where = path.relative_to(ROOT)
            for tag in PIN.findall(text):
                self.assertEqual(tag, declared_tag(), f"{where} pins {tag}")
            for match in FEED_PIN.finditer(text):
                seen += 1
                self.assertEqual("v" + match[1], declared_tag(), f"{where} pins {match[1]}")
                # The index is written just before --from in every launch.
                window = text[max(0, match.start() - 250) : match.start()]
                self.assertIn(gate.FEED_INDEX, window, f"{where}: a feed launch without --index")
        self.assertGreater(seen, 0, "no document names a pinned version")


if __name__ == "__main__":
    unittest.main()
