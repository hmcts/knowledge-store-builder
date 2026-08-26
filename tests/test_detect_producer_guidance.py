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
sensitivity in the same run. The extractor and the forge are `tests/doc_sections`,
shared with the other gates over prose; what stays here is this document's
headings, its fragments and its assertions.
"""

from __future__ import annotations

import unittest

from doc_sections import (
    Copy,
    body_of,
    collapsed,
    missing_commands,
    missing_elements,
    section_after_rename,
    section_lines,
    sensitivity,
)

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


class DetectProducerGuidanceTest(unittest.TestCase):
    def test_every_guarded_section_is_present(self):
        """Breaks if either heading is renamed or removed, which would otherwise
        leave every assertion below passing over an empty string."""
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                self.assertGreater(
                    section_lines(body_of(copy)),
                    8,
                    f"{copy.path}'s {copy.heading!r} section is gone or too short to hold "
                    "the producer, the trade and the case where the route is unavailable",
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
                absent = missing_elements(body_of(copy), copy.required)
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
            with self.subTest(path=copy.path):
                absent = missing_commands(body_of(copy), REQUIRED_COMMANDS)
                self.assertEqual(
                    absent,
                    [],
                    f"{copy.path}'s {copy.heading!r} section no longer shows {absent} in "
                    "a command block, so the call survives only as prose",
                )

    def test_no_copy_invents_a_route_for_a_store_where_detect_does_not_finish(self):
        """Breaks if somebody fills the gap with a plausible substitute.

        There is no measured route for that store, and a guessed one costs more
        than the admission: the reader is already in the case the guide cannot
        answer, and would spend the estate-scale run finding that out.
        """
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                self.assertIn("no substitute", collapsed(body_of(copy)).lower())

    def test_this_gate_notices_a_dropped_element(self):
        """The sensitivity check, in the same run.

        Forges each real section with one required element removed and asserts the
        checker reports exactly that element. If this ever passes trivially, the
        assertions above are measuring the presence of a document rather than the
        producer, the trade and the two facts it has to state.
        """
        for copy in GUARDED:
            report = sensitivity(body_of(copy), copy.required)
            with self.subTest(path=copy.path):
                self.assertEqual(
                    report.already_missing,
                    [],
                    f"precondition: {copy.path} has already dropped "
                    f"{report.already_missing}, so the forges below remove nothing and "
                    "conclude nothing",
                )
                self.assertEqual(
                    report.undetected,
                    [],
                    f"{copy.path} states {report.undetected} only incidentally: removing "
                    "it is not reported as that element going missing, so the producer or "
                    "the trade could leave the document unnoticed",
                )

    def test_this_gate_notices_a_removed_heading(self):
        """The other way the gate could go vacuous: the extractor finding nothing
        after a rename, and every content assertion passing over ""."""
        for copy in GUARDED:
            with self.subTest(path=copy.path):
                self.assertEqual(
                    section_after_rename(copy),
                    "",
                    f"renaming {copy.heading!r} away in {copy.path} still yielded a "
                    "section, so the assertions above may be reading a different one",
                )


if __name__ == "__main__":
    unittest.main()
