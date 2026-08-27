"""The release-time version check, and the wiring that makes it run.

Two hand-maintained numbers have to agree with the tag a release is cut at: the
plugin manifest's `version` and the build skill's declared library floor. Neither
is derived from anything - the library version is the git tag (hatch-vcs) and no
file in the tree names it - so the only thing that keeps them honest is a check.

That check used to live in the unit suite, comparing them against the newest
reachable tag. Its input changed when a *tag* was created rather than when a
commit was made, so the same `main` that passed before a release failed after it
with no commit to attribute the failure to. The comparison belongs at release
time, which is where the tag comes from, so it moved to a script the release job
runs before it publishes.

The breaks this file catches:

- the script stops reporting a number that is behind the tag being released, or
  reports one that is not - checked over behind, equal, ahead, unreadable and
  absent inputs, driving the real script as the release job drives it;
- the script's complaint stops naming which number is wrong and what it should
  say, which is the difference between an actionable release failure and reading
  code at the worst moment;
- the step disappears from `.github/workflows/build.yml`, moves after the
  publish, or loses the release condition that makes it run at all - a check
  that exists and never runs is indistinguishable from no check.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_release_versions.py"
WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"
# The path the workflow must name, spelt as the workflow spells it: relative to
# the checkout root, because that is the run step's working directory.
SCRIPT_IN_WORKFLOW = "scripts/check_release_versions.py"
RELEASE_ONLY = "github.event_name == 'release'"

try:  # PyYAML is the `deploy` extra, not a runtime dependency of this library
    import yaml

    HAS_YAML = True
except ImportError:  # pragma: no cover - the default-install CI job takes this path
    HAS_YAML = False

needs_yaml = unittest.skipUnless(HAS_YAML, "needs the `deploy` extra (PyYAML)")


def run_check(root: Path, tag: str | None, *, ref_name: str | None = None):
    """Drive the real script the way the release job does, from a fabricated tree.

    `--root` is what lets the suite hand it numbers of its choosing; the tag
    arrives as an argument, or from GITHUB_REF_NAME when none is given, which is
    the path CI takes.
    """
    environment = {key: value for key, value in os.environ.items() if key != "GITHUB_REF_NAME"}
    if ref_name is not None:
        environment["GITHUB_REF_NAME"] = ref_name
    command = [sys.executable, str(SCRIPT), "--root", str(root)]
    if tag is not None:
        command.append(tag)
    return subprocess.run(command, capture_output=True, text=True, env=environment, check=False)


class ReleaseTree:
    """A checkout with just the two files the check reads, at chosen versions."""

    def __init__(self, directory: str, manifest: str | None, declaration: str) -> None:
        self.root = Path(directory)
        plugin = self.root / ".claude-plugin"
        plugin.mkdir(parents=True)
        body: dict[str, object] = {"name": "knowledge-store"}
        if manifest is not None:
            body["version"] = manifest
        (plugin / "plugin.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        skill = self.root / "skills" / "knowledge-store-build"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(declaration, encoding="utf-8")


def declaring(minimum: str) -> str:
    """The floor sentence as the build skill writes it, at a chosen version."""
    return f"**This skill assumes knowledge-store-builder {minimum} or newer.** Check first.\n"


def wiring_problem(text: str) -> str | None:
    """The complaint about a workflow that does not run the check before it
    publishes, or None.

    A function rather than a method so the sensitivity checks below can drive it
    with forged workflows without constructing a TestCase to reach it.
    """
    workflow = yaml.safe_load(text)
    triggers = workflow.get(True, workflow.get("on"))
    if not isinstance(triggers, dict) or "release" not in triggers:
        return "build.yml does not trigger on a release, so nothing checks a tag"
    publish = workflow.get("jobs", {}).get("publish")
    if not isinstance(publish, dict):
        return "build.yml has no publish job"
    steps = publish.get("steps", [])
    checks = [i for i, step in enumerate(steps) if SCRIPT_IN_WORKFLOW in str(step.get("run"))]
    publishes = [
        i
        for i, step in enumerate(steps)
        if "twine upload" in str(step.get("run")) or "uv build" in str(step.get("run"))
    ]
    if not publishes:
        return "the publish job neither builds nor uploads, so this gate reads nothing"
    if not checks:
        return f"no step in the publish job runs {SCRIPT_IN_WORKFLOW}"
    if min(checks) > min(publishes):
        return (
            f"{SCRIPT_IN_WORKFLOW} runs at step {min(checks)}, after the publish job "
            f"starts building or uploading at step {min(publishes)}"
        )
    condition = steps[min(checks)].get("if")
    if condition != RELEASE_ONLY:
        return (
            f"the step running {SCRIPT_IN_WORKFLOW} is conditioned on {condition!r} "
            f"rather than {RELEASE_ONLY!r}, so what it runs on is no longer known"
        )
    return None


class TheReleaseCheckReadsBothNumbers(unittest.TestCase):
    """Exit status and text, over the five states the release job can meet."""

    def check(self, manifest: str | None, declaration: str, tag: str | None, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            ReleaseTree(directory, manifest, declaration)
            return run_check(Path(directory), tag, **kwargs)

    def test_both_numbers_matching_the_tag_pass(self):
        """Breaks if the check refuses the state a release is supposed to be in.

        A check that cannot pass gets switched off, so the steady state is pinned
        as hard as the failures are.
        """
        finished = self.check("0.15.0", declaring("0.15.0"), "v0.15.0")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("0.15.0", finished.stdout)

    def test_a_manifest_behind_the_tag_fails_and_says_what_it_should_say(self):
        """Breaks if the manifest stops being compared, or the complaint stops
        being actionable. This is the drift that happened: the manifest sat four
        releases behind while a test asking only whether it parsed stayed green."""
        finished = self.check("0.14.0", declaring("0.15.0"), "v0.15.0")
        self.assertEqual(finished.returncode, 1)
        self.assertIn(".claude-plugin/plugin.json", finished.stderr)
        self.assertIn("0.14.0", finished.stderr)
        self.assertIn("behind", finished.stderr)
        self.assertIn("should say 0.15.0", finished.stderr)
        self.assertNotIn("SKILL.md", finished.stderr, "the skill agreed and must not be named")

    def test_a_skill_floor_behind_the_tag_fails_and_names_the_skill(self):
        """Breaks if only the manifest is compared. An operator who installs the
        version the skill names gets `unknown stage` for every stage added since."""
        finished = self.check("0.15.0", declaring("0.14.0"), "v0.15.0")
        self.assertEqual(finished.returncode, 1)
        self.assertIn("skills/knowledge-store-build/SKILL.md", finished.stderr)
        self.assertIn("0.14.0", finished.stderr)
        self.assertIn("behind", finished.stderr)
        self.assertIn("should say 0.15.0", finished.stderr)
        self.assertNotIn(
            "plugin.json", finished.stderr, "the manifest agreed and must not be named"
        )

    def test_both_behind_are_both_reported(self):
        """Breaks if the check stops at the first problem: a release that fails
        twice for one omission costs two cycles instead of one."""
        finished = self.check("0.13.0", declaring("0.14.0"), "v0.15.0")
        self.assertEqual(finished.returncode, 1)
        self.assertIn("plugin.json", finished.stderr)
        self.assertIn("SKILL.md", finished.stderr)
        self.assertIn("0.13.0", finished.stderr)
        self.assertIn("0.14.0", finished.stderr)

    def test_a_number_ahead_of_the_tag_fails(self):
        """Breaks if `ahead` is quietly accepted at release time.

        Before the tag exists, ahead is a release being prepared and has to pass.
        At release time it is a different question: the tag is the version being
        published, so a number above it advertises a release that does not exist -
        somebody bumped for a later one, or is cutting the wrong tag.
        """
        finished = self.check("0.16.0", declaring("0.16.0"), "v0.15.0")
        self.assertEqual(finished.returncode, 1)
        self.assertIn("ahead", finished.stderr)
        self.assertIn("0.16.0", finished.stderr)
        self.assertIn("should say 0.15.0", finished.stderr)

    def test_it_compares_minor_and_patch_numerically(self):
        """String comparison would read 0.9.0 as newer than 0.14.0, and would keep
        reading it that way for the whole 0.1x series."""
        finished = self.check("0.9.0", declaring("0.9.0"), "v0.14.0")
        self.assertEqual(finished.returncode, 1)
        self.assertIn("behind", finished.stderr)

    def test_a_manifest_with_no_version_fails(self):
        """Breaks if an absent number reads as agreement. Nothing to compare is a
        release-blocking defect in the tree, not a pass."""
        finished = self.check(None, declaring("0.15.0"), "v0.15.0")
        self.assertEqual(finished.returncode, 1)
        self.assertIn("plugin.json", finished.stderr)
        self.assertIn("no version", finished.stderr)

    def test_an_unreadable_manifest_version_fails(self):
        """Breaks if an unparseable number is compared as though it parsed - the
        shape a two-part version or a `-rc1` suffix would arrive in."""
        finished = self.check("0.15", declaring("0.15.0"), "v0.15.0")
        self.assertEqual(finished.returncode, 1)
        self.assertIn("plugin.json", finished.stderr)
        self.assertIn("0.15", finished.stderr)

    def test_a_skill_that_declares_no_floor_fails(self):
        """Breaks if a reworded sentence reads as compliance rather than as the
        check having lost the thing it reads."""
        finished = self.check("0.15.0", "This skill needs a recent library.\n", "v0.15.0")
        self.assertEqual(finished.returncode, 1)
        self.assertIn("SKILL.md", finished.stderr)
        self.assertIn("no library minimum", finished.stderr)

    def test_a_tag_that_is_not_a_release_tag_cannot_be_compared(self):
        """Breaks if junk is parsed into a number. Exit 2 rather than 1: the check
        was not given something it can compare, which is a different fault from a
        stale number and points at the invocation."""
        finished = self.check("0.15.0", declaring("0.15.0"), "banana")
        self.assertEqual(finished.returncode, 2)
        self.assertIn("banana", finished.stderr)

    def test_a_two_part_tag_cannot_be_compared(self):
        """`v1.2` is the near miss a permissive pattern would accept and read as
        1.2.0, silently comparing against a version nobody cut."""
        finished = self.check("0.15.0", declaring("0.15.0"), "v1.2")
        self.assertEqual(finished.returncode, 2)
        self.assertIn("v1.2", finished.stderr)

    def test_no_tag_at_all_cannot_be_compared(self):
        """Breaks if an absent tag passes. The release job supplies it through
        GITHUB_REF_NAME, and a check that succeeds when that is unset is exactly
        the vacuous gate this replaces one of."""
        finished = self.check("0.15.0", declaring("0.15.0"), None)
        self.assertEqual(finished.returncode, 2)
        self.assertIn("GITHUB_REF_NAME", finished.stderr)

    def test_the_tag_is_read_from_the_environment_when_no_argument_is_given(self):
        """The path CI takes: GITHUB_REF_NAME is a default Actions variable and on
        a release event it holds the tag. Breaks if only the argument is read, which
        would make the wired-up check exit 2 on every release."""
        finished = self.check("0.15.0", declaring("0.15.0"), None, ref_name="v0.15.0")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)

    def test_the_environment_tag_is_compared_rather_than_assumed_to_agree(self):
        """The control for the test above: proves the environment value is read as
        the tag rather than merely making the check stop complaining."""
        finished = self.check("0.14.0", declaring("0.14.0"), None, ref_name="v0.15.0")
        self.assertEqual(finished.returncode, 1)
        self.assertIn("v0.15.0", finished.stderr)

    def test_an_argument_wins_over_the_environment(self):
        """Breaks if the environment silently overrides an explicit tag, which would
        make a local run report on whatever the shell happened to hold."""
        finished = self.check("0.15.0", declaring("0.15.0"), "v0.14.0", ref_name="v0.15.0")
        self.assertEqual(finished.returncode, 1)
        self.assertIn("v0.14.0", finished.stderr)


class TheCheckedTreeIsThisRepository(unittest.TestCase):
    """The fabricated trees above prove the script works. This proves it is pointed
    at the real one, and that the real one is in the state a release needs."""

    def test_it_reads_this_checkout_by_default(self):
        """Breaks if the default root stops resolving to the repository, which would
        make the release job check an empty tree and pass."""
        environment = {key: value for key, value in os.environ.items() if key != "GITHUB_REF_NAME"}
        manifest = json.loads(
            ROOT.joinpath(".claude-plugin/plugin.json").read_text(encoding="utf-8")
        )
        finished = subprocess.run(
            [sys.executable, str(SCRIPT), f"v{manifest['version']}"],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn(manifest["version"], finished.stdout)


class TheReleaseCheckIsWiredIntoTheReleaseJob(unittest.TestCase):
    """A committed check that no workflow calls is decoration.

    `build.yml` is where the release event arrives, and a step is as easy to lose
    in a reordering as it is to add. Parsed with PyYAML rather than matched with a
    regex: `on` is a YAML 1.1 boolean, so a naive read of the key `"on"` finds
    nothing and reports the triggers as absent.
    """

    @needs_yaml
    def test_the_release_check_runs_before_the_publish_job_builds_or_uploads(self):
        """Breaks if the step is deleted, moved after the build or upload, or has its
        release condition changed - the ways a wired check becomes an unrun one."""
        problem = wiring_problem(WORKFLOW.read_text(encoding="utf-8"))
        self.assertIsNone(problem, problem)

    def test_the_script_the_workflow_names_exists(self):
        """Breaks if the script is renamed or moved without the workflow following.

        The workflow would then fail at release time with `No such file`, which is
        loud - but only once a release is being cut, which is the worst moment to
        find out.
        """
        self.assertTrue(
            ROOT.joinpath(SCRIPT_IN_WORKFLOW).is_file(),
            f"{SCRIPT_IN_WORKFLOW} is named by build.yml but is not in the tree",
        )

    @needs_yaml
    def test_the_workflow_names_the_script_verbatim(self):
        """Breaks if the workflow reaches the check some other way than the path this
        gate reads, which would leave the gate green over a step it never saw."""
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(SCRIPT_IN_WORKFLOW, text)


class TheWiringGateCanStillTell(unittest.TestCase):
    """Break what the gate protects, confirm it notices - in this run.

    The assertion above can only pass or fail; it cannot report that it has stopped
    discriminating. So it is driven against forged workflows, each missing exactly
    one of the properties it claims to check, rather than trusted because the real
    workflow happens to be wired.
    """

    def workflow(self, steps: list[dict[str, object]], *, release: bool = True) -> str:
        triggers = "  push:\n    branches: [main]\n"
        if release:
            triggers += "  release:\n    types: [published]\n"
        body = {"jobs": {"publish": {"steps": steps}}}
        return "on:\n" + triggers + yaml.safe_dump(body, sort_keys=True)

    def setUp(self):
        if not HAS_YAML:
            self.skipTest("needs the `deploy` extra (PyYAML)")
        self.check_step: dict[str, object] = {
            "name": "Check the versions",
            "if": RELEASE_ONLY,
            "run": f"python3 {SCRIPT_IN_WORKFLOW}",
        }
        self.build_step: dict[str, object] = {"name": "Build", "run": "uv build"}
        self.upload_step: dict[str, object] = {"name": "Publish", "run": "uvx twine upload dist/*"}

    def test_the_shape_it_accepts(self):
        """The control: without this, every assertion below could be reporting a
        problem with the fixture rather than with what it forged."""
        text = self.workflow([self.check_step, self.build_step, self.upload_step])
        self.assertIsNone(wiring_problem(text), wiring_problem(text))

    def test_it_reports_a_missing_step(self):
        text = self.workflow([self.build_step, self.upload_step])
        problem = wiring_problem(text)
        self.assertIsNotNone(problem, "a publish job with no check at all read as wired")
        assert problem is not None
        self.assertIn(SCRIPT_IN_WORKFLOW, problem)

    def test_it_reports_a_step_that_runs_after_the_build(self):
        text = self.workflow([self.build_step, self.check_step, self.upload_step])
        problem = wiring_problem(text)
        self.assertIsNotNone(problem, "a check running after the build read as wired")
        assert problem is not None
        self.assertIn("after the publish job", problem)

    def test_it_reports_a_step_that_runs_after_the_upload(self):
        text = self.workflow([self.build_step, self.upload_step, self.check_step])
        problem = wiring_problem(text)
        self.assertIsNotNone(problem, "a check running after the upload read as wired")

    def test_it_reports_a_changed_condition(self):
        """A condition that is never true is the vacuous form of this step, and it
        looks identical in the checks list to one that ran."""
        never = dict(self.check_step, **{"if": "github.event_name == 'schedule'"})
        text = self.workflow([never, self.build_step, self.upload_step])
        problem = wiring_problem(text)
        self.assertIsNotNone(problem, "a step that cannot run on a release read as wired")
        assert problem is not None
        self.assertIn("schedule", problem)

    def test_it_reports_a_workflow_that_no_release_reaches(self):
        text = self.workflow([self.check_step, self.build_step, self.upload_step], release=False)
        problem = wiring_problem(text)
        self.assertIsNotNone(problem, "a workflow with no release trigger read as wired")
        assert problem is not None
        self.assertIn("release", problem)

    def test_it_reads_the_trigger_through_the_yaml_boolean(self):
        """The trap this gate was told to avoid: `on` parses as True, so a gate
        reading the key "on" finds nothing and reports every workflow as untriggered.
        This asserts the accepted shape above really was read, not defaulted past."""
        parsed = yaml.safe_load(self.workflow([self.check_step, self.upload_step]))
        self.assertIn(True, parsed, "PyYAML no longer resolves `on:` to a boolean key")
        self.assertNotIn("on", parsed)


if __name__ == "__main__":
    unittest.main()
