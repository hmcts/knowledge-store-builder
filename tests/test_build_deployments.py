"""The deployments stage: per-(service, environment) config, joined to services."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

# Importing this first puts the working tree ahead of any installed copy.
from settings_isolation import SettingsIsolated
from knowledgestore import build_deployments as deployments
from knowledgestore import config


def _deploy_repo(tmp: Path) -> Path:
    """One base layer and two environments, as a real ansible layout."""
    repo = tmp / "repositories" / "estate-deploy"
    base = repo / "ansible" / "group_vars"
    (repo / ".git").mkdir(parents=True)
    base.mkdir(parents=True)
    (base / "progression-service_values.yaml.j2").write_text(
        "replicas: 1\nresources:\n  limits:\n    cpu: '1'\n", encoding="utf-8"
    )
    (base / "prd").mkdir()
    (base / "prd" / "progression-service_values.yaml.j2").write_text(
        "replicas: {{ progression_replicas }}\nresources:\n  limits:\n    cpu: '4'\n",
        encoding="utf-8",
    )
    (base / "dev").mkdir()
    (base / "dev" / "sandbox-service_values.yaml.j2").write_text("replicas: 1\n", encoding="utf-8")
    return repo


class Discovery(SettingsIsolated, unittest.TestCase):
    def test_the_environment_comes_from_the_path_and_the_base_layer_is_named(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo = _deploy_repo(tmp)
            config.configure(root=tmp)
            found = deployments.discover(repo)
        keys = set(found)
        self.assertIn((config.DEPLOY_BASE_ENV, "progression-service"), keys)
        self.assertIn(("prd", "progression-service"), keys)
        self.assertIn(("dev", "sandbox-service"), keys)

    def test_a_templated_value_is_kept_as_a_placeholder_not_dropped(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo = _deploy_repo(tmp)
            config.configure(root=tmp)
            found = deployments.discover(repo)
        from knowledgestore import deploy_values

        self.assertEqual(
            found[("prd", "progression-service")]["replicas"], deploy_values.PLACEHOLDER
        )

    def test_environments_differ_which_is_the_whole_point(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo = _deploy_repo(tmp)
            config.configure(root=tmp)
            found = deployments.discover(repo)
        self.assertEqual(
            found[(config.DEPLOY_BASE_ENV, "progression-service")]["resources.limits.cpu"], "1"
        )
        self.assertEqual(found[("prd", "progression-service")]["resources.limits.cpu"], "4")


class Matching(unittest.TestCase):
    """The join is by name, and a wrong join is worse than a missing one.

    A missed join shows up in the match-rate report and can be chased. A wrong
    join is counted as a success, inflates the rate, and attaches production
    configuration to unrelated code. These cases are the ones a bare
    normalised-substring rule got wrong.
    """

    REPOS = {
        "cpp-context-progression",
        "cpp-context-idam",
        "cpp-video",
        "cpp-context-scheduling",
        "cpp-context-defence",
        "cpp-context-hearing",
        "cpp-context-hearing-d",
        "cpp-context-unifiedsearch-query",
        "cpp-ui-home",
    }

    def test_a_two_letter_stem_matches_nothing_rather_than_something_wrong(self):
        # `id` is inside `cppvideo`, and `sc` inside `cppcontextscheduling`. A
        # substring rule joined both, confidently and wrongly.
        self.assertEqual(deployments.match_services({"id-service"}, self.REPOS), {})
        self.assertEqual(deployments.match_services({"sc-service"}, self.REPOS), {})

    def test_a_whole_segment_match_beats_a_longer_coincidence(self):
        matched = deployments.match_services({"idam-service"}, self.REPOS)
        self.assertEqual(matched["idam-service"], "cpp-context-idam")

    def test_a_long_stem_may_still_match_across_segment_boundaries(self):
        # `unifiedsearchquery` is no single segment of
        # `cpp-context-unifiedsearch-query`, and is far too long to be a
        # coincidence - this is what the substring fallback exists for.
        matched = deployments.match_services({"unifiedsearchquery-service"}, self.REPOS)
        self.assertEqual(matched["unifiedsearchquery-service"], "cpp-context-unifiedsearch-query")

    def test_a_service_matches_the_repository_that_holds_it(self):
        matched = deployments.match_services(
            {"progression-service", "defence-service"},
            {"cpp-context-progression", "cpp-context-defence", "cpp-ui-home"},
        )
        self.assertEqual(matched["progression-service"], "cpp-context-progression")
        self.assertEqual(matched["defence-service"], "cpp-context-defence")

    def test_an_unmatched_service_is_absent_rather_than_guessed(self):
        matched = deployments.match_services({"artemis"}, {"cpp-context-progression"})
        self.assertNotIn("artemis", matched)

    def test_matching_never_returns_a_repository_twice_for_one_service(self):
        matched = deployments.match_services(
            {"hearing-service"}, {"cpp-context-hearing", "cpp-context-hearing-d"}
        )
        # Ambiguity resolves deterministically to the shortest, then alphabetical
        # name - never to whichever the filesystem listed first.
        self.assertEqual(matched["hearing-service"], "cpp-context-hearing")


if __name__ == "__main__":
    unittest.main()
