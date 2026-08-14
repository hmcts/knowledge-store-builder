"""The manifest must say what was searched, not only what was found.

A store that lacks a repository still knows it lacks it - the manifest lists what
is in. A store that never looked at a whole code host reports nothing at all, and
its answers are silently narrower than they appear.

A published finding once concluded a payload schema had never been readable in
one place because its `$ref`s did not resolve. They resolved perfectly, against a
repository outside the estate: drawn honestly from what was indexed, and false.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import build_knowledge_context as ctx  # noqa: E402


class ScopeStatementTest(SettingsIsolated):
    def test_it_names_the_organisation_that_was_searched(self):
        config.configure(GITHUB_ORG="some-org")
        text = "\n".join(ctx.scope_statement(163))
        self.assertIn("some-org", text)
        self.assertIn("163", text)

    def test_it_says_what_an_absence_means(self):
        config.configure(GITHUB_ORG="some-org")
        text = "\n".join(ctx.scope_statement(1))
        self.assertIn(
            "membership",
            text,
            "the point is not that the scope is narrow but that absence cannot be "
            "read as a fact about the estate",
        )

    def test_an_unconfigured_org_still_declares_the_limit(self):
        """The claim is about having searched one host, which holds however the
        organisation is configured - so this must not fall silent."""
        config.configure(GITHUB_ORG="")
        text = "\n".join(ctx.scope_statement(5))
        self.assertIn("one organisation", text)
        self.assertIn("never searched", text)

    def test_the_statement_reaches_the_written_manifest(self):
        """Behaviour covered above is worth nothing if the section is not
        actually emitted - the gap that shipped an unwired check twice."""
        config.configure(GITHUB_ORG="some-org")
        ctx.build_manifest([])
        written = config.MANIFEST_PATH.read_text(encoding="utf-8")
        self.assertIn("What this manifest does not cover", written)
        self.assertIn("some-org", written)


if __name__ == "__main__":
    unittest.main()
