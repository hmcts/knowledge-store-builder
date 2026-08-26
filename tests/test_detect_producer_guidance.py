"""An operator must be told how the detect result is produced, and what it costs.

Three stages read `graphify-out/.graphify_detect.json` and **no document said how
that file comes to exist** (#236). The guides said only "once graphify has scanned
the corpus", which reads like a command until you look for one: graphify's CLI has
no `detect` subcommand, and `graphify update .` at the store root exits 0 writing
no detect result. The producer is `detect(root, gitignore=False)` through
graphify's Python API, redirected into the file.

Naming the call is only half of it. `gitignore=False` is required for a store-root
scan — the alternative is classifying almost nothing, which is the original fault —
and it is not free: it moves the exclusion burden off `.gitignore`, which each
repository maintains for its own reasons, onto a `.graphifyignore` this pipeline
installs and `sync` deletes on every refresh. A copy that keeps the command and
drops that trade is the version an operator acts on without knowing what widened,
so both halves are pinned here.

Two supporting facts are pinned with it because a reader assumes the opposite of
both, and either assumption produces a wrong decision: `gitignore=False` does not
disable `.graphifyignore`, and for content a repository tracks the parameter
changes nothing at all.

The last element is the honest one. On at least one real estate the scan does not
finish at estate scale, so this route is unavailable there and the document says so
rather than inventing a substitute. A guide that only describes the path that works
sends that operator round the loop that already failed.

Content is pinned rather than wording, sections are extracted by heading rather
than searched for across the whole document, and the gate asserts its own
sensitivity in the same run - following `tests/test_store_root_extraction_guidance.py`,
which guards the neighbouring instruction the same way.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Copy:
    """One document's copy of the guidance, and what it must still say."""

    path: str
    heading: str
    required: tuple[str, ...]


# Each fragment is the shortest one that cannot survive its element being dropped,
# so the surrounding prose stays free to change while the decision does not.
REQUIRED = (
    # the producer: nothing in the CLI writes the file, and this is the call
    "graphify's CLI writes it",
    "graphify.detect",
    "gitignore=False",
    ".graphify_detect.json",
    # why the parameter is required, which is the whole reason the route exists
    "`repositories/` is in",
    # the trade, in its two halves: what widens, and why the guard is fragile
    "`.graphifyignore` re-excludes it",
    "forgotten reinstall",
    # the two facts a reader assumes the other way round
    "does not disable `.graphifyignore`",
    "the parameter changes nothing",
    # the estate where the route is unavailable, and no invented remedy
    "the scan does not finish",
)

# The skill says the same things in its own register, so the fragments differ where
# the wording has to. Only `repositories/` is phrased differently enough to need
# its own entry.
SKILL_REQUIRED = tuple(
    "`repositories/` is in the" if element == "`repositories/` is in" else element
    for element in REQUIRED
)

GUARDED = (
    Copy(
        "docs/creating-a-store.md",
        "### Write the detect result, then expose the content set",
        REQUIRED,
    ),
    Copy(
        "skills/knowledge-store-build/SKILL.md",
        "### Write the detect result, then expose the content set",
        SKILL_REQUIRED,
    ),
)

# The command itself, required inside a fenced block rather than in prose: a block
# is what an operator copies, and this one was run before it was written down.
REQUIRED_COMMANDS = (
    "mkdir -p graphify-out",
    "from graphify.detect import detect",
    "gitignore=False",
    "> graphify-out/.graphify_detect.json",
)


def section(text: str, heading: str) -> str:
    """The heading's own body, up to the next heading, or "" if it is gone.

    Extracted rather than searched for, so a fragment appearing elsewhere in the
    document cannot stand in for this section being present. Headings inside
    fenced blocks are not headings - a shell comment opens with `#` too.
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
    """Only what is inside the section's fenced blocks."""
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


class DetectProducerGuidanceTest(unittest.TestCase):
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
                    f"{copy.path}'s {copy.heading!r} section is too short to hold the "
                    "producer, the trade and the case where the route is unavailable",
                )

    def test_every_copy_names_the_producer_and_states_the_trade(self):
        """Breaks if either copy loses the call, the parameter, the trade or the
        two facts.

        The one that matters most is the trade: a copy naming the command without
        it reads as a recommendation, and the operator who acts on it has widened
        the content set to everything the estate's repositories generate and
        ignore.
        """
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                absent = missing_elements(self._section(copy), copy.required)
                self.assertEqual(
                    absent,
                    [],
                    f"{copy.path}'s {copy.heading!r} section no longer states: {absent}",
                )

    def test_every_copy_shows_the_command_in_a_block(self):
        """Breaks if the call survives only as prose.

        A reader copies the block. The `mkdir` is in it because the shell opens the
        redirect target before python runs, so without the directory the command
        fails and nothing has scanned anything.
        """
        for copy in GUARDED:
            block = commands(self._section(copy))
            for command in REQUIRED_COMMANDS:
                with self.subTest(path=copy.path, command=command):
                    self.assertIn(
                        command,
                        block,
                        f"{copy.path}'s {copy.heading!r} section no longer shows "
                        f"`{command}` in a command block",
                    )

    def test_no_copy_invents_a_route_for_a_store_where_detect_does_not_finish(self):
        """Breaks if somebody fills the gap with a plausible substitute.

        There is no measured route for that store, and a guessed one costs more
        than the admission: the reader is already in the case the guide cannot
        answer, and would spend the estate-scale run finding that out.
        """
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                flat = collapsed(self._section(copy)).lower()
                self.assertIn("no substitute", flat)

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
        """The other way the gate could go vacuous: the extractor finding nothing
        after a rename, and every content assertion passing over ""."""
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                text = (ROOT / copy.path).read_text(encoding="utf-8")
                self.assertEqual(
                    section(text.replace(copy.heading, "### Something Else"), copy.heading),
                    "",
                    "a renamed heading was still found, so the extractor is not reading "
                    "the heading it names",
                )

    def test_the_extractors_read_the_section_and_nothing_else(self):
        """Breaks if `section` starts returning the rest of the document, or if
        `commands` starts reading prose.

        Both documents continue past this section, so an extractor running on would
        find every fragment somewhere and report green over a section that had been
        gutted.
        """
        document = (
            "### First\nalpha\n\n```bash\n# not a heading\nmkdir -p graphify-out\n```\n\n"
            "#### Second\nbeta\n\n### Third\n"
        )
        body = section(document, "### First")
        self.assertEqual(body, "\nalpha\n\n```bash\n# not a heading\nmkdir -p graphify-out\n```\n")
        self.assertEqual(section(document, "### Missing"), "")
        self.assertEqual(commands(body), "# not a heading\nmkdir -p graphify-out")
        self.assertNotIn("alpha", commands(body))

    def _section(self, copy: Copy) -> str:
        return section((ROOT / copy.path).read_text(encoding="utf-8"), copy.heading)


if __name__ == "__main__":
    unittest.main()
