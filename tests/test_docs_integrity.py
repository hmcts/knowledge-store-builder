"""The docs-integrity gate, driven one check at a time and then as a whole.

Two directions for each check, because either on its own is worthless. A check
that cannot fire is decoration; a check that fires on a correct repository gets
switched off. So every check here is run against the real documents (it must
have nothing to say) and against a forged repository where the thing it guards
is broken (it must name it, and name only it).

The third direction is vacuity, which is the one a prose gate loses silently.
`Report.read` carries it: the checks are asserted to have looked at a floor of
real declarations and real links, and the runner is asserted to fail rather
than pass when a check read nothing. Without that, an extractor that stopped
matching would report the same clean result as a correct repository.

The floors are deliberately well under today's counts. They are there to catch
an extractor that has stopped reading, not to pin a number that moves whenever
a document is rewrapped.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from docs_integrity import (
    CHECKS,
    DECLARATION,
    ROOT,
    anchor_slug,
    declarations,
    declared_anchors_resolve,
    documents,
    heading_anchors,
    internal_links_resolve,
    main,
    relative_links,
)

# Floors, not counts. Well under what the repository holds today.
DECLARATIONS_FLOOR = 5
LINKS_FLOOR = 30


def forge(root: Path, files: dict[str, str]) -> None:
    """Write a small repository. Paths are repository-relative, text verbatim."""
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def run(root: Path) -> tuple[int, str, str]:
    """The runner's exit code, stdout and stderr for one forged repository."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(root)
    return code, out.getvalue(), err.getvalue()


class RealRepositoryTest(unittest.TestCase):
    """The quiet direction. Every check has to pass on the documents as they are."""

    def test_every_declared_anchor_still_has_a_heading(self):
        """Breaks when a heading another repository deep-links to is renamed or
        removed - the failure the declaration file exists for. Fixing it means
        restoring the heading, or changing the consumer and the declared line
        together; it does not mean deleting the line."""
        report = declared_anchors_resolve(ROOT)
        self.assertEqual(report.problems, [])

    def test_every_internal_link_resolves(self):
        """Breaks when a document is renamed, moved or deleted while something
        still links to it, or when a heading one of these documents targets is
        renamed. Both are silent on GitHub."""
        report = internal_links_resolve(ROOT)
        self.assertEqual(report.problems, [])

    def test_the_gate_passes_as_a_whole(self):
        """Breaks if any check fires on a correct repository, which is how a
        gate gets switched off. Also pins the exit-code contract: 0 on a clean
        run, with the per-check result on stdout."""
        code, out, err = run(ROOT)
        self.assertEqual(code, 0, err)
        for gate in CHECKS:
            self.assertIn(gate.name, out)


class NotVacuousTest(unittest.TestCase):
    """What stops each check reading as compliance for something it never read."""

    def test_the_declared_anchor_check_read_the_declaration(self):
        """Breaks if the declaration is emptied, renamed, or its parser stops
        matching. All three leave `problems` empty, which is indistinguishable
        from a pass unless the count is asserted."""
        self.assertGreaterEqual(declared_anchors_resolve(ROOT).read, DECLARATIONS_FLOOR)

    def test_the_declaration_exercises_the_anchor_half(self):
        """Breaks if the declaration is reduced to bare file paths. Those check
        only that a file exists, so the anchor comparison - the half that
        catches a rename - would never run. `#` is counted here rather than in
        the module, so the assertion does not rest on the parser under test."""
        self.assertTrue([entry for entry in declarations(ROOT) if "#" in entry])

    def test_the_link_check_read_the_documents(self):
        """Breaks if the link extractor stops matching, or the document walk
        stops finding files. Either reports no problems over nothing read."""
        self.assertGreaterEqual(internal_links_resolve(ROOT).read, LINKS_FLOOR)

    def test_the_documents_exercise_both_halves_of_the_link_check(self):
        """Breaks if the documents lose every in-page anchor, or every plain
        file link. The check would then be silent about the half it no longer
        sees, and silence would read as compliance."""
        targets = [
            target
            for document in documents(ROOT)
            for _, target in relative_links(document.read_text(encoding="utf-8"))
        ]
        self.assertTrue([target for target in targets if "#" in target])
        self.assertTrue([target for target in targets if "#" not in target])

    def test_a_check_that_read_nothing_fails_instead_of_passing(self):
        """Breaks if the read-nothing refusal is dropped.

        An empty repository has no declaration and no documents, so both checks
        report no problems. Reported as a pass, that is the exact shape of a
        gate gone vacuous - so the runner has to fail and say which check
        looked at nothing.
        """
        with TemporaryDirectory() as directory:
            code, _, err = run(Path(directory))
        self.assertEqual(code, 1)
        for gate in CHECKS:
            self.assertIn(gate.name, err)
        self.assertIn("looked at nothing", err)

    def test_every_check_function_is_in_the_registry(self):
        """Breaks when a check is written and not registered, which leaves it
        never run - the reason the registry is a listed tuple rather than
        import-time decoration is that both halves stay readable here."""
        self.assertEqual(
            {gate.run for gate in CHECKS},
            {declared_anchors_resolve, internal_links_resolve},
        )


class DeclaredAnchorTest(unittest.TestCase):
    """The firing direction for the declared-anchor check, on forged documents."""

    GUIDE = "# Guide\n\n## Install the thing\n\nRun it.\n"
    DECLARED = "# a comment\n\ndocs/guide.md\ndocs/guide.md#install-the-thing\n"

    def forge_store(self, root: Path, guide: str) -> None:
        forge(root, {"docs/guide.md": guide, str(DECLARATION): self.DECLARED})

    def test_it_is_quiet_when_the_declared_heading_is_there(self):
        """The baseline the two failures below are measured against. Without it
        a failure proves nothing: a check that fires on everything fires here
        too."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.forge_store(root, self.GUIDE)
            report = declared_anchors_resolve(root)
        self.assertEqual(report.problems, [])
        self.assertEqual(report.read, 2)

    def test_a_renamed_heading_is_named(self):
        """Breaks if a rename stops being detected. This is the incident: the
        heading is still a heading, the document still reads well, and the
        consumer's link is dead."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.forge_store(root, self.GUIDE.replace("## Install the thing", "## Set it up"))
            report = declared_anchors_resolve(root)
        self.assertEqual(len(report.problems), 1)
        self.assertIn("#install-the-thing", report.problems[0])
        self.assertEqual(report.read, 2)

    def test_a_declared_document_that_is_gone_is_named(self):
        """Breaks if a declared entry with no anchor stops being checked, which
        would leave a renamed or deleted guide undetected."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            forge(root, {str(DECLARATION): self.DECLARED})
            report = declared_anchors_resolve(root)
        self.assertEqual(len(report.problems), 2)
        self.assertIn("no such file", report.problems[0])

    def test_comments_and_blank_lines_are_not_declarations(self):
        """Breaks if the parser starts reading its own comments as entries. The
        file explains an incident at length; every line of that explanation
        would become a failing declaration."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            forge(
                root,
                {
                    "docs/guide.md": self.GUIDE,
                    str(DECLARATION): "# docs/nothing.md\n\n   \ndocs/guide.md\n",
                },
            )
            report = declared_anchors_resolve(root)
        self.assertEqual(report.read, 1)
        self.assertEqual(report.problems, [])


class InternalLinkTest(unittest.TestCase):
    """The firing direction for the link check, on forged documents."""

    def test_it_is_quiet_when_both_kinds_of_link_resolve(self):
        """The baseline. A file link and an in-page anchor, both good."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            forge(
                root,
                {
                    "README.md": "# Top\n\nSee [the guide](docs/guide.md).\n",
                    "docs/guide.md": "# Guide\n\n[up](../README.md) and [here](#a-part)\n\n## A part\n",
                },
            )
            report = internal_links_resolve(root)
        self.assertEqual(report.problems, [])
        self.assertEqual(report.read, 3)

    def test_a_link_to_a_missing_file_is_named(self):
        """Breaks if a moved or deleted document stops being detected."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            forge(root, {"README.md": "# Top\n\n[gone](docs/gone.md)\n"})
            report = internal_links_resolve(root)
        self.assertEqual(len(report.problems), 1)
        self.assertIn("README.md:3", report.problems[0])
        self.assertIn("no such file", report.problems[0])

    def test_a_link_to_a_missing_anchor_is_named(self):
        """Breaks if the anchor half stops running, which is the more likely of
        the two: the file is right there, so nothing else notices."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            forge(
                root,
                {
                    "README.md": "# Top\n\n[part](docs/guide.md#a-part)\n",
                    "docs/guide.md": "# Guide\n\n## Another part\n",
                },
            )
            report = internal_links_resolve(root)
        self.assertEqual(len(report.problems), 1)
        self.assertIn("#a-part", report.problems[0])

    def test_an_anchor_on_a_non_markdown_target_is_named_not_skipped(self):
        """Breaks if unverifiable anchors are silently passed. The file exists,
        the anchor cannot be read, and reporting that as a pass is a claim
        about something the check never looked at."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            forge(root, {"README.md": "# Top\n\n[a line](script.py#L10)\n", "script.py": "x = 1\n"})
            report = internal_links_resolve(root)
        self.assertEqual(len(report.problems), 1)
        self.assertIn("cannot be checked", report.problems[0])

    def test_external_links_are_left_alone(self):
        """Breaks if an off-repository URL starts being resolved as a path.
        Every `https://` link in these documents would then fail, and the gate
        would be useless on its first run."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            forge(
                root, {"README.md": "# Top\n\n[out](https://example.com/x) [mail](mailto:a@b.c)\n"}
            )
            report = internal_links_resolve(root)
        self.assertEqual(report.read, 0)
        self.assertEqual(report.problems, [])

    def test_a_link_inside_a_fenced_block_is_not_a_link(self):
        """Breaks if fence tracking is dropped in the link extractor. An example
        naming a path inside somebody else's store would then fail a check that
        has no way to see their store."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            forge(root, {"README.md": "# Top\n\n```md\n[example](their/store/notes.md)\n```\n"})
            report = internal_links_resolve(root)
        self.assertEqual(report.read, 0)
        self.assertEqual(report.problems, [])

    def test_an_unclosed_fence_is_reported_rather_than_passed_over(self):
        """Breaks if a document that stops being read starts reading as clean.

        Skipping fences means an odd one swallows the rest of the file: the
        broken link below it is never looked at, and the check would otherwise
        report no problems about a document it half read. That is the partial
        vacuity no problem count can show.
        """
        with TemporaryDirectory() as directory:
            root = Path(directory)
            forge(root, {"README.md": "# Top\n\n```bash\nls\n\n[gone](docs/gone.md)\n"})
            report = internal_links_resolve(root)
        self.assertEqual(report.read, 0)
        self.assertEqual(len(report.problems), 1)
        self.assertIn("README.md:3", report.problems[0])
        self.assertIn("unclosed code fence", report.problems[0])

    def test_balanced_fences_are_not_reported(self):
        """The baseline for the check above. Every document here holds fenced
        blocks, so a fence check that fired on a balanced pair would fail on
        all of them."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            forge(root, {"README.md": "# Top\n\n```bash\nls\n```\n\n[here](#top)\n"})
            report = internal_links_resolve(root)
        self.assertEqual(report.problems, [])
        self.assertEqual(report.read, 1)


class HeadingAnchorTest(unittest.TestCase):
    """The slugs, written out by hand rather than derived from the code."""

    def test_it_lowercases_drops_punctuation_and_hyphenates_spaces(self):
        """Breaks if the slug stops matching what GitHub puts in a URL, which
        would make both checks report failures nobody can act on."""
        self.assertEqual(anchor_slug("Install the prerequisites"), "install-the-prerequisites")
        self.assertEqual(anchor_slug("What do you need?"), "what-do-you-need")
        self.assertEqual(anchor_slug("Use `explorer.html`"), "use-explorerhtml")
        self.assertEqual(
            anchor_slug("5. Gate before you build, not after"),
            "5-gate-before-you-build-not-after",
        )

    def test_a_hash_inside_a_fenced_block_is_not_a_heading(self):
        """Breaks if fence tracking is dropped in the heading extractor.

        These documents are full of shell blocks whose first line is a `#`
        comment. Read as headings, those invent anchors - so a link to
        `#comments-must-be-on-their-own-line` would resolve, and a declared
        anchor could be satisfied by a comment in a code block rather than by
        the heading it names.
        """
        document = "# Title\n\n```bash\n# Not a heading\n```\n\n## Real heading\n"
        self.assertEqual(heading_anchors(document), ["title", "real-heading"])

    def test_repeated_headings_get_githubs_numbered_suffixes(self):
        """Breaks if duplicates collapse. Two of these guides carry a
        `## Troubleshooting`; a link to the second resolves as `-1` on GitHub
        and would be reported as broken."""
        document = "## Troubleshooting\n\n## Elsewhere\n\n## Troubleshooting\n"
        self.assertEqual(
            heading_anchors(document),
            ["troubleshooting", "elsewhere", "troubleshooting-1"],
        )

    def test_it_reads_every_heading_level(self):
        """Breaks if only some levels are read. The install sections are `##`
        in one guide and `###` in another, so a checker reading one level would
        pass over half the declared list."""
        document = "# One\n\n### Three\n\n###### Six\n"
        self.assertEqual(heading_anchors(document), ["one", "three", "six"])


if __name__ == "__main__":
    unittest.main()
