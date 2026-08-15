"""The clustering pin must survive in the documented command.

The build skill tells operators to export `PYTHONHASHSEED=0` before clustering,
because on two estates the same graph file produced different community
memberships in different processes, and summaries are keyed by community id.

Guidance is only worth what an executable check makes it worth. This one is
narrow on purpose: it asserts the instruction is present and *active*, which is
the failure a store would actually suffer - the line commented out, or set to a
value that reads like a pin and is not.

Deliberately NOT tested here: that pinned clustering is reproducible across
processes. In this environment unpinned clustering is reproducible too - three
attempts, including a real 2,832-node community, never varied - so such a test
would pass whether or not the pin worked. A test that cannot fail is the defect
this suite spent the week finding in other people's code.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

SKILL = Path(__file__).resolve().parent.parent / "skills" / "knowledge-store-build" / "SKILL.md"

# An export that is not commented out. `"PYTHONHASHSEED=0" in text` is not
# enough: it stays true when the line is commented, which left an equivalent
# check green on another repository.
ACTIVE_EXPORT = re.compile(r"^\s*export\s+PYTHONHASHSEED=0\s*$", re.MULTILINE)


class ClusteringPinDocumentedTest(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text(encoding="utf-8")

    def test_the_build_skill_exports_the_pin(self):
        self.assertRegex(
            self.text,
            ACTIVE_EXPORT,
            "the clustering pin is the one line standing between a rebuild and "
            "silently re-keyed summaries",
        )

    def test_a_commented_out_pin_would_not_satisfy_this(self):
        """Guards the check itself: the obvious substring test passes on a
        commented line, which is how this fails in practice."""
        self.assertNotRegex("# export PYTHONHASHSEED=0\n", ACTIVE_EXPORT)

    def test_a_plausible_but_inert_value_would_not_satisfy_this(self):
        """`PYTHONHASHSEED=random` is legal, reads like "vary the seed", and
        disables the pin. It is the mistake someone will actually make."""
        self.assertNotRegex("export PYTHONHASHSEED=random\n", ACTIVE_EXPORT)

    def test_the_skill_says_why_rather_than_only_what(self):
        """An unexplained export gets removed by the next person tidying up."""
        window = self.text[max(0, self.text.find("PYTHONHASHSEED") - 700) :]
        self.assertIn("community id", window)


if __name__ == "__main__":
    unittest.main()
