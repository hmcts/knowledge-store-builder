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
FILE_READING = ("graphify query", "zcat", "gunzip", "gzip.open", "jq ", "grep -r", "rg ")

BUILDER_ANCHOR = "creating-a-store.md#add-the-server-to-the-store-plugin"
UV_INSTALL = "https://docs.astral.sh/uv/getting-started/installation/"

# The server resolves its clone once, at start-up, and exits when none is listed or
# the clone has no manifest - the likeliest cause on first use, so it comes first.
UNREGISTERED = "not registered"
MANIFEST = "knowledge/store-manifest.json"
RECONNECT = "reconnect the server with /mcp"
NOT_LAUNCHED = "plugin does not launch the server"

# An asker may hold this skill and a store's own skill at once, and both trigger on
# a store question. Before stopping, the skill defers to a store skill that works.
DEFER = "If this store's own plugin provides a skill for it, use that skill instead"
DESCRIPTION_DEFER = "defer to a store's own skill"

TAG = re.compile(r"^v\d+\.\d+\.\d+$")

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
    """The tools the table routes to: the identifiers in each row's first column."""
    return [name for row in rows for name in IDENTIFIER.findall(row.split("|")[1])]


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


def description(text: str) -> str:
    """The front matter's `description:` value, or ""."""
    if not text.startswith("---\n"):
        return ""
    front = text.split("---\n", 2)[1]
    found = [line for line in front.splitlines() if line.startswith("description:")]
    return found[0] if found else ""


def defer_problems(text: str) -> list[str]:
    """Whether the skill defers to a store's own skill before giving the stop message."""
    problems = []
    if DESCRIPTION_DEFER not in description(text):
        problems.append("the description does not say to defer to a store's own skill")
    flat = " ".join(section(text, ANSWERING).split())
    message = " ".join(missing_server_message(text).split())
    if DEFER not in flat:
        problems.append("does not defer to a store's own skill before stopping")
    elif message and flat.index(DEFER) > flat.index(message):
        problems.append("defers to a store's own skill only after the stop message")
    return problems


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
    flat = " ".join(message.split())
    if "never read files" not in flat:
        problems.append('does not say "never read files"')
    if UNREGISTERED not in flat or MANIFEST not in flat:
        problems.append("does not name an unregistered clone or a missing manifest as a cause")
    elif NOT_LAUNCHED in flat and flat.index(UNREGISTERED) > flat.index(NOT_LAUNCHED):
        problems.append("does not list the unregistered clone first, as the likeliest cause")
    if RECONNECT not in flat or "restart Claude Code" not in flat:
        problems.append("does not say to reconnect the server once the clone is registered")
    if NOT_LAUNCHED not in flat:
        problems.append("does not name a plugin that does not launch the server as a cause")
    return problems


class ThePinTest(unittest.TestCase):
    def test_the_pin_names_a_tag_and_twelve_sorted_tools(self):
        """Breaks if the pin file loses its tag line or its tool list, which every
        check below reads - an empty list would let the skill name anything."""
        text = PIN.read_text(encoding="utf-8")
        self.assertRegex(pinned_tag(text), TAG)
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
        skill, which would put a second answering path beside the server's.

        It reads fenced blocks only: a prose instruction to run any of these
        passes, because it is not where an agent copies a command from
        (CLAUDE.md 2.10)."""
        found = file_reading_lines(self.skill)
        self.assertEqual(found, [], "; ".join(found))

    def test_the_skill_says_what_to_do_without_the_server(self):
        """Breaks if the skill stops telling the asker that their store's plugin does
        not launch the server, what the maintainer does about it and that they need
        `uv` - and so either fails without a word or falls back to reading files.

        Also breaks if the skill stops deferring to a store's own skill first, so it
        shadows a store skill that works with the stop message."""
        problems = message_problems(missing_server_message(self.skill))
        problems += defer_problems(self.skill)
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
        for marker in FILE_READING:
            with self.subTest(marker=marker):
                forged = self.skill + f"\n```bash\n{marker}<argument>\n```\n"
                self.assertEqual(len(file_reading_lines(forged)), 1)

    def test_a_skill_that_does_not_defer_is_noticed(self):
        self.assertEqual(defer_problems(self.skill), [], "precondition: the skill defers")
        self.assertEqual(len(defer_problems(self.skill.replace(DEFER, "UNSET"))), 1)
        self.assertEqual(len(defer_problems(self.skill.replace(DESCRIPTION_DEFER, "UNSET"))), 1)

    def test_only_the_first_column_names_a_tool(self):
        # Prose in the description column may mention a field in backticks; only
        # the first column routes a question to a tool.
        self.assertEqual(tools_named(["| `node` | Returns its `url`. |"]), ["node"])

    def test_a_message_listing_the_plugin_cause_first_is_noticed(self):
        message = " ".join(missing_server_message(self.skill).split())
        self.assertEqual(message_problems(message), [], "precondition: the message is clean")
        reordered = message.replace(UNREGISTERED, "UNSET").replace(NOT_LAUNCHED, UNREGISTERED)
        reordered = reordered.replace("UNSET", NOT_LAUNCHED)
        self.assertIn(
            "does not list the unregistered clone first, as the likeliest cause",
            message_problems(reordered),
        )

    def test_a_missing_message_is_noticed(self):
        message = missing_server_message(self.skill)
        self.assertTrue(message, "precondition: the message is found")
        self.assertTrue(message_problems(missing_server_message(self.skill.replace(message, ""))))
        for element in ("never read files", UNREGISTERED, MANIFEST, RECONNECT, NOT_LAUNCHED):
            with self.subTest(removed=element):
                self.assertIn(element, " ".join(message.split()))
                forged = " ".join(message.split()).replace(element, "")
                self.assertTrue(message_problems(forged))


if __name__ == "__main__":
    unittest.main()
