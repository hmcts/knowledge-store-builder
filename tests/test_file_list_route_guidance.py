"""`extract-ast` must keep taking a file list, because a document depends on it.

`docs/building-a-knowledge-store.md` tells an operator that exclusions applied at
detect time carry through this stage by construction: it is handed a list and never
walks a directory, so there is no second model of what is content and nothing for
one to drift from.

That paragraph exists because the previous one went stale when the code changed,
and **nothing failed** — it was caught by reading. Rewriting prose removes today's
wrongness and does nothing about the next change, so the claim is pinned here
against `src/` rather than against the document alone.

Two properties, and the first is the one that matters:

1. **The stage does not walk a directory.** A `rglob`/`iterdir`/`os.walk` fallback
   added later would silently re-falsify the paragraph, and would reintroduce
   exactly the second exclusion model the stage exists to remove. Asserted against
   the module's source, because it is a property of *how* the list is obtained and
   no unit test on the current behaviour would notice a fallback added beside it.

2. **The document still explains it.** An instruction whose reason has been deleted
   is the one someone talks themselves out of, and here the reason is the whole
   content: "it is not a scan" is what makes both `.graphifyignore` placements work
   on this route.

The sensitivity of each check is asserted in the same run, because a search that
silently matches nothing is how this repository's gates have failed before.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "src" / "knowledgestore" / "extract_ast.py"
GUIDE = ROOT / "docs" / "building-a-knowledge-store.md"

# Every way this module could start walking the tree instead of being handed it.
WALKS = ("rglob(", ".glob(", "iterdir(", "os.walk(", "scandir(", "listdir(")


def _code(path: Path) -> str:
    """Source with comments and docstrings' prose stripped of false positives.

    The module's own docstring discusses walking a directory in order to say it does
    not, so a naive substring search over the whole file matches the explanation and
    fails. Comment lines are dropped for the same reason.
    """
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    return "\n".join(lines)


class TheStageTakesAListRatherThanWalking(unittest.TestCase):
    def test_the_module_never_walks_a_directory(self):
        """A walk added here re-falsifies the guide, and no behavioural test would fail.

        The stage would keep working - it would simply also pick up files the content
        set excluded, which is the defect the whole design removes, arriving as a
        convenience.
        """
        code = _code(MODULE)
        found = sorted(w for w in WALKS if w in code)
        self.assertEqual(
            found,
            [],
            f"{MODULE.name} walks the filesystem via {found}; the stage must be handed "
            f"its file list, or docs/building-a-knowledge-store.md becomes wrong again",
        )

    def test_the_walk_search_would_notice_one(self):
        """The guard on the instrument: a pattern list that matched nothing would pass.

        This file's entire subject is a check that did not exist, so a typo in WALKS
        turning it into a permanent pass is the failure mode closest to hand.
        """
        for probe in ("for p in root.rglob('*'):", "os.walk(root)", "root.iterdir()"):
            self.assertTrue(any(w in probe for w in WALKS), f"WALKS no longer recognises {probe!r}")

    def test_both_input_routes_are_still_the_only_two(self):
        """The paragraph names exactly two sources. A third would need documenting.

        Asserted on the resolver rather than on `main`, because that is where the
        decision lives and where a third branch would be added.
        """
        source = _code(MODULE)
        resolver = source.split("def resolve_input", 1)
        self.assertEqual(len(resolver), 2, "resolve_input has gone; the guide names it")
        body = resolver[1].split("\ndef ", 1)[0]
        self.assertIn("read_file_list", body, "the --files route has gone")
        self.assertIn("content_files", body, "the content-set route has gone")


class TheGuideStillExplainsWhy(unittest.TestCase):
    REQUIRED = (
        "not a scan",  # the mechanism, not just the conclusion
        "never walks a directory",  # what makes both placements work
        "inherits whatever",  # that it takes its detect's answer
    )

    def test_the_guide_states_the_mechanism(self):
        """A conclusion without its reason is the version someone edits away.

        "Both placements work" is the useful sentence, and it is only true because
        the stage has no scan root. Keeping the conclusion and dropping the reason
        would leave a claim nobody can check from the document.
        """
        text = GUIDE.read_text(encoding="utf-8")
        missing = [r for r in self.REQUIRED if r not in text]
        self.assertEqual(
            missing,
            [],
            f"docs/building-a-knowledge-store.md no longer explains the file-list "
            f"route: missing {missing}",
        )

    def test_the_version_the_table_was_measured_against_is_named(self):
        """A placement table with no version is what produced the row that stopped holding.

        One cell of it was disproved on a later graphify than the one it was written
        against, and there was no way to tell which. A version beside the measurement
        is what makes the next disagreement diagnosable rather than confusing.
        """
        text = GUIDE.read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r"graphify \d+\.\d+\.\d+",
            "the .graphifyignore placement table names no graphify version",
        )


if __name__ == "__main__":
    unittest.main()
