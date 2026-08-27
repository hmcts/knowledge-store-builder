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


def shipped_documentation() -> list[Path]:
    """Every file that tells a reader to run something, plans excluded."""
    skills = sorted(ROOT.joinpath("skills").rglob("SKILL.md"))
    docs = [
        p
        for p in sorted(ROOT.joinpath("docs").rglob("*.md"))
        if "superpowers" not in p.relative_to(ROOT).parts
    ]
    return skills + docs


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
