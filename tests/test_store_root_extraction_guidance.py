"""An operator must be told not to extract at the store root, and why.

`graphify` is a peer CLI, and its own instructions say to scan the whole tree.
Over a store that is a tree of clones those instructions are wrong, and wrong in
the worst available way: `repositories/` is gitignored in every store and the
scan honours ignore rules, so a store-root run finds the store's own handful of
files, **does not error**, and builds a graph out of almost nothing. The store
then reads as a thin estate rather than a failed build.

The correction is prose in two places -- the build skill and the builder guide --
and prose has no gate unless one is written. `CLAUDE.md` records this exact
document drifting once already: "The README once kept `graphify .` at the store
root long after the build skill documented why that cannot work." Nothing failed
then, and until this file nothing would fail if either copy lost the instruction
now.

Two properties, and the second is the one worth having:

1. **The prohibition is stated.** An imperative an operator can follow.
2. **The mechanism is stated with it.** An instruction whose reason has been
   removed is the version someone talks themselves out of -- and this is the
   instruction competing with the tool's own, which prints at the operator while
   this one sits in a document. So each copy must still say that the directory is
   ignored, that the scan honours ignore rules, and that the outcome is a
   successful build over nothing rather than a failure.

The sections are extracted rather than searched for across the whole document, so
a phrase surviving somewhere else cannot stand in for the section being present,
and the gate asserts its own sensitivity in the same run.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Copy:
    """One document's copy of the instruction, and what it must still say."""

    path: str
    heading: str
    required: tuple[str, ...]


# Substrings of the collapsed section, each the shortest fragment that cannot
# survive its element being dropped -- so the surrounding wording stays free to
# change while the decision does not.
GUARDED = (
    Copy(
        "skills/knowledge-store-build/SKILL.md",
        "## Full build",
        (
            # the prohibition
            "Do not run graphify at the store root",
            # why the scan finds nothing: the directory is ignored, and the scan
            # obeys ignore rules. Either half alone explains nothing.
            "`repositories/` is gitignored",
            "honours ignore rules",
            # what an operator actually gets, which is a graph rather than an error
            "near-empty graph",
        ),
    ),
    Copy(
        "docs/creating-a-store.md",
        "### Build the graph",
        (
            # the prohibition, stated as the route to take instead
            "never from the store root",
            # the same mechanism, in the guide's own terms
            "`repositories/` is in `.gitignore`",
            "honours `.gitignore`",
            # the guide is where the silence is explicit, and the silence is the
            # whole hazard: a failed build that reports success
            "builds a graph from almost nothing",
            "There is no error",
        ),
    ),
)

# The route that replaces the prohibited one, required in the section's command
# blocks rather than its prose: a block is what an operator copies. Fragments
# rather than whole lines, so quoting and flags stay free to change.
REQUIRED_COMMANDS = ("cd ", "repositories/", "graphify merge-graphs")


def section(text: str, heading: str) -> str:
    """The heading's own body, up to the next heading, or "" if it is gone.

    Extracted rather than searched for so that a matching phrase elsewhere in the
    document cannot stand in for this section being present. Headings inside
    fenced code blocks are not headings -- a shell comment opens with `#` too.
    """
    if heading not in text:
        return ""
    body: list[str] = []
    fenced = False
    for line in text.split(heading, 1)[1].splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            break
        body.append(line)
    return "\n".join(body)


def commands(body: str) -> str:
    """Only what is inside the section's fenced blocks.

    Prose is excluded so that a sentence mentioning a command cannot stand in for
    the block that shows it.
    """
    inside: list[str] = []
    fenced = False
    for line in body.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif fenced:
            inside.append(line)
    return "\n".join(inside)


def collapsed(text: str) -> str:
    """One line, so a fragment matches across a wrapped line break."""
    return " ".join(text.split())


def missing_elements(text: str, required: tuple[str, ...]) -> list[str]:
    """Which required elements are absent. Separate so sensitivity can call it."""
    flat = collapsed(text)
    return [element for element in required if collapsed(element) not in flat]


class StoreRootExtractionGuidanceTest(unittest.TestCase):
    def test_every_guarded_section_is_present(self):
        """Breaks if a heading is renamed or removed, which would otherwise leave
        every assertion below passing over an empty string."""
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                body = section((ROOT / copy.path).read_text(encoding="utf-8"), copy.heading)
                self.assertTrue(
                    body, f"{copy.path} no longer has a section headed {copy.heading!r}"
                )
                self.assertGreater(
                    len(body.strip().splitlines()),
                    8,
                    f"{copy.path}'s {copy.heading!r} section is present but too short to "
                    "hold the instruction and its reason",
                )

    def test_every_copy_states_the_prohibition_and_the_mechanism(self):
        """Breaks if either copy loses the instruction, and -- the point of the
        gate -- if it keeps the imperative and drops the reason for it."""
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                absent = missing_elements(self._section(copy), copy.required)
                self.assertEqual(
                    absent,
                    [],
                    f"{copy.path}'s {copy.heading!r} section no longer states: {absent}. "
                    "The instruction competes with graphify's own, so a copy without its "
                    "mechanism loses the argument.",
                )

    def test_every_copy_shows_the_route_to_take_instead(self):
        """Breaks if the prohibition survives without the replacement.

        A reader told only what not to do falls back to the tool's instructions,
        which is the thing being prohibited. Extracting from inside each clone is
        also what keeps `source_file` repo-relative and what gives `merge-graphs`
        something to namespace, so those commands are the instruction's substance.
        """
        for copy in GUARDED:
            block = commands(self._section(copy))
            for command in REQUIRED_COMMANDS:
                with self.subTest(path=copy.path, command=command):
                    self.assertIn(
                        command,
                        block,
                        f"{copy.path}'s {copy.heading!r} section no longer shows "
                        f"`{command}` in a command block, so it prohibits a route "
                        "without showing the one to take",
                    )

    def test_this_gate_notices_a_dropped_element(self):
        """The sensitivity check, in the same run.

        Forges a copy of each real section with one required element removed and
        asserts the checker reports exactly that element. If this ever passes
        trivially, the assertions above are measuring the presence of a document
        rather than its content.
        """
        for copy in GUARDED:
            # Collapsed first: an element that wraps across a line break is not
            # present verbatim in the raw section, so a raw replace would match
            # nothing and the forge would silently be a no-op.
            body = collapsed(self._section(copy))
            already = missing_elements(body, copy.required)
            with self.subTest(path=copy.path):
                self.assertEqual(
                    already,
                    [],
                    f"precondition: {copy.path} still states every element, so every "
                    "forge below removes something",
                )
            if already:
                continue  # forging from an already broken section proves nothing
            for element in copy.required:
                with self.subTest(path=copy.path, element=element):
                    forged = body.replace(collapsed(element), "")
                    self.assertEqual(
                        missing_elements(forged, copy.required),
                        [element],
                        f"removing {element!r} from {copy.path} was not detected, so this "
                        "gate is vacuous",
                    )

    def test_this_gate_notices_a_removed_heading(self):
        """The other way the gate could go vacuous: the section extractor finding
        nothing after a rename, and every content assertion passing over "".
        """
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                text = (ROOT / copy.path).read_text(encoding="utf-8")
                self.assertEqual(
                    section(text.replace(copy.heading, "## Something Else"), copy.heading),
                    "",
                    "a renamed heading was still found, so the extractor is not reading "
                    "the heading it names",
                )

    def test_the_extractors_read_the_section_and_nothing_else(self):
        """Breaks if `section` starts returning the rest of the document, or if
        `commands` starts reading prose.

        The skill's instruction sits under a heading followed by many others; an
        extractor that ran past them would find every fragment somewhere and
        report green over a section that had been gutted. A `commands` that read
        prose would accept a sentence in place of the block.
        """
        document = (
            "## First\nalpha\n\n```bash\n# not a heading\ncd somewhere\n```\n\n"
            "### Second\nbeta\n\n## Third\n"
        )
        body = section(document, "## First")
        self.assertEqual(body, "\nalpha\n\n```bash\n# not a heading\ncd somewhere\n```\n")
        self.assertEqual(section(document, "## Missing"), "")
        self.assertEqual(commands(body), "# not a heading\ncd somewhere")
        self.assertNotIn("alpha", commands(body))

    def _section(self, copy: Copy) -> str:
        return section((ROOT / copy.path).read_text(encoding="utf-8"), copy.heading)


if __name__ == "__main__":
    unittest.main()
