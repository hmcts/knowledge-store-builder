"""What the shipped skills tell people to run must exist in the library shipping them.

The skills and the library install separately - the skills through the plugin cache, the
library through pip - so on a user's machine the two versions can differ, and the symptom
is a stage the instructions document reported as `unknown stage`. A reader diagnoses that
on their own machine by listing the stages their install has (`knowledgestore` with no
stage). What this file guarantees upstream is that any single release is internally
consistent, so a user whose plugin and library came from one release never meets the
failure at all.

This is the direction that can actually be checked here. Drift is a property of two
installs, which a test in one repository cannot see; internal consistency of one release
is a property of this commit, which it can.

No version number is compared here, because none is written down any more. Two were: the
plugin manifest's `version` and the library floor the build skill declared. Both were
typed by hand for the release they shipped in and derived from nothing - the library
version is the git tag (hatch-vcs) and no file in the tree names it - and between them
they produced four releases of silent drift, a red `main` after a tag, and two blocked
release publishes. Each block was correct and each fix was correct, which is what makes
the numbers the defect rather than the people retyping them.

Both were proxies, and both proxied for something readable directly:

- the manifest's version stood for which copy of the skills is installed, which cannot be
  derived at all - the plugin installs from `main`, so any number in the manifest is a
  claim about a release the files may not have come from;
- the skill's floor stood for whether the installed library has the stages the skill runs,
  which the library answers itself, by name, in one command.

So the build skill now tells a reader to list the stages their install has and to stop on
a missing one, and this file is the gate behind that instruction: every
`knowledgestore <stage>` the shipped documentation names is asserted to be a stage of this
release, so the list a reader compares against is the list this release actually ships.

Historical plans under `docs/superpowers/plans/` are excluded deliberately: they record
what was planned at a date, not what to run today, and one of them still names a stage
that was renamed afterwards. Freezing a record of a decision is the point of it.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from knowledgestore import cli

ROOT = Path(__file__).resolve().parent.parent
# A CLI invocation, not a Python import. `from knowledgestore import graph_stream`
# matched the earlier pattern and was read as the stage `import`, so documenting a
# public helper failed this test - the instrument answering a neighbouring
# question, which is the failure mode this file exists to catch elsewhere.
INVOCATION = re.compile(r"(?<!from )\bknowledgestore\s+([a-z][a-z0-9-]*)")

MANIFEST = ROOT / ".claude-plugin/plugin.json"
BUILD_SKILL = ROOT / "skills/knowledge-store-build/SKILL.md"

# The sentence a hand-maintained library floor was stated in. The pattern is kept, with
# nothing in the tree that satisfies it: what it asserts now is that no floor has been
# written back in, which is the only way this class of drift returns.
MINIMUM_DECLARATION = re.compile(r"assumes knowledge-store-builder (\d+\.\d+\.\d+) or newer")
# The invocation that lists the installed library's stages: `knowledgestore` on a line of
# its own, with or without a trailing comment. A line naming a stage must not match it -
# the skill is full of those, and reading one as the listing would make this vacuous.
STAGE_LISTING = re.compile(r"^knowledgestore[ \t]*(?:#.*)?$", re.MULTILINE)
# The instruction that turns the listing into a gate rather than a note. Whitespace
# rather than a literal space between the words: the skill is wrapped prose, so the
# sentence carries a newline wherever the wrap happens to fall, and a pattern spelt
# with spaces reads a reworded skill and a rewrapped one as the same defect.
STOP_ON_A_MISSING_STAGE = re.compile(r"stop\s+if\s+any\b[^.]*\babsent\b", re.IGNORECASE)
UPGRADE_COMMAND = "pip install --upgrade hmcts-knowledge-store-builder"


def capability_problem(skill: str) -> str | None:
    """The complaint if the build skill's setup cannot stop an older library, else None.

    Separate from the assertion so the sensitivity check can drive it with forged skills
    rather than trusting a clean result from the one real file.
    """
    floor = MINIMUM_DECLARATION.search(skill)
    if floor is not None:
        return (
            f"the build skill states a library floor again ({floor.group(1)}). A version "
            "typed by hand describes the release it was typed in and is wrong for every "
            "release after it; the stages the skill runs are the property it stands for, "
            "and the library reports those by name"
        )
    if STAGE_LISTING.search(skill) is None:
        return (
            "the build skill no longer tells a reader to list the stages their install "
            "has, so nothing in it can notice a library older than these instructions"
        )
    if STOP_ON_A_MISSING_STAGE.search(skill) is None:
        return (
            "the build skill lists the stages without telling a reader to stop when one "
            "it uses is absent. A check with no instruction on its failure reads as "
            "advisory, and continuing reaches `unknown stage` only after earlier stages "
            "have written committed artefacts"
        )
    if UPGRADE_COMMAND not in skill:
        return (
            f"the build skill does not name the fix ({UPGRADE_COMMAND}), so a reader who "
            "stops has nothing to do next"
        )
    return None


# The user-facing files that live at the repository root rather than under
# `docs/`. CHEATSHEET.md is the most command-dense file here and was scanned by
# nothing: its stage names were correct by luck rather than by a check, and
# `extract-ast` is named in these files and nowhere else - so without them the
# reverse check below would have reported a documented stage as undocumented.
ROOT_DOCUMENTS = ("README.md", "VISION.md", "CHEATSHEET.md")

# The escape hatch for the reverse check, and the reason it is a committed file
# rather than a constant: adding a stage to it is a claim that no operator needs
# to know the stage exists, and a claim like that belongs where it can be read
# and argued with.
INTERNAL_STAGES = Path("docs/internal-stages.txt")


def shipped_documentation() -> list[Path]:
    """Every file that tells a reader to run something, plans excluded."""
    skills = sorted(ROOT.joinpath("skills").rglob("SKILL.md"))
    docs = [
        p
        for p in sorted(ROOT.joinpath("docs").rglob("*.md"))
        if "superpowers" not in p.relative_to(ROOT).parts
    ]
    roots = [ROOT / name for name in ROOT_DOCUMENTS if (ROOT / name).is_file()]
    return skills + docs + roots


def declared_internal() -> set[str]:
    """Stage names a maintainer has declared no document needs to name."""
    path = ROOT / INTERNAL_STAGES
    if not path.is_file():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


class DocumentedStagesExist(unittest.TestCase):
    def test_the_scan_finds_anything_at_all(self):
        """A scan that silently matches nothing would pass the test below vacuously.

        This is the guard on the instrument rather than on the code: a later change to
        the regex, the glob or the directory layout would otherwise turn this whole file
        into a green check of an empty set.
        """
        files = shipped_documentation()
        self.assertGreater(len(files), 3, "no shipped documentation was found to scan")
        mentioned = {
            m.group(1) for f in files for m in INVOCATION.finditer(f.read_text(encoding="utf-8"))
        }
        self.assertGreater(len(mentioned), 10, f"only {len(mentioned)} invocations found")

    def test_the_scan_covers_the_build_skill(self):
        """The build skill tells a reader to compare their install's stage list against
        its own commands, so those commands have to be stages of this release for the
        comparison to mean anything. The scan below is what holds that, and the build
        skill is the file it most has to reach.
        """
        scanned = shipped_documentation()
        self.assertIn(BUILD_SKILL, scanned, "the build skill is not among the scanned files")
        found = {m.group(1) for m in INVOCATION.finditer(BUILD_SKILL.read_text(encoding="utf-8"))}
        self.assertGreater(
            len(found), 20, f"only {len(found)} stage invocations found in the build skill"
        )

    def test_every_documented_stage_is_a_real_stage(self):
        for path in shipped_documentation():
            text = path.read_text(encoding="utf-8")
            for match in INVOCATION.finditer(text):
                stage = match.group(1)
                with self.subTest(file=str(path.relative_to(ROOT)), stage=stage):
                    self.assertIn(
                        stage,
                        cli.STAGES,
                        f"{path.relative_to(ROOT)} documents `knowledgestore {stage}`, "
                        "which is not a stage in this release - a reader following it "
                        "gets `unknown stage`",
                    )


class EveryStageIsDocumented(unittest.TestCase):
    """The other direction, which nothing here asked for.

    Every gate in this repository asks "does this documented thing exist?" and
    the answer is always yes, because a stage that was renamed shows up
    immediately. None asked "is this existing thing documented?", so a stage no
    document mentions was structurally invisible - and two were. `convert` turns
    Office documents into something extraction can read, without which they
    contribute a filename; `check-corpus` reports agent instructions the corpus
    carries. Neither was internal. Nobody had decided they should be
    undocumented; nothing had ever asked.
    """

    def test_every_stage_is_named_by_a_shipped_document(self):
        named = {
            m.group(1)
            for path in shipped_documentation()
            for m in INVOCATION.finditer(path.read_text(encoding="utf-8"))
        }
        internal = declared_internal()
        for stage in sorted(cli.STAGES):
            with self.subTest(stage=stage):
                self.assertTrue(
                    stage in named or stage in internal,
                    f"`knowledgestore {stage}` is a stage of this release and no shipped "
                    f"document names it. Document it, or add it to {INTERNAL_STAGES} to "
                    "say that no operator needs to know it exists",
                )

    def test_the_reverse_scan_reads_the_stages_and_the_documents(self):
        """The vacuity guard. An empty stage table or an empty scan passes above.

        Both halves, because either one going to zero makes the check green over
        nothing, and the failure looks identical from the outside.
        """
        self.assertGreater(len(cli.STAGES), 20, "the stage table is suspiciously small")
        named = {
            m.group(1)
            for path in shipped_documentation()
            for m in INVOCATION.finditer(path.read_text(encoding="utf-8"))
        }
        self.assertGreater(
            len(named & set(cli.STAGES)), 20, "the scan found almost no documented stages"
        )

    def test_the_internal_list_is_read_and_is_not_a_dumping_ground(self):
        """An escape hatch nobody can see the size of stops being an escape hatch."""
        internal = declared_internal()
        self.assertTrue(
            (ROOT / INTERNAL_STAGES).is_file(),
            f"{INTERNAL_STAGES} is missing, so the check above has no escape hatch and "
            "the next internal stage will be documented under protest or the gate "
            "deleted",
        )
        for stage in internal:
            with self.subTest(stage=stage):
                self.assertIn(
                    stage, cli.STAGES, f"{INTERNAL_STAGES} names {stage}, which is not a stage"
                )
        self.assertLess(
            len(internal),
            len(cli.STAGES) // 2,
            "more than half the stages are declared internal; the exception has become "
            "the rule and this gate is no longer saying anything",
        )


class TheScanCanStillTell(unittest.TestCase):
    """Break the scan's inputs, confirm it notices, restore - in this run.

    The check above can only pass or fail; it cannot report that it has stopped
    discriminating. It nearly did: the pattern was narrowed to stop matching Python
    imports, and a narrowing is exactly the kind of improvement that quietly turns
    a check vacuous. So the pattern is exercised against text it must flag and text
    it must ignore, rather than trusted because the corpus happens to be clean.
    """

    def test_it_flags_an_invocation_of_a_stage_that_does_not_exist(self):
        found = {m.group(1) for m in INVOCATION.finditer("run `knowledgestore reticulate` now")}
        self.assertEqual(found, {"reticulate"})
        self.assertNotIn("reticulate", cli.STAGES, "the fixture must name a non-stage")

    def test_it_still_finds_a_real_invocation(self):
        found = {m.group(1) for m in INVOCATION.finditer("then `knowledgestore explorer`")}
        self.assertEqual(found, {"explorer"})

    def test_it_ignores_a_python_import(self):
        """The narrowing that prompted this class. Both import forms must be silent."""
        for text in (
            "from knowledgestore import graph_stream",
            "import knowledgestore",
        ):
            with self.subTest(text=text):
                self.assertEqual([m.group(1) for m in INVOCATION.finditer(text)], [])


class ThePluginManifestNamesNoVersion(unittest.TestCase):
    """A version in the plugin manifest can only be a claim that is not checkable.

    The plugin installs from this repository's `main` branch, which `docs/asking-questions.md`
    states and the refresh commands there rely on, so the files a user holds are whatever
    `main` was when they last ran the install - not a release. A number in the manifest
    named a release those files may never have come from, and it sat four releases behind
    while looking authoritative.

    Claude Code does not need it. Empirically, on 2.1.247:

        claude plugin validate <plugin dir>            # passes; absence is a warning
        claude plugin install knowledge-store@...      # succeeds, and the plugin enables
        claude plugin list                             # reports `Version: unknown`

    `claude plugin validate --strict` turns that warning into a failure. Nothing here runs
    it; if that changes, the decision to carry no version is what to revisit, rather than
    the number to reinstate. This test is what fails if one is added back.
    """

    def test_the_manifest_declares_no_version(self):
        """Breaks when a version is written back into the manifest - the drift this
        removes, which cost four releases and two blocked publishes while every check
        that read the number agreed with it."""
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertNotIn(
            "version",
            manifest,
            f"{MANIFEST.relative_to(ROOT)} declares a version again. The plugin installs "
            "from `main`, so no number here can be true of the files a user holds, and "
            "nothing derives it - it is maintained by hand or it is wrong",
        )

    def test_the_manifest_still_identifies_the_plugin(self):
        """The control: removing a field must not have removed the manifest's contents.

        Without this, a manifest emptied by accident would satisfy the assertion above.
        """
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("name"), "knowledge-store")
        self.assertIn("description", manifest)


class TheBuildSkillChecksTheStagesItUses(unittest.TestCase):
    """The skill is the newer artefact when the two drift, so it states what it needs.

    It states it as the stages it runs rather than as a version, because the stages are
    what actually fails: an older library reports `unknown stage`. `DocumentedStagesExist`
    above is the other half of the instruction - it holds every stage the skill names to
    be a stage of this release, so a reader comparing their install's list against the
    skill's commands is comparing against something true, with no number in either place.
    """

    def test_the_build_skill_tells_a_reader_to_check_for_them(self):
        """Breaks if the listing, the instruction to stop, or the fix is dropped, and if
        a hand-maintained floor is written back in."""
        problem = capability_problem(BUILD_SKILL.read_text(encoding="utf-8"))
        self.assertIsNone(problem, problem)


class TheCapabilityCheckCanStillTell(unittest.TestCase):
    """Break what the check protects, confirm it notices - in this run.

    The assertion above can only pass or fail; it cannot report that it has stopped
    reading the skill. Its predecessor read one sentence for a number and could not
    fire in CI at all, so this is driven against forged skills, each missing exactly one
    of the properties claimed, rather than trusted because the real file is in shape.
    """

    def forged(
        self,
        *,
        listing: bool = True,
        stop: bool = True,
        upgrade: bool = True,
        floor: str | None = None,
    ) -> str:
        """A setup section carrying the chosen subset of the properties checked."""
        parts = ["## Setup\n"]
        if floor is not None:
            parts.append(f"**This skill assumes knowledge-store-builder {floor} or newer.**\n")
        if listing:
            parts.append("```bash\nknowledgestore   # every stage this install has\n```\n")
        if stop:
            parts.append(
                "**Read that list against the commands below, and stop if any of "
                "them is absent.**\n"
            )
        if upgrade:
            parts.append(f"The fix is `{UPGRADE_COMMAND}`.\n")
        return "\n".join(parts)

    def test_the_shape_it_accepts(self):
        """The control: without this, every assertion below could be reporting a problem
        with the fixture rather than with what it forged."""
        self.assertIsNone(capability_problem(self.forged()), capability_problem(self.forged()))

    def test_it_reports_a_hand_typed_floor_written_back_in(self):
        """The bite, in the wording that shipped. A working stage check
        beside it is the realistic regression: someone adds the number back as extra
        reassurance, and it is stale from the next release onwards."""
        problem = capability_problem(self.forged(floor="0.15.2"))
        self.assertIsNotNone(problem, "a hand-maintained floor read as compliance")
        assert problem is not None
        self.assertIn("0.15.2", problem)

    def test_it_reports_a_skill_that_stops_listing_the_stages(self):
        problem = capability_problem(self.forged(listing=False))
        self.assertIsNotNone(problem, "a skill with no stage listing read as checked")
        assert problem is not None
        self.assertIn("list the stages", problem)

    def test_it_reports_a_listing_with_no_instruction_to_stop(self):
        """A listing a reader is not told to act on is a note, and this skill's failure
        mode is continuing: the artefacts earlier stages commit are the cost."""
        problem = capability_problem(self.forged(stop=False))
        self.assertIsNotNone(problem, "a listing with no stop instruction read as a gate")
        assert problem is not None
        self.assertIn("stop", problem)

    def test_it_reports_a_skill_that_does_not_name_the_fix(self):
        problem = capability_problem(self.forged(upgrade=False))
        self.assertIsNotNone(problem, "a check with no remedy read as complete")
        assert problem is not None
        self.assertIn("upgrade", problem)

    def test_a_stage_invocation_does_not_count_as_the_listing(self):
        """The vacuity this shape invites. The skill names more than twenty
        `knowledgestore <stage>` commands, so a pattern loose enough to read one of those
        as the listing would report every later version of this skill as checked.
        """
        stages_only = "## Setup\n\n```bash\nknowledgestore discover\nknowledgestore sync\n```\n"
        self.assertIsNone(STAGE_LISTING.search(stages_only))
        problem = capability_problem(stages_only + f"stop if any is absent. `{UPGRADE_COMMAND}`\n")
        self.assertIsNotNone(problem, "a stage invocation read as the stage listing")
        assert problem is not None
        self.assertIn("list the stages", problem)

    def test_the_stop_instruction_survives_a_line_wrap(self):
        """Where the wrap falls is not a property of the instruction. The pattern was
        written with literal spaces first and failed against the shipped skill, whose
        sentence breaks between `if` and `any` - a check reporting a formatting choice
        as a missing gate."""
        wrapped = "and stop if\nany of them is absent."
        self.assertIsNotNone(STOP_ON_A_MISSING_STAGE.search(wrapped))
        self.assertIsNone(capability_problem(self.forged(stop=False) + wrapped))

    def test_the_listing_is_found_with_and_without_a_trailing_comment(self):
        """The two forms the command is written in, so the pattern is not pinned to the
        comment that happens to be beside it today."""
        for line in ("knowledgestore", "knowledgestore   # every stage this install has"):
            with self.subTest(line=line):
                self.assertIsNotNone(STAGE_LISTING.search(f"```bash\n{line}\n```\n"))


if __name__ == "__main__":
    unittest.main()
# --- appended to tests/test_documented_stages.py ---

# A documented command line, read out of a code fence or an inline code span rather than
# out of prose. The stage name alone is not the claim being checked here: `knowledgestore
# topics merge` names a real stage and was still refused, so a scan that captures only the
# stage answers a neighbouring question and reports clean.
CODE_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
INLINE_CODE = re.compile(r"`([^`\n]+)`")
# `(?<!from )` for the same reason as INVOCATION above: `from knowledgestore import
# graph_stream` is a code span in these documents and reads as the stage `import`.
COMMAND_LINE = re.compile(r"(?<!from )\bknowledgestore\s+([a-z][a-z0-9-]*)([^\n]*)")
# Where a shell line stops being arguments to this command.
TAIL = re.compile(r"\s(?:#|\||&&|\|\||;)|\s*$")


def documented_invocations(text: str) -> list[tuple[str, list[str], str]]:
    """Every `knowledgestore <stage> <arguments>` printed as a command, with its line.

    Prose is excluded deliberately: "`knowledgestore status` never returns non-zero" is a
    sentence about a stage, not a line anyone runs, and reading the following words as
    arguments would make this fire on documentation that is correct.
    """
    regions = [m.group(1) for m in CODE_FENCE.finditer(text)]
    regions += [m.group(1) for m in INLINE_CODE.finditer(text)]
    found = []
    for region in regions:
        for match in COMMAND_LINE.finditer(region):
            stage, tail = match.group(1), match.group(2)
            cut = TAIL.search(tail)
            arguments = tail[: cut.start()].split() if cut else tail.split()
            found.append((stage, arguments, match.group(0).strip()))
    return found


class DocumentedInvocationsAreAccepted(unittest.TestCase):
    """Every command line the documentation prints must survive the argument guard.

    `test_every_documented_stage_is_a_real_stage` above checks the stage exists. That is a
    different property, and the gap between them shipped: the guard in `cli` was widened
    from refusing an unrecognised `--help` to refusing every unrecognised argument, three
    stages that dispatch on a subcommand were not in `SELF_PARSING`, and six lines in the
    shipped documentation stopped working. Every one of them still named a real stage, so
    nothing here failed.

    The guard is asked directly rather than restated. A copy of the condition would pass
    this file while the real one refused the user.
    """

    def test_the_scan_finds_invocations_that_carry_arguments(self):
        """Without this the class below is vacuous in the one way that matters.

        An invocation with no arguments can never be refused, so a scan that captured only
        bare `knowledgestore <stage>` lines would report clean over a guard refusing
        everything else.
        """
        with_arguments = [
            (stage, args)
            for path in shipped_documentation()
            for stage, args, _line in documented_invocations(path.read_text(encoding="utf-8"))
            if args
        ]
        self.assertGreater(
            len(with_arguments),
            5,
            f"only {len(with_arguments)} documented invocations carry arguments; the scan "
            "is no longer reading the lines this check exists for",
        )
        stages = {stage for stage, _ in with_arguments}
        for expected in ("topics", "deepdive"):
            self.assertIn(
                expected,
                stages,
                f"`{expected}` takes a subcommand and the scan found no documented line "
                "passing it one",
            )

    def test_no_documented_invocation_is_refused(self):
        for path in shipped_documentation():
            text = path.read_text(encoding="utf-8")
            for stage, arguments, line in documented_invocations(text):
                if stage not in cli.STAGES:
                    continue  # the check above owns that failure
                with self.subTest(file=str(path.relative_to(ROOT)), line=line):
                    self.assertFalse(
                        cli.refuses_arguments(stage, arguments),
                        f"{path.relative_to(ROOT)} tells a reader to run `{line}`, and the "
                        f"argument guard refuses it: `{stage}` is not in SELF_PARSING, so "
                        f"{', '.join(arguments)} is rejected and the stage never runs",
                    )


class TheAcceptanceScanCanStillTell(unittest.TestCase):
    """Drive the scan with text it must flag and text it must ignore, in this run."""

    def test_it_flags_a_documented_line_the_guard_would_refuse(self):
        forged = "Run this:\n\n```bash\nknowledgestore sync --prune\n```\n"
        found = documented_invocations(forged)
        self.assertEqual(found[0][:2], ("sync", ["--prune"]))
        self.assertTrue(
            cli.refuses_arguments(*found[0][:2]),
            "the fixture must be an invocation the guard actually refuses",
        )

    def test_it_reads_arguments_off_a_real_documented_line(self):
        found = documented_invocations("```bash\nknowledgestore deepdive extract <repo>\n```")
        self.assertEqual(found[0][:2], ("deepdive", ["extract", "<repo>"]))

    def test_it_stops_at_a_trailing_comment(self):
        found = documented_invocations("```\nknowledgestore topics merge  # renders briefs\n```")
        self.assertEqual(found[0][:2], ("topics", ["merge"]))

    def test_it_ignores_a_python_import_in_a_code_span(self):
        """`from knowledgestore import graph_stream` is not a command line."""
        self.assertEqual(documented_invocations("`from knowledgestore import graph_stream`"), [])

    def test_it_ignores_prose(self):
        """Words after a stage in a sentence are not arguments to it."""
        self.assertEqual(documented_invocations("knowledgestore status never returns non-zero"), [])

    def test_it_reads_an_inline_span_as_a_command_without_swallowing_the_sentence(self):
        found = documented_invocations("see `knowledgestore status` for drift, which is normal")
        self.assertEqual(found[0][:2], ("status", []))

    def test_the_guard_still_accepts_help_for_a_stage_with_no_arguments(self):
        """The over-correction guard: widening SELF_PARSING must not silence `--help`."""
        self.assertFalse(cli.refuses_arguments("sync", ["--help"]))
        self.assertFalse(cli.refuses_arguments("sync", []))
        self.assertTrue(cli.refuses_arguments("sync", ["--prune"]))
