"""The grounding contract must still be inside the skills that restate it.

`docs/grounding-and-verification.md` is the master, and `CLAUDE.md` makes
updating every copy a hard obligation in the same change. Its reasoning is that
**an agent reads the skill it was invoked with and may never open the master**,
so a superseded rule in a skill is the rule that gets applied — which makes a
drifted skill worse than one that never mentioned the contract, because it reads
as authoritative.

Nothing enforced that. `test_documented_stages.py` reads
`skills/knowledge-store-build/SKILL.md`, but only to check it declares a library
minimum version. So the one rule this repository calls most dangerous to let
drift was protected by discipline alone, and discipline is what fails when a
document is edited under time pressure by someone who has not read `CLAUDE.md`.

Three properties, and each names the break it catches:

1. **The master's mirror list is the source of truth for what to check.** The
   list is parsed rather than duplicated here, so adding a row makes it
   load-bearing immediately. A row naming a file this test has no sentences for
   is a failure with instructions, not a silent skip — otherwise the list could
   grow while coverage stood still.
2. **Every mirror carries the rule, not just the pointer.** `CLAUDE.md` says
   "Do not rely on the pointer" in as many words. A file that cites the master
   and states none of its rules is the specific drift that matters: it looks
   compliant to a reader and gives an agent nothing to apply.
3. **The parse asserts it found something.** A gate that greps an artefact can
   silently start matching nothing after a reformat, and then reports green over
   a check that no longer runs. The floor turns that into a failure.

This gate also verifies its own sensitivity in the same run, because a gate that
can only pass or fail cannot report that it has become vacuous.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "docs" / "grounding-and-verification.md"
POINTER = "docs/grounding-and-verification.md"

# The operative sentences each mirror must carry, keyed by the path the master's
# table names. Substrings rather than regexes: the point is that the *rule* is
# present in words an agent can act on, and an exact phrase is what a careless
# edit removes. Keep these to the shortest fragment that could not survive the
# rule being dropped or reversed.
REQUIRED: dict[str, tuple[str, ...]] = {
    "skills/knowledge-store/SKILL.md": (
        "Every claim traces to evidence in the store",
        "Say which layer answered",
        "Absence of evidence is a fact about the store's membership",
        "Never invent nodes, edges or tickets",
    ),
    "skills/knowledge-store-build/SKILL.md": (
        "Verify grounding, not only coverage",
        "this is not delegable to the author",
        "summaries verify",
    ),
    "skills/knowledge-store-export/SKILL.md": (
        "re-derive it",
        "before it goes in the document",
    ),
    "docs/building-a-knowledge-store.md": ("not delegable to the author",),
}

# Below this the table parse has stopped working rather than the table having
# shrunk. Four rows exist today; a real removal is a deliberate edit that should
# lower this line in the same change.
MINIMUM_MIRRORS = 4


def mirrored_paths(master_text: str) -> list[str]:
    """The paths the master's own mirror table names, in table order.

    Parsed from the master so the table is load-bearing rather than decorative.
    Matches a backticked path in the first cell of a blockquoted table row, which
    is how the master writes it.
    """
    found: list[str] = []
    for line in master_text.splitlines():
        stripped = line.lstrip("> ").strip()
        if not stripped.startswith("|"):
            continue
        first_cell = stripped.split("|")[1] if stripped.count("|") >= 2 else ""
        match = re.search(r"`([^`]+\.md)`", first_cell)
        if match:
            found.append(match.group(1))
    return found


def missing_rules(text: str, required: tuple[str, ...]) -> list[str]:
    """Which required sentences are absent. Separate so sensitivity can call it."""
    return [sentence for sentence in required if sentence not in text]


class GroundingContractMirrorsTest(unittest.TestCase):
    def setUp(self):
        self.master = MASTER.read_text(encoding="utf-8")
        self.paths = mirrored_paths(self.master)

    def test_the_mirror_table_still_parses(self):
        """Breaks if the table is reformatted such that this gate reads nothing.

        Without this the whole file would pass over an empty list — green, and
        checking nothing, which is the failure mode the gate exists to prevent
        elsewhere.
        """
        self.assertGreaterEqual(
            len(self.paths),
            MINIMUM_MIRRORS,
            f"parsed only {len(self.paths)} mirrored paths from {MASTER.name}; the table "
            "format changed and this gate is no longer reading it",
        )

    def test_every_mirrored_file_exists(self):
        """Breaks if the master points at a file that has been renamed or removed.

        A mirror list naming a path that is not there is worse than no list: it
        reads as coverage while the rule it claims to track has no home.
        """
        for path in self.paths:
            with self.subTest(path=path):
                self.assertTrue(
                    (ROOT / path).is_file(),
                    f"{MASTER.name} lists {path} as carrying the contract, but it does not exist",
                )

    def test_this_gate_knows_what_each_mirror_must_say(self):
        """Breaks if a row is added to the table without adding its sentences here.

        The alternative is that the list grows while coverage stands still, which
        would make the table look more enforced over time and be less so.
        """
        for path in self.paths:
            with self.subTest(path=path):
                self.assertIn(
                    path,
                    REQUIRED,
                    f"{MASTER.name} now mirrors into {path}, but this gate has no required "
                    "sentences for it — add them to REQUIRED so the new mirror is checked",
                )

    def test_every_mirror_states_the_rule(self):
        """Breaks if an edit removes a contract rule from a skill and leaves the rest.

        The rules are what an agent applies. This is the case `CLAUDE.md` calls
        most dangerous, because the skill still reads as authoritative afterwards.
        """
        for path in self.paths:
            required = REQUIRED.get(path)
            if required is None:
                continue  # reported by the test above; not this one's business
            with self.subTest(path=path):
                absent = missing_rules((ROOT / path).read_text(encoding="utf-8"), required)
                self.assertEqual(
                    absent,
                    [],
                    f"{path} no longer states: {absent}. The master is "
                    f"{MASTER.name}; update the copy in the same change as the master.",
                )

    def test_the_pointer_alone_is_not_compliance(self):
        """Breaks if a mirror is reduced to citing the master.

        `CLAUDE.md`: "Do not rely on the pointer." A file that references the
        contract and states none of it looks compliant to a human reader and
        gives an agent nothing to act on — so a pointer with no rules is the
        failure this asserts, not a partial pass.
        """
        for path in self.paths:
            required = REQUIRED.get(path)
            if required is None:
                continue
            text = (ROOT / path).read_text(encoding="utf-8")
            if POINTER not in text:
                continue  # a mirror need not cite the master; it must state the rule
            with self.subTest(path=path):
                self.assertNotEqual(
                    missing_rules(text, required),
                    list(required),
                    f"{path} cites {POINTER} but states none of its rules — the pointer is "
                    "not the rule, and a reader cannot tell the difference",
                )

    def test_this_gate_notices_a_removed_rule(self):
        """The sensitivity check, in the same run.

        A gate that can only pass or fail cannot report that it has gone vacuous.
        This removes a real required sentence from a real mirror's text and
        asserts the checker reports exactly that sentence — so if `missing_rules`
        is ever weakened to return nothing, this fails rather than the suite
        quietly protecting nothing.
        """
        path = "skills/knowledge-store/SKILL.md"
        required = REQUIRED[path]
        text = (ROOT / path).read_text(encoding="utf-8")
        self.assertEqual(missing_rules(text, required), [], "precondition: the mirror is intact")

        forged = text.replace(required[0], "")
        self.assertEqual(
            missing_rules(forged, required),
            [required[0]],
            "removing a contract rule from a mirror was not detected, so this gate is vacuous",
        )


if __name__ == "__main__":
    unittest.main()
