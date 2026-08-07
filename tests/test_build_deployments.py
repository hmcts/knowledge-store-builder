"""The deployments stage: per-(service, environment) config, joined to services."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

# Importing this first puts the working tree ahead of any installed copy.
from settings_isolation import SettingsIsolated
from knowledgestore import build_deployments as deployments
from knowledgestore import config

try:  # PyYAML is the `deploy` extra, not a runtime dependency of this library
    import yaml  # noqa: F401

    HAS_YAML = True
except ImportError:  # pragma: no cover - the default-install CI job takes this path
    HAS_YAML = False

# Skipping rather than failing is the point: CI runs this suite twice, once on the
# default install and once with the extras, so a dependency creeping into the core
# shows up as the first run failing rather than as nobody noticing.
needs_yaml = unittest.skipUnless(HAS_YAML, "needs the `deploy` extra (PyYAML)")


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


@needs_yaml
class Discovery(SettingsIsolated, unittest.TestCase):
    def test_the_environment_comes_from_the_path_and_the_base_layer_is_named(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo = _deploy_repo(tmp)
            config.configure(root=tmp)
            found, _ = deployments.discover(repo)
        keys = set(found)
        self.assertIn((config.DEPLOY_BASE_ENV, "progression-service"), keys)
        self.assertIn(("prd", "progression-service"), keys)
        self.assertIn(("dev", "sandbox-service"), keys)

    def test_a_templated_value_is_kept_as_a_placeholder_not_dropped(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo = _deploy_repo(tmp)
            config.configure(root=tmp)
            found, _ = deployments.discover(repo)
        from knowledgestore import deploy_values

        self.assertEqual(
            found[("prd", "progression-service")]["replicas"], deploy_values.PLACEHOLDER
        )

    def test_environments_differ_which_is_the_whole_point(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo = _deploy_repo(tmp)
            config.configure(root=tmp)
            found, _ = deployments.discover(repo)
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


class MissingDependency(unittest.TestCase):
    """Without the extra, the stage must say what to install, not raise a traceback.

    This path is what a first-time user hits, and it runs in the default-install
    CI job where PyYAML genuinely is absent. Patching the import here means the
    message is checked in both jobs rather than only the one.
    """

    def test_a_missing_pyyaml_names_the_install_command(self):
        import builtins

        real = builtins.__import__

        def without_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("no yaml")
            return real(name, *args, **kwargs)

        builtins.__import__ = without_yaml
        try:
            with self.assertRaises(SystemExit) as caught:
                deployments._parse("a: 1")
        finally:
            builtins.__import__ = real
        message = str(caught.exception)
        self.assertIn("PyYAML", message)
        self.assertIn("[deploy]", message)


def _graph(tmp: Path) -> None:
    """A graph holding one real service node, so the join has something to hit."""
    (tmp / "graphify-out").mkdir(parents=True, exist_ok=True)
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {
                "id": "cpp-context-progression::CaseAggregate",
                "label": "CaseAggregate",
                "repo": "cpp-context-progression",
                "source_file": "src/main/java/CaseAggregate.java",
            }
        ],
        "links": [],
    }
    (tmp / "graphify-out" / "graph.json").write_text(json.dumps(graph), encoding="utf-8")


@needs_yaml
class Layer(SettingsIsolated, unittest.TestCase):
    def test_the_stage_does_nothing_until_a_repository_is_named(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _graph(tmp)
            _deploy_repo(tmp)
            config.configure(root=tmp)
            self.assertEqual(deployments.main(), 0)
            graph = json.loads((tmp / "graphify-out" / "graph.json").read_text())
        self.assertEqual([n for n in graph["nodes"] if n.get("_origin") == "deployments"], [])

    def test_a_node_per_service_and_environment_with_an_edge_to_the_service_repo(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _graph(tmp)
            _deploy_repo(tmp)
            config.configure(root=tmp, DEPLOY_REPOS={"estate-deploy"})
            self.assertEqual(deployments.main(), 0)
            graph = json.loads((tmp / "graphify-out" / "graph.json").read_text())
        added = [n for n in graph["nodes"] if n.get("_origin") == "deployments"]
        kinds = {n["metadata"]["kind"] for n in added}
        self.assertEqual(kinds, {"deployment", "environment"})
        labels = {n["label"] for n in added if n["metadata"]["kind"] == "deployment"}
        self.assertIn("progression-service (prd)", labels)
        targets = {e["target"] for e in graph["links"]}
        self.assertIn("cpp-context-progression::CaseAggregate", targets)

    def test_the_configuration_travels_on_the_node_so_it_can_be_quoted(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _graph(tmp)
            _deploy_repo(tmp)
            config.configure(root=tmp, DEPLOY_REPOS={"estate-deploy"})
            deployments.main()
            graph = json.loads((tmp / "graphify-out" / "graph.json").read_text())
        node = next(n for n in graph["nodes"] if n.get("label") == "progression-service (prd)")
        self.assertEqual(node["metadata"]["config"]["resources.limits.cpu"], "4")
        self.assertEqual(node["metadata"]["environment"], "prd")

    def test_every_node_cites_a_file_that_exists_including_the_base_layer(self):
        # A citation nobody can open is worse than none: `status` counts it as
        # dangling, and a reader sent to a path that is not there stops trusting
        # the citations that are good. The base layer has no environment segment,
        # so a path built as root/<environment>/<service> is wrong for it.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _graph(tmp)
            repo = _deploy_repo(tmp)
            config.configure(root=tmp, DEPLOY_REPOS={"estate-deploy"})
            deployments.main()
            graph = json.loads((tmp / "graphify-out" / "graph.json").read_text())
            cited = {
                n["source_file"]
                for n in graph["nodes"]
                if n.get("_origin") == "deployments" and n.get("source_file")
            }
            missing = sorted(rel for rel in cited if not (repo / rel).is_file())
        # The assertIn keeps this from passing vacuously on an empty citation set.
        self.assertIn("ansible/group_vars/progression-service_values.yaml.j2", cited)
        self.assertEqual(missing, [])

    def test_running_twice_leaves_the_graph_the_same_size(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _graph(tmp)
            _deploy_repo(tmp)
            config.configure(root=tmp, DEPLOY_REPOS={"estate-deploy"})
            deployments.main()
            first = json.loads((tmp / "graphify-out" / "graph.json").read_text())
            deployments.main()
            second = json.loads((tmp / "graphify-out" / "graph.json").read_text())
        self.assertEqual(len(first["nodes"]), len(second["nodes"]))
        self.assertEqual(len(first["links"]), len(second["links"]))


class Stage(unittest.TestCase):
    def test_the_stage_is_registered_between_packages_and_summaries(self):
        from knowledgestore import cli

        names = list(cli.STAGES)
        self.assertIn("deployments", names)
        self.assertLess(names.index("packages"), names.index("deployments"))
        self.assertLess(names.index("deployments"), names.index("summaries"))


if __name__ == "__main__":
    unittest.main()
