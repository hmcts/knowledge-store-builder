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

The version the build skill declares as its floor is the other half, and it is checked
against the newest *release* rather than against this commit's own version -
`TheBuildSkillDeclaresTheLibraryItNeeds` says why the second is not knowable before
the tag exists, and what that cost.

Historical plans under `docs/superpowers/plans/` are excluded deliberately: they record
what was planned at a date, not what to run today, and one of them still names a stage
that was renamed afterwards. Freezing a record of a decision is the point of it.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from knowledgestore import cli

ROOT = Path(__file__).resolve().parent.parent
# A CLI invocation, not a Python import. `from knowledgestore import graph_stream`
# matched the earlier pattern and was read as the stage `import`, so documenting a
# public helper failed this test - the instrument answering a neighbouring
# question, which is the failure mode this file exists to catch elsewhere.
INVOCATION = re.compile(r"(?<!from )\bknowledgestore\s+([a-z][a-z0-9-]*)")

BUILD_SKILL = ROOT / "skills/knowledge-store-build/SKILL.md"
# The sentence the build skill states its library floor in, and the shape of the
# release tags this project cuts. hatch-vcs derives the version from those tags,
# so the most recent one is the newest library `pip install` can reach.
MINIMUM_DECLARATION = re.compile(r"assumes knowledge-store-builder (\d+\.\d+\.\d+) or newer")
RELEASE_TAG = "v[0-9]*"


def triple(version: str) -> tuple[int, int, int]:
    """The comparable part of a version, tag prefix and dev suffix discarded."""
    parts = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version)
    if parts is None:
        raise AssertionError(f"unparseable version {version!r}")
    return (int(parts[1]), int(parts[2]), int(parts[3]))


def declared_minimum(skill: str) -> str | None:
    """The library version the build skill's prose says it needs, if it says one."""
    found = MINIMUM_DECLARATION.search(skill)
    return found.group(1) if found else None


def latest_release_tag(root: Path) -> str | None:
    """The most recent release tag reachable from HEAD, or None when there is none.

    None covers an sdist, a tarball and a shallow clone with no tags fetched -
    conditions of the checkout rather than defects in the skill, so the caller
    skips rather than failing. `git describe` is asked for reachable tags only,
    so a tag on some other branch does not count as shipped from here.
    """
    try:
        finished = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match", RELEASE_TAG],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # pragma: no cover - depends on the machine having git
        return None
    if finished.returncode != 0:
        return None
    return finished.stdout.strip() or None


def behind_the_release(skill: str, tag: str) -> str | None:
    """The complaint if the skill's declared minimum is older than `tag`, else None.

    Separate from the assertion so the sensitivity checks can drive it with a
    forged skill text and a fabricated tag, rather than trusting a clean result
    from the one real pair.
    """
    declared = declared_minimum(skill)
    if declared is None:
        return f"{BUILD_SKILL.relative_to(ROOT)} declares no library minimum"
    if triple(declared) < triple(tag):
        return (
            f"the build skill assumes {declared} or newer, but {tag} has shipped since. "
            "A reader who installs the version it names gets `unknown stage` for every "
            "stage added after it"
        )
    return None


def plugin_version_behind(manifest_version: str, tag: str) -> str | None:
    """The complaint if the plugin manifest's version is older than `tag`, else None.

    Separate from the assertion so the sensitivity check can drive it with a
    fabricated pair rather than trusting a clean result from the one real one.
    """
    if triple(manifest_version) < triple(tag):
        return (
            f"the plugin manifest says {manifest_version}, but {tag} has shipped since. "
            "The manifest version is how a user tells one copy of the skills from "
            "another, so a stale one makes a plugin/library mismatch undiagnosable"
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
        manifest = json.loads(
            ROOT.joinpath(".claude-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertIn("version", manifest, "plugin.json carries no version")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")

    def test_the_plugin_version_is_not_behind_the_latest_release(self):
        """Breaks if the manifest is left behind a release.

        It sat at 0.11.6 across four releases while the test above was green, because
        that test asks only whether a version exists and parses. The manifest is the
        one file in this repository that names a version - `CLAUDE.md` records that
        the version is the git tag and nothing else states it - so it is the one
        place a release has to be carried by hand, and the only thing that keeps it
        honest is a check comparing it to what shipped.

        Ahead is accepted, as it is for the build skill's floor: that is a release
        being prepared. Only behind is a defect.
        """
        tag = latest_release_tag(ROOT)
        if tag is None:
            raise unittest.SkipTest(
                "no release tag is reachable from HEAD (an sdist, a tarball or a "
                "shallow clone), so there is no shipped version to compare against"
            )
        manifest = json.loads(
            ROOT.joinpath(".claude-plugin/plugin.json").read_text(encoding="utf-8")
        )
        problem = plugin_version_behind(manifest["version"], tag)
        self.assertIsNone(problem, problem)

    def test_this_gate_notices_a_manifest_left_behind(self):
        """The sensitivity check, in the same run.

        Drives the predicate with a fabricated pair. If this ever passes silently,
        the assertion above is measuring that a string parses rather than that the
        manifest kept up.
        """
        self.assertIsNotNone(plugin_version_behind("0.11.6", "v0.15.0"))
        self.assertIsNone(plugin_version_behind("0.15.0", "v0.15.0"))
        self.assertIsNone(plugin_version_behind("0.16.0", "v0.15.0"))


class TheBuildSkillDeclaresTheLibraryItNeeds(unittest.TestCase):
    """The skill is the newer artefact when the two drift, so it states what it needs.

    Only one direction of that is checkable here, and it is not the obvious one.

    **Not checkable: the declaration is not ahead of what this commit ships.** The
    version is the git tag (hatch-vcs) and no file in the tree names it, so before
    the tag exists there is nothing to compare against - and `tests.yml` runs on
    `pull_request` and pushes to `main`, never on a tag, so the installed version
    a test can read in CI is always a `devN` derived from the *previous* release.
    Comparing the declaration against that made a release-preparing bump fail by
    construction, which is how the declaration came to name 0.11.6 while the skill
    documented ten stages that release does not have. `0.11.6 <= 0.14.1.devN` is
    true, so the comparison passed the whole time.

    **Checkable: the declaration is not behind the newest release.** A skill
    documenting current stages cannot be followed with an older library, and the
    most recent release tag is reachable in CI (`fetch-depth: 0`). Three states,
    all deliberate:

    - behind the latest tag - fails, the drift this exists to catch;
    - equal to it - passes, the steady state between releases;
    - ahead of it - passes, a release being prepared, as the bump to 0.15.0 was.

    The obligation that follows is that the release-preparing change bumps this
    sentence, because after the tag lands an unbumped declaration is behind.
    """

    def setUp(self):
        self.skill = BUILD_SKILL.read_text(encoding="utf-8")

    def test_the_build_skill_declares_a_library_minimum(self):
        """Breaks if the declaration is deleted or reworded past the pattern.

        The guard on the instrument: the check below reads that one sentence, and
        with no sentence to read it would report a clean result about nothing.
        """
        self.assertIsNotNone(
            declared_minimum(self.skill),
            f"{BUILD_SKILL.relative_to(ROOT)} declares no library minimum a reader can "
            "check their install against",
        )

    def test_the_declared_minimum_is_not_behind_the_latest_release(self):
        """Breaks when a release ships and the skill keeps naming an older library.

        Which is the state this file found: stages landed release after release and
        the declaration stayed at 0.11.6, so an operator who followed it literally
        installed a library without them.
        """
        tag = latest_release_tag(ROOT)
        if tag is None:
            self.skipTest(
                "no release tag is reachable from HEAD (an sdist, a tarball or a shallow "
                "clone), so there is no shipped version to compare the declaration against"
            )
        problem = behind_the_release(self.skill, tag)
        self.assertIsNone(problem, problem)


class TheMinimumCheckCanStillTell(unittest.TestCase):
    """Break what the check protects, confirm it notices, restore - in this run.

    The check above can only pass or fail; it cannot report that it has stopped
    comparing. Its predecessor could not fire in CI at all and looked exactly like
    this one from the outside, so the comparison is driven against forged text and
    fabricated tags rather than trusted because the shipped pair happens to agree.
    """

    def forged(self, minimum: str) -> str:
        """The declaration sentence as the skill writes it, at a chosen version."""
        text = f"**This skill assumes knowledge-store-builder {minimum} or newer.** Check first.\n"
        self.assertEqual(declared_minimum(text), minimum, "the fixture must be readable")
        return text

    def test_it_reports_a_minimum_behind_the_latest_release(self):
        """The bite. 0.11.6 against a 0.14.0 release is the real historical pair."""
        problem = behind_the_release(self.forged("0.11.6"), "v0.14.0")
        self.assertIsNotNone(problem, "a minimum two releases stale went unreported")
        assert problem is not None
        self.assertIn("0.11.6", problem)
        self.assertIn("v0.14.0", problem)

    def test_it_accepts_a_minimum_equal_to_the_latest_release(self):
        """The steady state between releases. Failing here would make every commit
        after a release red until someone invented a version to name."""
        self.assertIsNone(behind_the_release(self.forged("0.14.0"), "v0.14.0"))

    def test_it_accepts_a_minimum_ahead_of_the_latest_release(self):
        """A release being prepared - the case the previous comparison forbade, and
        the reason the declaration could never name the release it ships in."""
        self.assertIsNone(behind_the_release(self.forged("0.15.0"), "v0.14.0"))

    def test_it_reports_a_skill_that_declares_nothing(self):
        """A reworded sentence must read as a problem, not as compliance."""
        problem = behind_the_release("This skill needs a recent library.\n", "v0.14.0")
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("no library minimum", problem)

    def test_it_compares_minor_and_patch_numerically(self):
        """String comparison would read 0.9.0 as newer than 0.14.0, and would keep
        reading it that way for the whole 0.1x series."""
        self.assertIsNotNone(behind_the_release(self.forged("0.9.0"), "v0.14.0"))
        self.assertIsNone(behind_the_release(self.forged("0.14.10"), "v0.14.9"))


class TheLatestReleaseTagComesFromGit(unittest.TestCase):
    """The skip path, exercised rather than assumed.

    A skip nothing has ever triggered is a class that could stop running with no
    symptom, so both reasons for one are reproduced against real git rather than
    reasoned about.
    """

    def test_it_reads_the_most_recent_release_tag_of_this_repository(self):
        """The control for the two below: proves None means absent rather than that
        nothing looked. Breaks if the tag pattern stops matching what is cut here."""
        tag = latest_release_tag(ROOT)
        if tag is None:
            self.skipTest("no release tag is reachable from HEAD in this checkout")
        self.assertRegex(tag, r"^v\d+\.\d+\.\d+$")

    def test_it_reports_none_when_no_tag_is_reachable(self):
        """Breaks if an untagged checkout raised or returned a junk version instead.

        A shallow CI clone with no tags fetched lands here, and a test that fails
        because git history is absent gets deleted rather than fixed.
        """
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "untagged"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            (repository / "file.txt").write_text("content\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "file.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(repository)]
                + ["-c", "user.email=t@e", "-c", "user.name=t"]
                + ["commit", "-q", "-m", "one"],
                check=True,
            )
            tags = subprocess.run(
                ["git", "-C", str(repository), "tag", "--list"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(tags.stdout, "", "the fixture repository must carry no tags")
            self.assertIsNone(latest_release_tag(repository))

    def test_it_reports_none_outside_a_repository(self):
        """An sdist or an unpacked wheel has no git directory at all."""
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            probe = subprocess.run(
                ["git", "-C", str(outside), "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode == 0:
                self.skipTest(f"the temporary directory is inside a repository ({outside})")
            self.assertIsNone(latest_release_tag(outside))


if __name__ == "__main__":
    unittest.main()
