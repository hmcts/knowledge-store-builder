"""The question skill answers through the knowledge-store server's tools, and only those.

Every front door, Claude Code included, answers from a store through the same MCP
server, so an answer in one assistant and an answer in another come from the same
tools under the same rules. The skill used to answer by reading the store's files
itself - decompressing the graph, running `graphify query`, reading gzipped layers
with Python and grepping the corpus - which is a second answering path with its own
rules, and the one nothing else shared.

`docs/mcp-server.txt` is the single declared pin of the server: its first line is
the release tag the store plugin launches, and the rest are the tools that release
serves. The tests here hold the skill to that list in both directions, keep the
file-reading path from returning to a command block an agent would run, and pin
what an asker is told when the store's plugin does not launch the server.

Each test names the break it catches.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from doc_sections import section

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "knowledge-store" / "SKILL.md"
PIN = ROOT / "docs" / "mcp-server.txt"

ANSWERING = "## Step 2 — Answer through the knowledge-store tools"

# The answering path the skill retired, as a command block would spell it. Scoped
# to the skill: `graphify query` and the corpus search stay documented for humans
# in `docs/asking-questions.md`, so none of these may be retired repository-wide.
FILE_READING = ("graphify query", "zcat", "gzip.open", "jq ", "grep -r")

BUILDER_ANCHOR = "creating-a-store.md#add-the-server-to-the-store-plugin"
UV_INSTALL = "https://docs.astral.sh/uv/getting-started/installation/"

IDENTIFIER = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


def pinned_tag(text: str) -> str:
    first = text.splitlines()[0] if text.strip() else ""
    return first.removeprefix("tag ") if first.startswith("tag ") else ""


def pinned_tools(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()[1:] if line.strip()]


def tool_table(text: str) -> list[str]:
    """The rows of the table in the answering section, header and rule excluded."""
    rows = [line for line in section(text, ANSWERING).splitlines() if line.startswith("|")]
    return [row for row in rows[2:] if not set(row) <= set("|- ")]


def tools_named(rows: list[str]) -> list[str]:
    return [name for row in rows for name in IDENTIFIER.findall(row)]


def fenced_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append("\n".join(current))
                current = None
        elif current is not None:
            current.append(line)
    return blocks


def file_reading_lines(text: str) -> list[str]:
    return [
        f"{marker!r} in: {line.strip()}"
        for block in fenced_blocks(text)
        for line in block.splitlines()
        for marker in FILE_READING
        if marker in line
    ]


def missing_server_message(text: str) -> str:
    """The fenced message the answering section tells the agent to give, or ""."""
    blocks = [b for b in fenced_blocks(section(text, ANSWERING)) if "uv" in b.split()]
    return blocks[0] if len(blocks) == 1 else ""


def message_problems(message: str) -> list[str]:
    problems = []
    if not message:
        return ["no single fenced missing-server message in the answering section"]
    if "uv" not in message.split():
        problems.append("does not name `uv`")
    if UV_INSTALL not in message:
        problems.append("does not say where to install `uv`")
    if BUILDER_ANCHOR not in message:
        problems.append("does not link the builder step that adds the server")
    if "never read files" not in " ".join(message.split()):
        problems.append('does not say "never read files"')
    return problems


class ThePinTest(unittest.TestCase):
    def test_the_pin_names_a_tag_and_twelve_sorted_tools(self):
        """Breaks if the pin file loses its tag line or its tool list, which every
        check below reads - an empty list would let the skill name anything."""
        text = PIN.read_text(encoding="utf-8")
        self.assertEqual(pinned_tag(text), "v0.1.0")
        tools = pinned_tools(text)
        self.assertEqual(len(tools), 12, tools)
        self.assertEqual(tools, sorted(set(tools)))


class TheSkillUsesTheServerToolsTest(unittest.TestCase):
    def setUp(self):
        self.skill = SKILL.read_text(encoding="utf-8")
        self.tools = pinned_tools(PIN.read_text(encoding="utf-8"))

    def test_every_tool_the_skill_names_is_a_server_tool(self):
        """Breaks if the skill tells an agent to call a tool the pinned server does
        not serve - a renamed tool, a typo, or one from a later release - which the
        agent then cannot find and works around by guessing."""
        rows = tool_table(self.skill)
        self.assertTrue(rows, f"no tool table under {ANSWERING!r}")
        unknown = sorted(set(tools_named(rows)) - set(self.tools))
        self.assertEqual(unknown, [], f"the skill names tools the server does not serve: {unknown}")

    def test_every_server_tool_appears_in_the_skill(self):
        """Breaks if a tool the server serves drops out of the skill's table, so no
        question is routed to it and its layer goes unasked."""
        named = set(tools_named(tool_table(self.skill)))
        missing = [tool for tool in self.tools if tool not in named]
        self.assertEqual(missing, [], f"server tools the skill's table does not name: {missing}")

    def test_the_skill_carries_no_file_reading_answer_path(self):
        """Breaks if a command block that answers by reading the store's files -
        `graphify query`, a gzip read, `jq`, a recursive grep - returns to the
        skill, which would put a second answering path beside the server's."""
        found = file_reading_lines(self.skill)
        self.assertEqual(found, [], "; ".join(found))

    def test_the_skill_says_what_to_do_without_the_server(self):
        """Breaks if the skill stops telling the asker that their store's plugin does
        not launch the server, what the maintainer does about it and that they need
        `uv` - and so either fails without a word or falls back to reading files."""
        problems = message_problems(missing_server_message(self.skill))
        self.assertEqual(problems, [], "; ".join(problems))


class TheseChecksCanFailTest(unittest.TestCase):
    """CLAUDE.md 2.8: each check above is shown, in the same run, to notice the
    break it names, by forging the real skill."""

    def setUp(self):
        self.skill = SKILL.read_text(encoding="utf-8")
        self.tools = pinned_tools(PIN.read_text(encoding="utf-8"))

    def test_a_wrong_tool_name_in_the_table_is_noticed(self):
        rows = tool_table(self.skill)
        self.assertTrue(rows, "precondition: the table is found")
        forged = self.skill.replace(f"`{self.tools[0]}`", "`no_such_tool`", 1)
        self.assertIn("no_such_tool", tools_named(tool_table(forged)))
        self.assertNotIn("no_such_tool", self.tools)

    def test_a_file_reading_block_is_noticed(self):
        forged = self.skill + '\n```bash\ngraphify query "<question>"\n```\n'
        self.assertEqual(len(file_reading_lines(forged)), 1)

    def test_a_missing_message_is_noticed(self):
        message = missing_server_message(self.skill)
        self.assertTrue(message, "precondition: the message is found")
        self.assertTrue(message_problems(missing_server_message(self.skill.replace(message, ""))))
        self.assertTrue(message_problems(message.replace("never read files", "")))


if __name__ == "__main__":
    unittest.main()
