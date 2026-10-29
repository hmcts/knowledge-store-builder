"""Every stage that reads the graph copes with a repository node (#536).

A repository node has `repo` set and no `source_file`, which is what no extractor
node looks like: `gherkin` once called `.endswith` on that missing path and
crashed on the first store that had run `deployments`. The way such a consumer is
found is by running it, so this runs every graph-reading stage over the same small
store with and without the node.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated
from knowledgestore import cli, config, repository_node

# Graph-reading stages that are not run here, and why. A stage that reads the graph
# and is in neither this table nor RUN fails `test_every_graph_reader_is_classified`,
# so a new consumer cannot arrive unexamined.
NOT_RUN = {
    "check-grammars": "needs the tree-sitter grammars; reads detect output, not graph nodes",
    "extract-ast": "writes the graph from source; never reads a repository node",
    "merge-inputs": "builds the graph from per-repository inputs, before any enrichment",
    "merge-layers": "builds the graph from layers, before any enrichment",
    "check-citations": "needs git history of a real clone",
    "answers": "takes a question as its argument",
    "semantic": "needs the `semantic` extra's embedding model",
}
RUN: dict[str, list[str]] = {
    "gherkin": [],
    "packages": [],
    "json-schema": [],
    "deployments": [],
    "source-urls": [],
    "size-cuts": [],
    "record-clustering": [],
    "summaries": ["extract"],
    "topics": ["extract"],
    "deepdive": ["extract", "alpha"],
    "explorer": [],
    "index": [],
    "status": [],
    "dangling-endpoints": [],
}
# A stage whose optional dependency is not installed refuses before it reads the
# graph, with or without the node, so running it says nothing: it is left out, and
# named, rather than passed or failed on. CI's default install has no PyYAML.
NEEDS_MODULE = {"deployments": "yaml"}
LEFT_OUT = {
    stage: f"needs {module}, which is not installed"
    for stage, module in NEEDS_MODULE.items()
    if importlib.util.find_spec(module) is None
}
# Stages that must actually reach the graph here, so a harness that has gone vacuous
# (every stage refusing for a missing input) says so instead of passing.
MUST_SUCCEED = (
    "gherkin",
    "packages",
    "json-schema",
    "deployments",
    "source-urls",
    "size-cuts",
    "summaries",
    "topics",
    "deepdive",
    "explorer",
    "index",
    "status",
    "dangling-endpoints",
)


def _reads_the_graph(module: str) -> bool:
    source = (Path(cli.__file__).parent / f"{module}.py").read_text(encoding="utf-8")
    return bool(re.search(r"GRAPH_PATH|graph\.json|graph_files|graph_stream", source))


def _store(root: Path, with_repository_node: bool) -> None:
    alpha = root / "repositories" / "alpha"
    (alpha / ".git").mkdir(parents=True)
    (alpha / "features").mkdir()
    (alpha / "features" / "orders.feature").write_text(
        "Feature: Orders\n  Scenario: An order is placed\n    Given a customer\n",
        encoding="utf-8",
    )
    (alpha / "src").mkdir()
    (alpha / "src" / "Orders.java").write_text("class Orders {}\n", encoding="utf-8")
    deploy = root / "repositories" / "estate-deploy" / "ansible" / "group_vars" / "prd"
    (root / "repositories" / "estate-deploy" / ".git").mkdir(parents=True)
    deploy.mkdir(parents=True)
    (deploy / "alpha-agent_values.yaml.j2").write_text("replicas: 1\n", encoding="utf-8")
    nodes = [
        {
            "id": "alpha::Orders",
            "label": "Orders",
            "repo": "alpha",
            "source_file": "src/Orders.java",
            "file_type": "code",
            "community": 0,
        },
        {
            "id": "alpha::Helper",
            "label": "Helper",
            "repo": "alpha",
            "source_file": "src/Orders.java",
            "file_type": "code",
            "community": 0,
        },
    ]
    graph = {"directed": False, "multigraph": False, "graph": {}, "nodes": nodes, "links": []}
    graph["links"].append(
        {"source": "alpha::Orders", "target": "alpha::Helper", "relation": "calls"}
    )
    if with_repository_node:
        repository_node.ensure_repository_node(graph, "alpha")
        graph["links"].append(
            {"source": "alpha::repository", "target": "alpha::Orders", "relation": "uses"}
        )
    (root / "graphify-out").mkdir()
    config.GRAPH_PATH.write_text(json.dumps(graph), encoding="utf-8")
    # What the stages read besides the merged graph: the per-repository graph the
    # extraction wrote, the detect result, the sync record and the topics list.
    (alpha / "graphify-out").mkdir()
    (alpha / "graphify-out" / "graph.json").write_text(
        json.dumps({**graph, "nodes": nodes, "links": graph["links"][:1]}), encoding="utf-8"
    )
    config.DETECT_PATH.write_text(
        json.dumps({"files": {"code": [str(alpha / "src" / "Orders.java")]}}), encoding="utf-8"
    )
    config.PROVENANCE_PATH.parent.mkdir(exist_ok=True)
    config.PROVENANCE_PATH.write_text(
        json.dumps(
            {
                "repositories": {
                    "alpha": {
                        "sha": "0" * 40,
                        "branch": "main",
                        "committed": "2026-01-01T00:00:00Z",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config.REPOSITORIES_CONFIG.parent.mkdir(exist_ok=True)
    config.REPOSITORIES_CONFIG.write_text(
        "alpha|git@github.com:example-org/alpha.git|main\n", encoding="utf-8"
    )
    config.TOPICS_CONFIG_PATH.parent.mkdir(exist_ok=True)
    config.TOPICS_CONFIG_PATH.write_text("orders | Orders | orders, alpha\n", encoding="utf-8")
    config.LABELS_PATH.write_text(json.dumps({"0": "Orders"}), encoding="utf-8")


def _run(stage: str, arguments: list[str]) -> tuple[str, str]:
    """('ok', '') or ('refused', rc) or ('crashed', 'ExceptionType: message')."""
    module = __import__(f"knowledgestore.{cli.STAGES[stage][0]}", fromlist=["main"])
    argv, sys.argv = sys.argv, [f"knowledgestore {stage}", *arguments]
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = module.main()
    except SystemExit as stop:
        code = stop.code
    except ModuleNotFoundError as missing:
        return "unavailable", str(missing.name)
    except Exception as error:
        return "crashed", f"{type(error).__name__}: {error}"
    finally:
        sys.argv = argv
    return ("ok", "") if not code else ("refused", str(code))


class EveryGraphReaderIsClassified(unittest.TestCase):
    def test_every_graph_reader_is_classified(self):
        """Break caught: a new stage reading the graph that nobody ran against a
        repository node."""
        readers = {s for s, (module, _) in cli.STAGES.items() if _reads_the_graph(module)}
        self.assertEqual(readers - set(RUN) - set(NOT_RUN), set())
        self.assertEqual((set(RUN) | set(NOT_RUN)) - readers, set(), "classified but not a reader")


class EveryStageReadsARepositoryNode(SettingsIsolated, unittest.TestCase):
    def test_no_stage_crashes_on_a_repository_node_and_none_behaves_differently(self):
        """Break caught: a consumer assuming every node with a `repo` has a
        `source_file` or a numeric community, as `gherkin` did. Each stage runs on a
        fresh copy of the store with the node and without it: it must not raise, and
        must finish the way it finishes without the node."""
        outcomes: dict[str, dict[bool, tuple[str, str]]] = {}
        for stage in RUN:
            if stage in LEFT_OUT:
                continue
            for with_node in (False, True):
                with tempfile.TemporaryDirectory() as raw:
                    config.configure(
                        root=Path(raw), DEPLOY_REPOS={"estate-deploy"}, GITHUB_ORG="example-org"
                    )
                    _store(Path(raw), with_node)
                    outcomes.setdefault(stage, {})[with_node] = _run(stage, RUN[stage])
        crashed = {s: o[True] for s, o in outcomes.items() if o[True][0] == "crashed"}
        self.assertEqual(crashed, {})
        differ = {s: o for s, o in outcomes.items() if o[True][0] != o[False][0]}
        self.assertEqual(differ, {})
        self.assertEqual(
            [s for s in MUST_SUCCEED if s not in LEFT_OUT and outcomes[s][True][0] != "ok"],
            [],
            f"these stages never reached the graph, so the check above says nothing: {outcomes}",
        )


if __name__ == "__main__":
    unittest.main()
