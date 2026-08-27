"""What the shipped skills tell people to run must exist in the library shipping them.

The skills and the library install separately - the skills through the plugin cache, the
library through pip - so on a user's machine the two versions can differ, and the symptom
is a stage the instructions document reported as `unknown stage`. That is diagnosable now
(`knowledgestore --version`), but the cheaper win is upstream: guarantee that any single
release is internally consistent, so a user whose plugin and library are the same version
never meets the failure at all.

This is the direction that can actually be checked here. Drift is a property of two
installs, which a test in one repository cannot see; internal consistency of one release
is a property of this commit, which it can.

Two numbers in this repository are written by hand and derived from nothing: the plugin
manifest's `version` and the library floor the build skill declares. What this file
checks of them is that they agree with *each other*, which is a property of this commit
and needs no tag - and which catches a drift neither of the checks it replaces could
see, because two equally stale numbers satisfied both.

Whether they agree with the newest release is a property of a *tag*, not of the tree, so
it is not checked here. Comparing against `git describe` made this suite's result change
when a tag was created rather than when a commit was made: the same `main` passed before
a release and failed after it, with nothing to attribute the failure to. That comparison
now lives in `scripts/check_release_versions.py`, which the release job runs before it
publishes, and `tests/test_release_version_check.py` covers it.

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
# The sentence the build skill states its library floor in.
MINIMUM_DECLARATION = re.compile(r"assumes knowledge-store-builder (\d+\.\d+\.\d+) or newer")


def declared_minimum(skill: str) -> str | None:
    """The library version the build skill's prose says it needs, if it says one."""
    found = MINIMUM_DECLARATION.search(skill)
    return found.group(1) if found else None


def disagreement(manifest_version: str | None, declared: str | None) -> str | None:
    """The complaint if the two hand-maintained numbers differ, else None.

    Separate from the assertion so the sensitivity check can drive it with forged
    values rather than trusting a clean result from the one real pair.

    Compared as text, not as version triples: both are written by hand in the
    form N.N.N, and a release that publishes `0.15.0` beside `0.15.00` has the
    drift this exists to catch even though the numbers order equally.
    """
    if manifest_version is None:
        return f"{MANIFEST.relative_to(ROOT)} declares no version"
    if declared is None:
        return f"{BUILD_SKILL.relative_to(ROOT)} declares no library minimum"
    if manifest_version != declared:
        return (
            f"{MANIFEST.relative_to(ROOT)} says {manifest_version} and the build skill "
            f"assumes {declared} or newer. Both are bumped by hand for the same release, "
            "so a release cut from this commit publishes a plugin and a floor that "
            "describe different libraries"
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


class ThePluginIsIdentifiable(unittest.TestCase):
    """A plugin with no version cannot be told apart from any other copy of itself.

    Which is what made version drift hard to even discuss: asked what they had installed,
    a user could answer for the library (with the flag added alongside this) but had
    nothing to read for the skills.
    """

    def test_the_plugin_manifest_carries_a_version(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertIn("version", manifest, "plugin.json carries no version")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")


class TheBuildSkillDeclaresTheLibraryItNeeds(unittest.TestCase):
    """The skill is the newer artefact when the two drift, so it states what it needs.

    What this class checks is that the sentence stating it is there and readable.
    Whether the version it names is current is checked twice, in the two places the
    question can actually be answered:

    - against the plugin manifest, by `TheTwoHandMaintainedNumbersAgree` below - a
      property of this commit, so a test can hold it;
    - against the tag being cut, by `scripts/check_release_versions.py` at release
      time - a property of a tag, which is not knowable from a tree. A comparison
      against the installed version was tried and could not fire: `tests.yml` never
      runs on a tag, so that version is always a `devN` derived from the *previous*
      release, and `0.11.6 <= 0.14.1.devN` stayed true while the skill documented ten
      stages that release does not have. A comparison against `git describe` fired
      correctly but made this suite's result change on a tag rather than on a commit.
    """

    def setUp(self):
        self.skill = BUILD_SKILL.read_text(encoding="utf-8")

    def test_the_build_skill_declares_a_library_minimum(self):
        """Breaks if the declaration is deleted or reworded past the pattern.

        The guard on the instrument: the agreement check below and the release
        script both read that one sentence, and with no sentence to read they would
        report a clean result about nothing.
        """
        self.assertIsNotNone(
            declared_minimum(self.skill),
            f"{BUILD_SKILL.relative_to(ROOT)} declares no library minimum a reader can "
            "check their install against",
        )


class TheTwoHandMaintainedNumbersAgree(unittest.TestCase):
    """The plugin manifest's version and the build skill's floor are bumped together.

    Both are written by hand for the same release and nothing derives one from the
    other, so they drift independently. This is the half of that property a tree can
    hold, and it catches a defect the tag comparisons it replaces could not: two
    equally stale numbers were behind nothing and passed both of them.

    A release cut from a commit where they disagree publishes a plugin naming one
    library beside a skill requiring another, which is the mismatch the manifest
    version exists to make diagnosable in the first place.
    """

    def test_the_manifest_version_and_the_declared_floor_are_the_same(self):
        """Breaks when a release-preparing change bumps one of the two and not the
        other - the drift no comparison against a tag can see, because both numbers
        can be equally stale or equally ahead and satisfy it."""
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        problem = disagreement(
            manifest.get("version"), declared_minimum(BUILD_SKILL.read_text(encoding="utf-8"))
        )
        self.assertIsNone(problem, problem)


class TheAgreementCheckCanStillTell(unittest.TestCase):
    """Break what the check protects, confirm it notices - in this run.

    The check above can only pass or fail; it cannot report that it has stopped
    comparing. Its predecessor could not fire in CI at all and looked identical from
    the outside, so the comparison is driven against forged values rather than
    trusted because the shipped pair happens to agree.
    """

    def forged(self, minimum: str) -> str:
        """The declaration sentence as the skill writes it, at a chosen version."""
        text = f"**This skill assumes knowledge-store-builder {minimum} or newer.** Check first.\n"
        self.assertEqual(declared_minimum(text), minimum, "the fixture must be readable")
        return text

    def test_it_reports_a_manifest_that_disagrees_with_the_skill(self):
        """The bite. 0.11.6 beside a 0.15.0 floor is the real historical manifest."""
        problem = disagreement("0.11.6", declared_minimum(self.forged("0.15.0")))
        self.assertIsNotNone(problem, "a manifest four releases from the floor read as agreeing")
        assert problem is not None
        self.assertIn("0.11.6", problem)
        self.assertIn("0.15.0", problem)

    def test_it_reports_a_skill_left_behind_a_bumped_manifest(self):
        """The other direction, which a check reading only one file would miss."""
        problem = disagreement("0.16.0", declared_minimum(self.forged("0.15.0")))
        self.assertIsNotNone(problem, "a floor left behind the manifest read as agreeing")

    def test_it_accepts_the_two_saying_the_same_thing(self):
        """The steady state. A check that cannot pass gets switched off."""
        self.assertIsNone(disagreement("0.15.0", declared_minimum(self.forged("0.15.0"))))

    def test_it_reports_a_skill_that_declares_nothing(self):
        """A reworded sentence must read as a problem, not as compliance: with no
        number to read, `None == None` would otherwise be agreement."""
        problem = disagreement("0.15.0", declared_minimum("This skill needs a recent library.\n"))
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("no library minimum", problem)

    def test_it_reports_a_manifest_that_declares_nothing(self):
        """The same absence on the other side, and for the same reason."""
        problem = disagreement(None, "0.15.0")
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("no version", problem)

    def test_it_reports_two_absences_rather_than_calling_them_equal(self):
        """The tautology this shape invites: both files losing their number is the
        worst state, and comparing None with None is the way it reads as the best."""
        self.assertIsNotNone(disagreement(None, None))


if __name__ == "__main__":
    unittest.main()
