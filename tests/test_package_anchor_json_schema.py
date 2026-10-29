"""The packages stage's `provided_by` anchor against the json-schema stage, in both orders.

A package directory that also holds a schema file: the schema's graphify nodes
sort lowest, and the json-schema stage both removes them and writes ids that sort
before most graphify ids. Run through the two real stages on a real store, so the
edge is judged by what the graph holds after both have run.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import build_package_edges as packages  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import extract_json_schema as json_schema_stage  # noqa: E402

SCHEMA_FILE = "packages/lib/schemas/x.schema.json"
MANIFEST_FILE = "packages/lib/package.json"
CODE_FILE = "packages/lib/src/index.ts"

SCHEMA = """{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Thing",
  "type": "object",
  "properties": {"name": {"type": "string"}}
}
"""


def _node(node_id: str, source_file: str, repo: str = "repo-a") -> dict:
    return {
        "id": node_id,
        "label": node_id,
        "repo": repo,
        "source_file": source_file,
        "_origin": "ast",
    }


def _graph(with_manifest_node: bool) -> dict:
    # Ids as graphify writes them: the schema file's nodes sort below the others.
    nodes = [
        _node("repo-a::a_thing", SCHEMA_FILE),
        _node("repo-a::a_thing_properties", SCHEMA_FILE),
        _node("repo-a::m_index", CODE_FILE),
        _node("repo-b::app_main", "src/main.ts", "repo-b"),
    ]
    if with_manifest_node:
        nodes.append(_node("repo-a::z_package_dependencies", MANIFEST_FILE))
    return {"directed": False, "multigraph": False, "graph": {}, "nodes": nodes, "links": []}


class ProvidedByAnchorTest(SettingsIsolated):
    def store(self, with_manifest_node: bool) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        files = {
            "repo-a": {
                MANIFEST_FILE: json.dumps({"name": "@example/lib"}),
                CODE_FILE: "export const x = 1;\n",
                SCHEMA_FILE: SCHEMA,
            },
            "repo-b": {
                "package.json": json.dumps(
                    {"name": "app", "dependencies": {"@example/lib": "^1.0.0"}}
                ),
                "src/main.ts": "import { x } from '@example/lib';\n",
            },
        }
        for repo, tree in files.items():
            (root / "repositories" / repo / ".git").mkdir(parents=True)
            for rel, text in tree.items():
                path = root / "repositories" / repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        (root / "graphify-out").mkdir()
        config.configure(root=str(root))
        config.GRAPH_PATH.write_text(json.dumps(_graph(with_manifest_node)), encoding="utf-8")
        config.DETECT_PATH.write_text(
            json.dumps({"files": {"code": [str(root / "repositories" / "repo-a" / SCHEMA_FILE)]}}),
            encoding="utf-8",
        )

    def run_stages(self, order: tuple[str, ...]) -> dict:
        stages = {"packages": packages.main, "json-schema": lambda: json_schema_stage.main([])}
        for name in order:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(stages[name](), 0, name)
        return json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))

    def provided_by(self, graph: dict) -> list[dict]:
        return [e for e in graph["links"] if e.get("relation") == "provided_by"]

    def check(self, order: tuple[str, ...], with_manifest_node: bool, expected: str) -> None:
        self.store(with_manifest_node)
        graph = self.run_stages(order)
        edges = self.provided_by(graph)
        self.assertEqual(
            [(e["source"], e["target"]) for e in edges],
            [("repo-a::pkg:@example/lib", expected)],
        )
        target = next(n for n in graph["nodes"] if n["id"] == expected)
        self.assertNotEqual(target.get("_origin"), "json-schema")

    def test_the_edge_lands_on_the_manifest_node_in_both_stage_orders(self):
        """Break caught: the lowest id under the directory taken whatever file it came
        from. `packages` first, the schema's graphify node is removed by `json-schema`
        and takes the edge with it; `json-schema` first, the edge lands on a schema node."""
        for order in (("packages", "json-schema"), ("json-schema", "packages")):
            with self.subTest(order=order):
                self.check(order, True, "repo-a::z_package_dependencies")

    def test_without_a_manifest_node_the_edge_lands_on_a_code_node(self):
        """Break caught: no manifest node in the graph and the anchor falling to a
        schema file's node, rather than the lowest id among non-JSON source files."""
        for order in (("packages", "json-schema"), ("json-schema", "packages")):
            with self.subTest(order=order):
                self.check(order, False, "repo-a::m_index")

    def test_a_manifest_the_json_schema_stage_owns_is_not_the_anchor(self):
        """Break caught: the manifest's representative taken whatever owns it. A
        `package.json` declaring a json-schema.org `$schema` is read as a schema, so
        once `json-schema` has run its node in the layer is the one at that path and
        the edge would land on a node that stage rebuilds."""
        for order in (("packages", "json-schema"), ("json-schema", "packages")):
            with self.subTest(order=order):
                self.store(False)
                manifest = config.ROOT / "repositories" / "repo-a" / MANIFEST_FILE
                manifest.write_text(
                    json.dumps(
                        {
                            "$schema": "http://json-schema.org/draft-07/schema#",
                            "name": "@example/lib",
                            "properties": {"name": {"type": "string"}},
                        }
                    ),
                    encoding="utf-8",
                )
                config.DETECT_PATH.write_text(
                    json.dumps({"files": {"code": [str(manifest)]}}), encoding="utf-8"
                )
                graph = self.run_stages(order)
                self.assertTrue(
                    any(n.get("_origin") == "json-schema" for n in graph["nodes"]),
                    "the manifest was not read as a schema; the check below would be vacuous",
                )
                self.assertEqual(
                    [(e["source"], e["target"]) for e in self.provided_by(graph)],
                    [("repo-a::pkg:@example/lib", "repo-a::m_index")],
                )

    def test_the_module_consumer_anchor_is_the_repository_in_both_stage_orders(self):
        """Break caught: the per-repository `USES_MODULE` anchor being a node `json-schema`
        owns, which it removes, or any file's node, which is not the repository. The edge
        starts from the repository node whichever stage ran last."""
        for order in (("packages", "json-schema"), ("json-schema", "packages")):
            with self.subTest(order=order):
                self.store(False)
                root = config.ROOT
                consumer = root / "repositories" / "repo-b"
                (consumer / "main.tf").write_text(
                    'module "m" {\n  source = "github.com/example-org/repo-a"\n}\n',
                    encoding="utf-8",
                )
                (consumer / "schemas").mkdir()
                (consumer / "schemas" / "y.schema.json").write_text(SCHEMA, encoding="utf-8")
                graph = _graph(False)
                graph["nodes"] += [
                    _node("repo-b::a_thing", "schemas/y.schema.json", "repo-b"),
                ]
                config.GRAPH_PATH.write_text(json.dumps(graph), encoding="utf-8")
                config.DETECT_PATH.write_text(
                    json.dumps({"files": {"code": [str(consumer / "schemas" / "y.schema.json")]}}),
                    encoding="utf-8",
                )
                result = self.run_stages(order)
                uses = [e for e in result["links"] if e.get("relation") == "USES_MODULE"]
                self.assertEqual(
                    [(e["source"], e["target"]) for e in uses],
                    [("repo-b::repository", "repo-a::module")],
                )


if __name__ == "__main__":
    unittest.main()
