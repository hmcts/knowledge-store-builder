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
and the gate asserts its own sensitivity in the same run. The extractor and the
forge are `tests/doc_sections`, shared with the other gates over prose; what
stays here is this instruction's headings, its fragments and its assertions.
"""

from __future__ import annotations

import unittest

from doc_sections import (
    Copy,
    body_of,
    missing_commands,
    missing_elements,
    section_after_rename,
    section_lines,
    sensitivity,
)

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


class StoreRootExtractionGuidanceTest(unittest.TestCase):
    def test_every_guarded_section_is_present(self):
        """Breaks if a heading is renamed or removed, which would otherwise leave
        every assertion below passing over an empty string."""
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                self.assertGreater(
                    section_lines(body_of(copy)),
                    8,
                    f"{copy.path}'s {copy.heading!r} section is gone or too short to hold "
                    "the instruction and its reason",
                )

    def test_every_copy_states_the_prohibition_and_the_mechanism(self):
        """Breaks if either copy loses the instruction, and -- the point of the
        gate -- if it keeps the imperative and drops the reason for it."""
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                absent = missing_elements(body_of(copy), copy.required)
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
            with self.subTest(path=copy.path):
                absent = missing_commands(body_of(copy), REQUIRED_COMMANDS)
                self.assertEqual(
                    absent,
                    [],
                    f"{copy.path}'s {copy.heading!r} section no longer shows {absent} in a "
                    "command block, so it prohibits a route without showing the one to "
                    "take",
                )

    def test_this_gate_notices_a_dropped_element(self):
        """The sensitivity check, in the same run.

        Forges each real section with one required element removed and asserts the
        checker reports exactly that element. If this ever passes trivially, the
        assertions above are measuring the presence of a document rather than the
        prohibition and the mechanism it has to carry.
        """
        for copy in GUARDED:
            report = sensitivity(body_of(copy), copy.required)
            with self.subTest(path=copy.path):
                self.assertEqual(
                    report.already_missing,
                    [],
                    f"precondition: {copy.path} no longer states "
                    f"{report.already_missing}, so nothing below is a forge",
                )
                self.assertEqual(
                    report.undetected,
                    [],
                    f"removing {report.undetected} from {copy.path} was not attributed to "
                    "that element, so this gate would not notice the mechanism being "
                    "dropped while the imperative stayed",
                )

    def test_this_gate_notices_a_removed_heading(self):
        """The other way the gate could go vacuous: the section extractor finding
        nothing after a rename, and every content assertion passing over "".
        """
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                self.assertEqual(
                    section_after_rename(copy),
                    "",
                    f"the extractor found a section in {copy.path} after "
                    f"{copy.heading!r} was renamed away, so it is not reading the "
                    "heading it was given",
                )


if __name__ == "__main__":
    unittest.main()
