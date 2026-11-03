"""Re-running `deployments` keeps what other stages hung on its nodes (#548).

The stage strips its layer and rebuilds it. Before this, the strip took every edge
touching one of its nodes with it - including edges another stage or a store's own
extractor had added - and the rebuilt nodes lost the `url` `source-urls` gave them,
so each re-run silently undid work that only re-running those stages restored.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

# Importing this first puts the working tree ahead of any installed copy.
from settings_isolation import SettingsIsolated
from knowledgestore import build_deployments as deployments
from knowledgestore import config
from test_build_deployments import _deploy_repo, _graph, needs_yaml

PRD = "progression-service (prd)"
DEV = "sandbox-service (dev)"


def _read(tmp: Path) -> dict:
    return json.loads((tmp / "graphify-out" / "graph.json").read_text(encoding="utf-8"))


def _write(tmp: Path, graph: dict) -> None:
    (tmp / "graphify-out" / "graph.json").write_text(json.dumps(graph), encoding="utf-8")


def _id(graph: dict, label: str) -> str:
    return next(n["id"] for n in graph["nodes"] if n.get("label") == label)


def _run() -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        if deployments.main() != 0:
            raise AssertionError(out.getvalue())
    return out.getvalue()


@needs_yaml
class AReRunKeepsOtherStagesWork(SettingsIsolated, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        raw = tempfile.TemporaryDirectory()
        self.addCleanup(raw.cleanup)
        self.tmp = Path(raw.name)
        _graph(self.tmp)
        self.repo = _deploy_repo(self.tmp)
        config.configure(root=self.tmp, DEPLOY_REPOS={"estate-deploy"})
        _run()

    def _hang(self, edges: list[dict], url: str | None = None, source_file: str | None = None):
        graph = _read(self.tmp)
        graph["links"].extend(edges)
        node = next(n for n in graph["nodes"] if n.get("label") == PRD)
        if url:
            node["url"] = url
        if source_file:
            node["source_file"] = source_file
        _write(self.tmp, graph)
        return graph

    def test_an_edge_another_stage_added_onto_a_deployment_node_survives(self):
        """Catches the strip dropping every edge that touches a deployment node. One
        store lost 53 environment edges from its own extractor this way."""
        graph = _read(self.tmp)
        prd = _id(graph, PRD)
        inbound = {
            "source": "svc-context-progression::CaseAggregate",
            "target": prd,
            "relation": "configured_by",
        }
        outbound = {
            "source": prd,
            "target": "svc-context-progression::CaseAggregate",
            "relation": "runs",
        }
        self._hang([inbound, outbound])

        _run()

        links = _read(self.tmp)["links"]
        self.assertIn(inbound, links)
        self.assertIn(outbound, links)

    def test_the_stages_own_edges_are_rebuilt_not_carried_twice(self):
        """Catches the carry treating the stage's own `deploys`, `deployed_in` and
        `uses_chart` edges as another stage's: each re-run would then add them
        again, and the edge count would grow without bound."""
        before = sorted(json.dumps(e, sort_keys=True) for e in _read(self.tmp)["links"])

        _run()
        _run()

        after = sorted(json.dumps(e, sort_keys=True) for e in _read(self.tmp)["links"])
        self.assertEqual(after, before)

    def test_an_edge_whose_deployment_node_is_gone_goes_with_it_and_is_counted(self):
        """Catches a kept edge left dangling at a node the rebuild did not recreate,
        and the drop happening silently: it is another stage's work being lost, so
        the run says how much."""
        graph = _read(self.tmp)
        dev = _id(graph, DEV)
        dangling = {
            "source": "svc-context-progression::CaseAggregate",
            "target": dev,
            "relation": "configured_by",
        }
        self._hang([dangling])
        for path in (self.repo / "ansible" / "group_vars" / "dev").glob("*"):
            path.unlink()

        report = _run()

        graph = _read(self.tmp)
        ids = {n["id"] for n in graph["nodes"]}
        self.assertNotIn(dev, ids)
        self.assertNotIn(dangling, graph["links"])
        self.assertIn("1 edge(s) other stages added lost their deployment node", report)

    def test_a_rebuilt_node_keeps_the_url_source_urls_gave_it(self):
        """Catches the rebuild dropping `url`, which left every deployment node
        without a link until `source-urls` ran again."""
        url = "https://git.example/estate-deploy/blob/main/prd.yaml"
        self._hang([], url=url)

        _run()

        node = next(n for n in _read(self.tmp)["nodes"] if n.get("label") == PRD)
        self.assertEqual(node.get("url"), url)

    def test_a_url_for_a_file_the_node_no_longer_cites_is_not_carried(self):
        """Catches the carry restoring a link to a file the node no longer cites -
        a `url` is derived from `source_file`, so a moved file makes the old one a
        wrong citation rather than a missing one."""
        self._hang([], url="https://git.example/old.yaml", source_file="ansible/old/place.yaml")

        _run()

        node = next(n for n in _read(self.tmp)["nodes"] if n.get("label") == PRD)
        self.assertNotIn("url", node)


if __name__ == "__main__":
    unittest.main()
