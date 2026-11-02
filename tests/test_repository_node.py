"""A repository is a repository node: what `deploys` and `USES_MODULE` point at (#536).

Both stages used to stand for a repository with the lowest node id it held, which
is whatever sorts first - a document sentence, a symbol, a pipeline file - so
"service X deploys <that sentence>" read as a fact. These tests run the real stages
over a real graph file and read what they wrote.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated
from knowledgestore import build_deployments as deployments
from knowledgestore import (
    build_explorer,
    config,
    deploy_values,
    kinds,
    repository_node,
    store_index,
)
from knowledgestore import build_package_edges as packages
from knowledgestore.server import graph as server_graph

try:  # PyYAML is the `deploy` extra, not a runtime dependency of this library
    import yaml  # noqa: F401

    HAS_YAML = True
except ImportError:  # pragma: no cover - the default-install CI job takes this path
    HAS_YAML = False

needs_yaml = unittest.skipUnless(HAS_YAML, "needs the `deploy` extra (PyYAML)")

# Sorts before every other id in `alpha`: digits precede letters. It is a sentence
# of a document, which is the kind of node the old representative landed on.
SENTENCE = "alpha::0_report_a_bug_through_the_bounty_programme"


def _node(node_id: str, source_file: str, label: str) -> dict:
    return {"id": node_id, "label": label, "repo": "alpha", "source_file": source_file}


def _store(
    tmp: Path, services: tuple[str, ...] = ("alpha-agent", "alpha-backend"), values: str = ""
) -> None:
    """`alpha`, whose lowest id is a document sentence and which consumes a Terraform
    module, and a deployment clone declaring the named services in one environment."""
    alpha = tmp / "repositories" / "alpha"
    (alpha / ".git").mkdir(parents=True)
    (alpha / "main.tf").write_text(
        'module "net" {\n  source = "github.com/example-org/tf-network"\n}\n', encoding="utf-8"
    )
    environment = tmp / "repositories" / "estate-deploy" / "ansible" / "group_vars" / "prd"
    (tmp / "repositories" / "estate-deploy" / ".git").mkdir(parents=True)
    environment.mkdir(parents=True)
    for service in services:
        (environment / f"{service}_values.yaml.j2").write_text(
            values or f"replicas: 2\nimage:\n  name: {service}\n", encoding="utf-8"
        )
    (tmp / "graphify-out").mkdir(parents=True)
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            _node(SENTENCE, "SECURITY.md", "Report a bug through the bounty programme"),
            _node("alpha::AgentMain", "src/AgentMain.java", "AgentMain"),
            _node("alpha::main_tf", "main.tf", "main.tf"),
        ],
        "links": [],
    }
    (tmp / "graphify-out" / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    config.configure(root=tmp, DEPLOY_REPOS={"estate-deploy"}, GITHUB_ORG="example-org")


def _run(stage) -> str:
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        if stage.main() != 0:
            raise AssertionError(f"{stage.__name__} failed: {said.getvalue()}")
    return said.getvalue()


def _graph() -> dict:
    return json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))


def _repository_nodes(graph: dict) -> list[dict]:
    return [n for n in graph["nodes"] if n["id"].endswith("::repository:self")]


def _edges(graph: dict, relation: str) -> list[tuple[str, str]]:
    return sorted((e["source"], e["target"]) for e in graph["links"] if e["relation"] == relation)


@needs_yaml
class DeploysTargetTheRepository(SettingsIsolated, unittest.TestCase):
    def test_deploys_ends_on_the_repository_not_on_its_lowest_node(self):
        """Break caught: `deploys` anchored on the repository's lowest node id again.
        Here that is a sentence of SECURITY.md, which an asker would read as the
        thing the service deploys."""
        with tempfile.TemporaryDirectory() as raw:
            _store(Path(raw))
            lowest = min(n["id"] for n in _graph()["nodes"] if n["repo"] == "alpha")
            self.assertEqual(lowest, SENTENCE, "the premise: the lowest id is a sentence")
            _run(deployments)
            graph = _graph()
        self.assertEqual(
            _edges(graph, "deploys"),
            [
                ("estate-deploy::deploy:prd:alpha-agent", "alpha::repository:self"),
                ("estate-deploy::deploy:prd:alpha-backend", "alpha::repository:self"),
            ],
        )
        node = _repository_nodes(graph)[0]
        self.assertEqual(
            (node["label"], node["repo"], node["metadata"]["kind"], node["source_file"]),
            ("alpha", "alpha", "repository", None),
        )

    def test_a_service_no_repository_matches_gets_no_edge_and_is_counted(self):
        """Break caught: an unmatched service given an edge to some node anyway - the
        fallback that fabricates a relationship - or left out of the tally, which
        reads as a complete join."""
        with tempfile.TemporaryDirectory() as raw:
            _store(Path(raw), services=("omega-agent",))
            printed = _run(deployments)
            graph = _graph()
        self.assertEqual(_edges(graph, "deploys"), [])
        self.assertEqual(_repository_nodes(graph), [])
        self.assertIn("joined to a repository in the graph: 0 of 1", printed)
        self.assertIn("omega-agent", printed)


@needs_yaml
class OneRepositoryNode(SettingsIsolated, unittest.TestCase):
    def test_a_second_run_writes_the_same_graph_byte_for_byte(self):
        """Break caught: a rerun that duplicates the repository node, drops it while
        its edges stay, or moves it, so two builds of one input differ."""
        # A stage run alone is its own case: with a second stage after it, that
        # stage's tail placement hides a first stage that leaves the node mid-list.
        for stages in (
            (deployments,),
            (packages,),
            (packages, deployments),
            (deployments, packages),
        ):
            with self.subTest(order=[stage.FORMAT for stage in stages]):
                with tempfile.TemporaryDirectory() as raw:
                    _store(Path(raw))
                    for stage in stages:
                        _run(stage)
                    first = config.GRAPH_PATH.read_bytes()
                    for stage in stages:
                        _run(stage)
                    second = config.GRAPH_PATH.read_bytes()
                self.assertEqual(first, second)

    def test_packages_and_deployments_share_one_repository_node_in_either_order(self):
        """Break caught: each stage creating its own node (a duplicate id), or one
        stage's strip deleting the node the other's edge points at, leaving a
        dangling edge."""
        for stages in ((packages, deployments), (deployments, packages)):
            with self.subTest(order=[stage.FORMAT for stage in stages]):
                with tempfile.TemporaryDirectory() as raw:
                    _store(Path(raw))
                    for stage in (*stages, *stages):
                        _run(stage)
                    graph = _graph()
                ids = [n["id"] for n in graph["nodes"]]
                self.assertEqual(ids.count("alpha::repository:self"), 1)
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(
                    _edges(graph, "USES_MODULE"), [("alpha::repository:self", "tf-network::module")]
                )
                self.assertEqual(
                    {target for _, target in _edges(graph, "deploys")}, {"alpha::repository:self"}
                )
                present = set(ids)
                self.assertEqual(
                    [e for e in graph["links"] if {e["source"], e["target"]} - present], []
                )


@needs_yaml
class NoOrphanedRepositoryNode(SettingsIsolated, unittest.TestCase):
    def test_a_repository_node_lives_only_while_some_stage_still_links_it(self):
        """Break caught: a stage leaving the repository node behind once it no longer
        links the repository, so the explorer lists a repository with no relations and
        the graph differs from a fresh build of the same inputs."""
        with tempfile.TemporaryDirectory() as raw:
            _store(Path(raw), services=("alpha-agent",))
            _run(packages)
            _run(deployments)
            self.assertEqual(len(_edges(_graph(), "deploys")), 1)
            # The deployment repository now declares a service nothing matches: the
            # deploys edges go, the USES_MODULE edge from `packages` remains.
            group = Path(raw) / "repositories" / "estate-deploy" / "ansible" / "group_vars" / "prd"
            (group / "alpha-agent_values.yaml.j2").unlink()
            (group / "omega-agent_values.yaml.j2").write_text(
                "replicas: 2\nimage:\n  name: omega-agent\n", encoding="utf-8"
            )
            _run(deployments)
            kept = _graph()
            self.assertEqual(_edges(kept, "deploys"), [])
            self.assertEqual([n["id"] for n in _repository_nodes(kept)], ["alpha::repository:self"])
            # Nothing links it any more.
            (Path(raw) / "repositories" / "alpha" / "main.tf").unlink()
            _run(packages)
            rerun = config.GRAPH_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as raw:
            _store(Path(raw), services=("omega-agent",))
            (Path(raw) / "repositories" / "alpha" / "main.tf").unlink()
            _run(packages)
            _run(deployments)
            fresh = config.GRAPH_PATH.read_bytes()
        self.assertEqual(_repository_nodes(json.loads(rerun)), [])
        self.assertEqual(rerun, fresh)

    def test_deployments_alone_leaves_none_once_nothing_matches(self):
        """Break caught: `deployments` leaving the node it created after a rerun in
        which no service matches a repository any more."""
        with tempfile.TemporaryDirectory() as raw:
            _store(Path(raw), services=("alpha-agent",))
            _run(deployments)
            self.assertEqual(len(_repository_nodes(_graph())), 1)
            group = Path(raw) / "repositories" / "estate-deploy" / "ansible" / "group_vars" / "prd"
            (group / "alpha-agent_values.yaml.j2").rename(group / "omega-agent_values.yaml.j2")
            _run(deployments)
            self.assertEqual(_repository_nodes(_graph()), [])


@needs_yaml
class ConfigTruncation(SettingsIsolated, unittest.TestCase):
    def test_a_capped_config_says_how_many_keys_were_dropped(self):
        """Break caught: `KSB_DEPLOY_MAX_KEYS` truncating with no marker, so a key a
        reader cannot find reads as a key the service never set."""
        with tempfile.TemporaryDirectory() as raw:
            _store(Path(raw), services=("alpha-agent",), values="a: 1\nb: 2\nc: 3\nd: 4\ne: 5\n")
            config.configure(DEPLOY_MAX_KEYS=2)
            _run(deployments)
            node = next(n for n in _graph()["nodes"] if n["label"] == "alpha-agent (prd)")
        self.assertEqual(node["metadata"]["config"], {"a": "1", "b": "2"})
        self.assertEqual(node["metadata"]["config_truncated"], 3)

    def test_a_config_within_the_cap_carries_no_marker(self):
        """Break caught: the marker written as 0 on every node, which changes every
        committed deployment node for a store nothing truncated."""
        with tempfile.TemporaryDirectory() as raw:
            _store(Path(raw), services=("alpha-agent",), values="a: 1\nb: 2\n")
            config.configure(DEPLOY_MAX_KEYS=2)
            _run(deployments)
            node = next(n for n in _graph()["nodes"] if n["label"] == "alpha-agent (prd)")
        self.assertNotIn("config_truncated", node["metadata"])

    def test_flatten_counts_what_the_cap_dropped(self):
        """Break caught: the count taken before the secret-location policy, so a
        withheld key reads as truncated, or after the cap, so it is always 0."""
        flat = deploy_values.flatten({"a": 1, "b": 2, "c": 3}, 2, 100)
        self.assertEqual((dict(flat), flat.dropped), ({"a": "1", "b": "2"}, 1))
        self.assertEqual(deploy_values.flatten({"a": 1}, 2, 100).dropped, 0)


@needs_yaml
class ConsumersReadARepositoryNode(SettingsIsolated, unittest.TestCase):
    def test_the_explorer_and_both_indexes_take_a_node_with_no_source_file(self):
        """Break caught: a consumer assuming every node has a `source_file` (CLAUDE.md
        6.4), or reading the new kind as something else."""
        with tempfile.TemporaryDirectory() as raw:
            _store(Path(raw))
            _run(deployments)
            graph = _graph()
            node = _repository_nodes(graph)[0]
            entries, _ = build_explorer.build_index(graph, {}, {})
            rows = {row[0]: row for row in store_index._node_rows(config.GRAPH_PATH)}
        self.assertEqual(kinds.node_kind(node), kinds.REPOSITORY)
        entry = next(e for e in entries if e[0] == "alpha" and e[2] == "")
        self.assertEqual(entry[1], "alpha")
        row = rows["alpha::repository:self"]
        self.assertEqual((row[3], row[4]), ("repository", ""))
        self.assertEqual(next(iter(server_graph._node_rows([node]))), row)


if __name__ == "__main__":
    unittest.main()


def _plant(*, nodes: list[dict] = (), links: list[dict] = ()) -> None:
    """Add to the graph file `_store` wrote, as an earlier stage or graphify would have."""
    graph = _graph()
    graph["nodes"].extend(nodes)
    graph["links"].extend(links)
    config.GRAPH_PATH.write_text(json.dumps(graph), encoding="utf-8")


def _link(source: str, target: str, relation: str, origin: str | None = None) -> dict:
    return {"source": source, "target": target, "relation": relation, "_origin": origin}


# What graphify emits for a class named `Repository` in `alpha`: the id the repository
# node used to take, with another origin.
OLD_ID = "alpha::repository"
CLASS_NODE = {
    "id": OLD_ID,
    "label": "Repository",
    "repo": "alpha",
    "source_file": "src/Repository.java",
    "_origin": "ast",
}
CLASS_EDGE = _link("alpha::AgentMain", OLD_ID, "uses", "ast")


def _previous_version_repository_node() -> dict:
    """The node the previous version wrote, as it was written."""
    return {
        "id": OLD_ID,
        "label": "alpha",
        "norm_label": "alpha",
        "file_type": "concept",
        "source_file": None,
        "source_location": None,
        "repo": "alpha",
        "_origin": repository_node.ORIGIN,
        "local_id": "repository",
        "metadata": {"kind": kinds.REPOSITORY},
    }


@needs_yaml
class AGraphifyNodeCannotBecomeTheRepository(SettingsIsolated, unittest.TestCase):
    def test_a_class_named_repository_is_not_adopted_and_is_left_as_it_was(self):
        """Break caught: the repository node's id being one graphify can emit, so a
        class named `Repository` is taken for the repository and `deploys` and
        `USES_MODULE` land on code; or the stage rewriting the class it found."""
        for stages in ((deployments,), (packages,), (packages, deployments)):
            with self.subTest(order=[stage.FORMAT for stage in stages]):
                with tempfile.TemporaryDirectory() as raw:
                    _store(Path(raw))
                    _plant(nodes=[CLASS_NODE], links=[CLASS_EDGE])
                    for stage in stages:
                        _run(stage)
                    graph = _graph()
                by_id = {n["id"]: n for n in graph["nodes"]}
                self.assertEqual(by_id[OLD_ID], CLASS_NODE)
                self.assertEqual(by_id["alpha::repository:self"]["_origin"], "repository")
                self.assertEqual(
                    [e["target"] for e in graph["links"] if e["relation"] == "uses"], [OLD_ID]
                )
                landed = {e["target"] for e in graph["links"] if e["relation"] == "deploys"} | {
                    e["source"] for e in graph["links"] if e["relation"] == "USES_MODULE"
                }
                self.assertEqual(landed, {"alpha::repository:self"})

    def test_a_second_run_over_the_collision_writes_the_same_bytes(self):
        """Break caught: a rerun that adds, drops or moves a node when the class and
        the repository both exist, so two builds of one input differ."""
        with tempfile.TemporaryDirectory() as raw:
            _store(Path(raw))
            _plant(nodes=[CLASS_NODE], links=[CLASS_EDGE])
            for stage in (packages, deployments):
                _run(stage)
            first = config.GRAPH_PATH.read_bytes()
            for stage in (packages, deployments):
                _run(stage)
            self.assertEqual(first, config.GRAPH_PATH.read_bytes())

    def test_a_foreign_node_at_the_repository_id_is_refused_not_adopted(self):
        """Break caught: `ensure_repository_node` taking whatever holds the id, so the
        edges land on a node that is not a repository. The stage must say so and
        leave the graph as it found it."""
        foreign = {**CLASS_NODE, "id": "alpha::repository:self"}
        with tempfile.TemporaryDirectory() as raw:
            _store(Path(raw))
            _plant(nodes=[foreign])
            before = config.GRAPH_PATH.read_bytes()
            graph = _graph()
            with self.assertRaises(repository_node.ForeignNodeError):
                repository_node.ensure_repository_node(graph, "alpha")
            for stage in (deployments, packages):
                said = io.StringIO()
                with contextlib.redirect_stderr(said):
                    self.assertEqual(stage.main(), 1, stage.__name__)
                self.assertIn("alpha::repository:self", said.getvalue())
                self.assertIn("refusing to adopt", said.getvalue())
            self.assertEqual(config.GRAPH_PATH.read_bytes(), before)


@needs_yaml
class AGraphFromThePreviousVersionMigrates(SettingsIsolated, unittest.TestCase):
    def test_the_old_node_takes_the_new_id_and_its_edges_follow(self):
        """Break caught: a re-run leaving the old-id node behind, or moving the node
        but not the edges of the stage that did not run, which then dangle. The class
        sharing the old id in the same graph must stay with its own edge."""
        old = _previous_version_repository_node()
        with tempfile.TemporaryDirectory() as raw:
            _store(Path(raw))
            # Both layers as the previous version wrote them; only `deployments` runs,
            # so the module edge is one it does not rebuild and must still retarget.
            _plant(
                nodes=[old],
                links=[
                    _link(OLD_ID, "tf-network::module", "USES_MODULE", "package"),
                    _link("estate-deploy::deploy:prd:alpha-agent", OLD_ID, "deploys", "deployment"),
                ],
            )
            _run(deployments)
            graph = _graph()
        ids = [n["id"] for n in graph["nodes"]]
        self.assertNotIn(OLD_ID, ids)
        self.assertEqual(ids.count("alpha::repository:self"), 1)
        self.assertEqual(
            _edges(graph, "USES_MODULE"), [("alpha::repository:self", "tf-network::module")]
        )
        self.assertEqual({t for _, t in _edges(graph, "deploys")}, {"alpha::repository:self"})
        moved = next(n for n in graph["nodes"] if n["id"] == "alpha::repository:self")
        self.assertEqual({**moved, "id": OLD_ID}, old)

    def test_a_class_at_the_old_id_stays_when_the_old_node_is_gone(self):
        """Break caught: migration rewriting by id alone, so it takes the class that
        shares the old id and re-points the class's own edges at the repository."""
        with tempfile.TemporaryDirectory() as raw:
            _store(Path(raw))
            _plant(nodes=[CLASS_NODE], links=[CLASS_EDGE])
            graph = _graph()
            repository_node.migrate_repository_nodes(graph)
            self.assertEqual(graph, _graph())

    def test_a_migrated_graph_is_the_graph_a_fresh_build_writes_and_stays_put(self):
        """Break caught: migration that is not a fixed point, or that leaves a trace
        (a stale twin, an edge on the old id), so a migrated store differs from one
        built fresh, or every run of it rewrites the graph."""
        with tempfile.TemporaryDirectory() as raw:
            _store(Path(raw))
            for stage in (packages, deployments):
                _run(stage)
            fresh = config.GRAPH_PATH.read_bytes()
            # The previous version's graph: this one with the repository id as it was.
            old = fresh.replace(b"alpha::repository:self", OLD_ID.encode())
            self.assertNotEqual(old, fresh, "the premise: the graph holds the new id")
            config.GRAPH_PATH.write_bytes(old)
            for stage in (packages, deployments):
                _run(stage)
            migrated = config.GRAPH_PATH.read_bytes()
            for stage in (packages, deployments):
                _run(stage)
            self.assertEqual(migrated, fresh)
            self.assertEqual(config.GRAPH_PATH.read_bytes(), fresh)
