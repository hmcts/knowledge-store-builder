"""Office documents must reach extraction, and must not drag fixtures in with them.

graphify is blind to Office formats, so a repository carrying a design document
or a field mapping contributes its filename and nothing else. Measured on two
estates: 805 Office files and 443 CSVs on one, 503 and 253 on the other.

The two design constraints are asserted here because both are silent when wrong:
the Markdown must land beside its source or `source_file` stops being
repository-relative and the file-to-ticket join dies with every count healthy,
and fixture trees must stay out or the store fills with assertions rather than
answers.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledgestore import convert_documents as convert  # noqa: E402


class Fake:
    """Stands in for markitdown, which is an optional dependency."""

    def __init__(self, text="# Heading\n\nbody", error=None):
        self.text, self.error = text, error

    def convert(self, path):
        if self.error:
            raise self.error
        return type("Result", (), {"text_content": self.text})()


class FixtureFilterTest(unittest.TestCase):
    def test_conventional_test_trees_are_excluded(self):
        for path in (
            "repo/src/test/resources/expected/report.xlsx",
            "repo/src/integrationTest/resources/stub.csv",
            "repo/functional_tests/data.xlsx",
            "repo/target/test-classes/x.docx",
            "repo/node_modules/pkg/readme.docx",
        ):
            self.assertTrue(convert.is_fixture(Path(path)), path)

    def test_ordinary_documents_are_not_excluded(self):
        """The pattern is anchored so a word merely ending in "test" survives -
        `latest` is the one that would otherwise take a docs tree with it."""
        for path in (
            "repo/docs/reference/Architecture HLD.docx",
            "repo/docs/latest/HLD.docx",
            "repo/contest/entry.docx",
            "repo/knowledge-transfer/Ldap_KT.docx",
        ):
            self.assertFalse(convert.is_fixture(Path(path)), path)


class ConversionTest(unittest.TestCase):
    def _corpus(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "repo-a" / "docs").mkdir(parents=True)
        (root / "repo-a" / "src" / "test").mkdir(parents=True)
        (root / "repo-a" / "docs" / "HLD.docx").write_bytes(b"x")
        (root / "repo-a" / "src" / "test" / "expected.xlsx").write_bytes(b"x")
        (root / "repo-a" / "docs" / "notes.md").write_text("already readable", encoding="utf-8")
        return root

    def test_only_convertible_non_fixture_documents_are_selected(self):
        found = convert.convertible_documents(self._corpus())
        self.assertEqual([p.name for p in found], ["HLD.docx"])

    def test_the_markdown_lands_beside_its_source(self):
        """Anywhere else and `source_file` stops being repository-relative,
        which kills the file-to-ticket join while every count stays healthy."""
        root = self._corpus()
        source = root / "repo-a" / "docs" / "HLD.docx"
        self.assertTrue(convert.convert(source, Fake()))
        written = source.with_name("HLD.docx.converted.md")
        self.assertTrue(written.is_file())
        self.assertEqual(
            written.parent,
            source.parent,
            "a converted document filed under another root joins nothing",
        )
        self.assertIn("Heading", written.read_text(encoding="utf-8"))

    def test_an_unreadable_document_is_reported_not_raised(self):
        """One corrupt spreadsheet must not cost the whole estate's run."""
        root = self._corpus()
        source = root / "repo-a" / "docs" / "HLD.docx"
        self.assertFalse(convert.convert(source, Fake(error=ValueError("corrupt"))))
        self.assertFalse(source.with_name("HLD.docx.converted.md").exists())

    def test_an_empty_conversion_writes_nothing(self):
        """A document that yields no text is worse than absent: it would add a
        node with a name and no content, which cannot be cited or explained."""
        root = self._corpus()
        source = root / "repo-a" / "docs" / "HLD.docx"
        self.assertFalse(convert.convert(source, Fake(text="   \n")))
        self.assertFalse(source.with_name("HLD.docx.converted.md").exists())

    def test_a_missing_corpus_is_not_an_error(self):
        self.assertEqual(convert.convertible_documents(Path("/does/not/exist")), [])


if __name__ == "__main__":
    unittest.main()
