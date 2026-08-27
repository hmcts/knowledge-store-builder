"""The suite must run when a release tag is created (#256's retro finding).

Two gates compare a hand-maintained number against the most recent reachable
release tag: the plugin manifest's version, and the build skill's declared library
floor. Creating a tag is the only event that can newly falsify either.

It was also the one event nothing ran. `tests.yml` triggered on `pull_request` and
`push: branches: [main]`, and a tag is neither, so when v0.15.1 shipped without
those numbers being carried, `Build` ran and published while `tests` never
executed. The breakage surfaced on the next unrelated pull request and was
attributed to it.

These tests assert the property - the workflow that runs the suite is triggered by
release tags, and checks out enough history for a tag to be reachable - rather than
the wording of the trigger, so the file can be reorganised without failing this and
cannot lose the trigger without failing it.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/tests.yml"


def triggers() -> dict:
    """`tests.yml`'s `on:` mapping.

    Loaded with a real YAML parser rather than matched with a regex: `on` is a
    YAML 1.1 boolean, so a naive read of the key silently misses it, and that is
    exactly the kind of near-miss this file exists to prevent.
    """
    yaml = __import__("yaml")
    parsed = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML resolves the bare key `on` to True under YAML 1.1.
    return parsed.get("on") or parsed.get(True) or {}


class TheSuiteRunsWhenAReleaseIsTagged(unittest.TestCase):
    def setUp(self):
        try:
            __import__("yaml")
        except ImportError:  # pragma: no cover - depends on the extra
            raise unittest.SkipTest("needs the `deploy` extra (PyYAML) to parse the workflow")

    def test_a_release_tag_triggers_the_suite(self):
        """Breaks if the tag trigger is removed.

        Without it a release can falsify the manifest and floor gates while no run
        exists to notice, which is what happened for v0.15.1.
        """
        tags = (triggers().get("push") or {}).get("tags")
        self.assertTrue(tags, f"{WORKFLOW.name} does not run on any tag push")

    def test_the_pattern_matches_a_release_tag(self):
        """Breaks if the pattern stops matching the tags releases actually use.

        A trigger present but non-matching is worse than none: it reads as covered.
        Checked against the shapes this repository has tagged.
        """
        import fnmatch

        patterns = (triggers().get("push") or {}).get("tags") or []
        for tag in ("v0.15.0", "v0.15.1", "v1.0.0", "v10.2.3"):
            with self.subTest(tag=tag):
                self.assertTrue(
                    any(fnmatch.fnmatch(tag, p) for p in patterns),
                    f"no pattern in {patterns} matches {tag}",
                )

    def test_the_branch_and_pull_request_triggers_survive(self):
        """Breaks if adding the tag trigger cost the triggers that were there.

        `tests` is a required check on the main ruleset; losing the pull_request
        trigger would leave every pull request waiting for a context that never
        arrives, which `CLAUDE.md` records as having deadlocked a docs change.
        """
        on = triggers()
        self.assertIn("pull_request", on)
        self.assertIn("main", (on.get("push") or {}).get("branches") or [])

    def test_the_checkout_can_reach_a_tag(self):
        """Breaks if the fetch depth is narrowed.

        The gates this protects resolve the latest tag with `git describe`, and
        both skip when no tag is reachable. A shallow checkout would make them skip
        on every run - green, and measuring nothing.
        """
        self.assertIn("fetch-depth: 0", WORKFLOW.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
