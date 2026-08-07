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
