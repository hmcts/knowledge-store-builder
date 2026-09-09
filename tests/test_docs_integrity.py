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
    MASTER,
    MIRRORS,
    RETIRED,
    ROOT,
    anchor_slug,
    declarations,
    declared_anchors_resolve,
    declared_entries,
    documents,
    fenced_lines,
    heading_anchors,
    instructed,
    internal_links_resolve,
    main,
    master_mirror_list,
    master_prose,
    mirrored_contract_agrees,
    mirrors_are_declared,
    paired,
    relative_links,
    retired_instructions_stay_retired,
)

# Floors, not counts. Well under what the repository holds today.
DECLARATIONS_FLOOR = 5
LINKS_FLOOR = 30
STATEMENTS_FLOOR = 10
CANDIDATES_FLOOR = 3
COMPARISONS_FLOOR = 100

# A forged repository for the mirror checks: a master that states two rules and
# declares one mirror, and the mirror that carries them. The mirror wraps one
# statement across a line break, because the real documents do and a checker
# that matched raw text could not read them.
FORGED_MASTER = """# Contract

Everything here is the master.

> **This document is the master, and these rules are mirrored.**
>
> | Mirrored in | Which part |
> |---|---|
> | `skills/example/SKILL.md` — its rules | every claim carries its evidence |

## The rule

Two rules. The first is that every claim carries its evidence, and the second is
that interpretation is allowed - invention is not.
"""

FORGED_MIRROR = """# Example

When you answer, every claim carries
its evidence, and interpretation is allowed - inventing what the evidence does
not show is not.
"""

FORGED_DECLARATION = """# what each mirror shares with the master

skills/example/SKILL.md :: every claim carries its evidence
skills/example/SKILL.md :: interpretation is allowed
"""

# A forged retired-instruction list, with a replacement so the failure carries
# a remedy rather than only a refusal.
FORGED_RETIRED = "# retired\n\nold-tool . :: run it from inside each directory instead\n"


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

    def test_every_mirrored_statement_is_at_both_ends(self):
        """Breaks when the master is reworded and a skill keeps the superseded
        rule, which is the drift `CLAUDE.md` calls the dangerous one, and when a
        copy is edited away from a master that has not moved. Also breaks when
        the master's mirror list and the declaration disagree about which files
        carry a copy."""
        report = mirrored_contract_agrees(ROOT)
        self.assertEqual(report.problems, [])

    def test_no_document_restates_the_contract_undeclared(self):
        """Breaks when a document starts carrying the contract's rules without
        the master's mirror list naming it, which leaves it out of reach of the
        next edit to the master."""
        report = mirrors_are_declared(ROOT)
        self.assertEqual(report.problems, [])

    def test_no_command_block_runs_a_retired_instruction(self):
        """Breaks when a retired instruction reappears in a block a reader
        copies - the `graphify .` incident, which one document held for as long
        as it did because nothing compared it with the skill."""
        report = retired_instructions_stay_retired(ROOT)
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

    def test_the_mirror_check_read_the_declaration(self):
        """Breaks if the mirror declaration is emptied or its `path :: statement`
        parse stops matching. Both leave `problems` empty over nothing checked,
        which is indistinguishable from every copy agreeing."""
        self.assertGreaterEqual(mirrored_contract_agrees(ROOT).read, STATEMENTS_FLOOR)

    def test_the_master_is_read_without_its_own_mirror_list(self):
        """Breaks if the blockquote exclusion is dropped, or if it swallows the
        master's prose.

        Both directions matter. Left in, the master's mirror list paraphrases
        its rules closely enough to satisfy a declared statement, so the check
        would compare the table with itself. Taken too far - dropping the whole
        document - every statement would fail at once.
        """
        prose = master_prose(ROOT)
        self.assertNotIn("Mirrored in |", prose)
        self.assertIn("traces to evidence in the store", prose)

    def test_the_masters_mirror_list_is_still_being_parsed(self):
        """Breaks if the master's declarations are reformatted such that no path
        is read out of them. Every declared statement would then be reported as
        undeclared by the master - loud, but for the wrong reason, and the fix
        would look like deleting declarations."""
        self.assertIn("skills/knowledge-store/SKILL.md", master_mirror_list(ROOT))

    def test_the_undeclared_mirror_check_read_the_documents(self):
        """Breaks if the document walk stops finding the files that are not
        already declared mirrors. Nothing to scan reports nothing carried."""
        self.assertGreaterEqual(mirrors_are_declared(ROOT).read, CANDIDATES_FLOOR)

    def test_the_retired_instruction_check_read_command_blocks(self):
        """Breaks if the fenced-line extractor stops matching, or the retired
        list is emptied. Either makes the comparison count zero while the check
        still reports no problems."""
        self.assertGreaterEqual(retired_instructions_stay_retired(ROOT).read, COMPARISONS_FLOOR)

    def test_the_documents_still_hold_command_blocks_to_read(self):
        """Breaks if these documents lose their fenced blocks, or the extractor
        stops seeing them. The retired-instruction check reads nothing else, so
        its silence would then be about prose it never looks at. Counted here
        rather than in the module, so the assertion does not rest on the count
        under test."""
        blocks = [
            line
            for document in documents(ROOT)
            for _, line in fenced_lines(document.read_text(encoding="utf-8"))
        ]
        self.assertGreaterEqual(len(blocks), COMPARISONS_FLOOR)

    def test_every_route_into_a_store_is_read(self):
        """Breaks if a document a reader copies commands out of stops being read.

        `CHEATSHEET.md` is the reason this test exists rather than the example
        it uses. It is one of the three routes into a store and it sat outside
        `DOC_ROOTS`, so every prose gate here reported "nothing to report" over
        a file none of them opened - which is how #346 came to have two
        documents instructing `--no-cluster` while the build skill forbade it.
        Dropping it from `DOC_ROOTS` again would restore that silence and no
        other assertion in this module would fall, because every one of them is
        a floor and the remaining documents clear it on their own.
        """
        read = {document.relative_to(ROOT).as_posix() for document in documents(ROOT)}
        for route in ("README.md", "CHEATSHEET.md", "docs/creating-a-store.md"):
            self.assertIn(route, read, f"{route} is a route into a store and is not read")

    def test_the_real_retired_list_is_not_empty(self):
        """Breaks if the retired instruction is removed from the list rather
        than from the documents. The comparison count would fall to zero and the
        runner would report that, but the list is the substance of the check and
        an empty one protects nothing."""
        self.assertTrue(paired(declared_entries(ROOT, RETIRED)))

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
            {
                declared_anchors_resolve,
                internal_links_resolve,
                mirrored_contract_agrees,
                mirrors_are_declared,
                retired_instructions_stay_retired,
            },
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


class MirroredContractTest(unittest.TestCase):
    """The firing direction for the mirrored-contract check, on forged documents.

    Each failure below is one end of the same obligation. The master and the
    copy have to move together, and which of them was edited is not knowable
    from the files - so the check reports the disagreement and names both ends
    rather than guessing at a culprit.
    """

    MIRROR = "skills/example/SKILL.md"

    def report(
        self,
        master: str = FORGED_MASTER,
        mirror: str | None = FORGED_MIRROR,
        declaration: str = FORGED_DECLARATION,
    ):
        """The check's report over a forged store, one part of it replaced.

        `mirror=None` writes no mirror at all, which is how a renamed or deleted
        copy is forged.
        """
        files = {str(MASTER): master, str(MIRRORS): declaration}
        if mirror is not None:
            files[self.MIRROR] = mirror
        with TemporaryDirectory() as directory:
            root = Path(directory)
            forge(root, files)
            return mirrored_contract_agrees(root)

    def test_it_is_quiet_when_both_ends_state_the_rule(self):
        """The baseline the failures below are measured against, and the
        wrapped-line case with it: the forged mirror breaks one statement across
        a line, as the real documents do."""
        report = self.report()
        self.assertEqual(report.problems, [])
        self.assertEqual(report.read, 2)

    def test_a_rule_reworded_in_the_master_is_named(self):
        """Breaks if the master's own prose stops being checked.

        This is the failure a list of sentences held only against the skills
        cannot have: the copies still say what the list expects, so everything
        passes while the master says something else. `CLAUDE.md` puts the
        obligation this way round because the skill is what an agent reads.
        """
        report = self.report(
            master=FORGED_MASTER.replace("every claim carries its evidence", "cite the node")
        )
        self.assertEqual(len(report.problems), 1)
        self.assertIn("no longer states it in its own prose", report.problems[0])
        self.assertIn("every claim carries its evidence", report.problems[0])

    def test_a_rule_dropped_from_the_mirror_is_named(self):
        """Breaks if the copy stops being checked, which is the drift that leaves
        a skill reading as authoritative while stating nothing."""
        report = self.report(mirror=FORGED_MIRROR.replace("interpretation is allowed", "so on"))
        self.assertEqual(len(report.problems), 1)
        self.assertIn("the mirror no longer states it", report.problems[0])
        self.assertIn("interpretation is allowed", report.problems[0])

    def test_a_statement_only_in_the_masters_mirror_list_does_not_count(self):
        """Breaks if blockquoted lines start being read as the master's prose.

        The master's mirror list paraphrases the rules it points at, closely
        enough that a declared statement can appear in a table cell. Read as
        prose, the check would satisfy itself out of its own declaration and
        report agreement over a rule the master no longer makes.
        """
        gutted = FORGED_MASTER.replace(
            "The first is that every claim carries its evidence",
            "The first is that the node is cited",
        )
        self.assertIn("| every claim carries its evidence |", gutted)
        report = self.report(master=gutted)
        self.assertEqual(len(report.problems), 1)
        self.assertIn("no longer states it in its own prose", report.problems[0])

    def test_a_declared_mirror_the_master_does_not_list_is_named(self):
        """Breaks if the two lists stop being reconciled. A copy the master does
        not name is a copy the next edit to the master will not reach, however
        faithfully it agrees today."""
        report = self.report(
            declaration=FORGED_DECLARATION + "docs/elsewhere.md :: interpretation is allowed\n"
        )
        self.assertEqual(len(report.problems), 2)
        self.assertIn("the master's mirror list does not name it", report.problems[0])
        self.assertIn("no such file", report.problems[1])

    def test_a_listed_mirror_with_nothing_declared_is_named(self):
        """Breaks if a row can be added to the master's list without anything
        being checked for it. The list would then look more enforced the longer
        it grew, and be less."""
        report = self.report(
            master=FORGED_MASTER.replace(
                "| `skills/example/SKILL.md` — its rules | every claim carries its evidence |",
                "| `skills/example/SKILL.md` — its rules | every claim carries its evidence |"
                "\n> | `skills/second/SKILL.md` — its rules | the same rule |",
            )
        )
        self.assertEqual(len(report.problems), 1)
        self.assertIn("declares no statement for it", report.problems[0])
        self.assertIn("skills/second/SKILL.md", report.problems[0])

    def test_a_mirror_that_is_gone_is_named_once(self):
        """Breaks if a renamed or deleted mirror reads as a missing statement
        instead of a missing file. Two statements were declared for it, and
        reporting each one separately buries the fact that the file is gone."""
        report = self.report(mirror=None)
        self.assertEqual(len(report.problems), 2)
        for problem in report.problems:
            self.assertIn("no such file", problem)


class UndeclaredMirrorTest(unittest.TestCase):
    """The firing direction for the completeness check, on forged documents."""

    SHARED = "the rule: every claim carries its evidence"
    BOTH = "every claim carries its evidence, and interpretation is allowed"

    def report(self, extra: dict[str, str], declaration: str = FORGED_DECLARATION):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            forge(
                root,
                {
                    str(MASTER): FORGED_MASTER,
                    str(MIRRORS): declaration,
                    "skills/example/SKILL.md": FORGED_MIRROR,
                }
                | extra,
            )
            return mirrors_are_declared(root)

    def test_one_shared_phrase_is_vocabulary_rather_than_a_copy(self):
        """The baseline, and the false positive that would switch this check off.

        Documents here share the contract's vocabulary without restating it - a
        command name, a phrase in passing. Firing on one phrase would report
        two of the real guides as undeclared mirrors on the first run.
        """
        report = self.report({"docs/other.md": f"# Other\n\n{self.SHARED}\n"})
        self.assertEqual(report.problems, [])
        self.assertGreaterEqual(report.read, 1)

    def test_a_document_that_restates_two_rules_is_named(self):
        """Breaks if the completeness direction stops being checked. A fourth
        file carrying the contract, with the master's list unaware of it, is the
        stale-list defect pointing outward."""
        report = self.report({"docs/other.md": f"# Other\n\n{self.BOTH}\n"})
        self.assertEqual(len(report.problems), 1)
        self.assertIn("docs/other.md", report.problems[0])
        self.assertIn("every claim carries its evidence", report.problems[0])
        self.assertIn("interpretation is allowed", report.problems[0])

    def test_a_declared_mirror_carrying_the_rules_is_not_reported(self):
        """Breaks if declared mirrors start being reported as undeclared ones.
        Every real mirror carries several statements by design, so this check
        would fail on all of them and be removed rather than fixed."""
        report = self.report({})
        self.assertEqual(report.problems, [])

    def test_an_empty_declaration_is_reported_rather_than_passed_over(self):
        """Breaks if the check can go quiet by having nothing to look for.

        The statements are what recognises a copy. With none declared, every
        document reads as carrying nothing - and unlike the other checks here
        the document count stays high, so the runner's read-nothing refusal
        would not catch it.
        """
        report = self.report(
            {"docs/other.md": f"# Other\n\n{self.BOTH}\n"}, declaration="# empty\n"
        )
        self.assertEqual(len(report.problems), 1)
        self.assertIn("declares no statements", report.problems[0])


class RetiredInstructionTest(unittest.TestCase):
    """The firing direction for the retired-instruction check, and its limit.

    The quiet direction is the one that decides whether this check survives: a
    document that retires an instruction has to name it, so prose about it is
    legitimate and common.
    """

    def report(self, document: str, declaration: str = FORGED_RETIRED):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            forge(root, {"README.md": document, str(RETIRED): declaration})
            return retired_instructions_stay_retired(root)

    def test_a_retired_instruction_in_a_command_block_is_named(self):
        """Breaks if a retired instruction can come back in a block a reader
        copies - the `graphify .` incident, where one document kept the
        instruction the skill had already explained away."""
        report = self.report("# Top\n\nBuild it:\n\n```bash\nold-tool .\n```\n")
        self.assertEqual(len(report.problems), 1)
        self.assertIn("README.md:6", report.problems[0])
        self.assertIn("run it from inside each directory instead", report.problems[0])

    def test_prose_about_a_retired_instruction_is_left_alone(self):
        """Breaks if the check starts reading prose, which is how it would be
        turned off: the documents that retire an instruction are the documents
        that name it, and so is the list itself."""
        report = self.report(
            "# Top\n\nThe tool's own README will tell you to run `old-tool .` at the top of\n"
            "the store. Do not: follow the sequence here instead.\n\n```bash\ncd repo\n```\n"
        )
        self.assertEqual(report.problems, [])
        self.assertEqual(report.read, 1)

    def test_a_longer_command_that_merely_starts_the_same_is_left_alone(self):
        """Breaks if the match stops being bounded. `old-tool ...` is a different
        command, and a substring match would report the route that replaced the
        retired one as though it were the retired one."""
        report = self.report("# Top\n\n```bash\nold-tool ... --deep\n```\n")
        self.assertEqual(report.problems, [])
        self.assertEqual(report.read, 1)

    def test_the_instruction_is_found_after_other_commands_on_the_line(self):
        """Breaks if only the start of a line is read. A block chains commands
        with `&&` and `|`, and the retired instruction is as retired in the
        middle of one as at the start."""
        report = self.report("# Top\n\n```bash\ncd store && old-tool . --deep\n```\n")
        self.assertEqual(len(report.problems), 1)
        self.assertIn("cd store && old-tool . --deep", report.problems[0])


class InstructionBoundaryTest(unittest.TestCase):
    """The boundary rule, written out by hand rather than derived from the code."""

    def test_it_matches_the_command_and_not_a_longer_one(self):
        """Breaks if `graphify .` starts matching `graphify ...`, or stops
        matching the command it names. The first reports a good command as
        retired; the second is the check quietly doing nothing."""
        self.assertTrue(instructed("graphify .", "graphify ."))
        self.assertTrue(instructed("graphify . --deep", "graphify ."))
        self.assertTrue(instructed("cd store && graphify .", "graphify ."))
        self.assertFalse(instructed("graphify ...", "graphify ."))
        self.assertFalse(instructed("mygraphify .", "graphify ."))
        self.assertFalse(instructed("graphify merge-graphs", "graphify ."))


if __name__ == "__main__":
    unittest.main()
