"""Tests for knowledgestore/extract_json_schema.py - the layer in the merged graph."""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import json_schema_fixtures as fixtures  # noqa: E402
from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import extract_json_schema as layer  # noqa: E402
from knowledgestore import json_schema, json_schema_refs as refs  # noqa: E402

NO_FORKS: frozenset[str] = frozenset()
WIDGET_FILE = "schemas/widget-created.json"

SELF_REFERENCE = (
    '{"$schema": "http://json-schema.org/draft-04/schema#",'
    ' "id": "http://schemas.example/widgets/self.json",'
    ' "definitions": {"Node": {"type": "object",'
    ' "properties": {"next": {"$ref": "#/definitions/Node"}}}},'
    ' "$ref": "#", "properties": {"head": {"$ref": "#/definitions/Node"}}}'
)

TWO_EXTERNALS = (
    '{"$schema": "http://json-schema.org/draft-04/schema#",'
    ' "id": "http://schemas.example/widgets/externals.json",'
    ' "properties": {"zeta": {"$ref": "http://elsewhere.example/zeta.json"},'
    ' "alpha": {"$ref": "http://elsewhere.example/alpha.json"}}}'
)

DATE_FIELD = (
    '{"$schema": "http://json-schema.org/draft-04/schema#",'
    ' "id": "http://schemas.example/widgets/dated.json",'
    ' "properties": {"at": {"type": "string", "format": "date-time"}}}'
)


def make_owned(repo: str, rel: str, text: str) -> refs.Owned:
    document, lines = json_schema.load_with_lines(text)
    basis = json_schema.recognise(document)
    assert basis is not None
    return refs.Owned(repo, rel, basis, json_schema.walk(document, lines, rel))


W = make_owned("widgets", WIDGET_FILE, fixtures.WIDGET_CREATED)
P = make_owned("common", "person.json", fixtures.PERSON)
P_FORK = make_owned("widgets-fork", "person.json", fixtures.PERSON)


def graph_with_keyword_nodes(
    source_file: str = WIDGET_FILE, repo: str | None = "widgets", extra_semantic: bool = False
) -> dict:
    """A merged graph as graphify leaves it: keyword nodes for the schema and for other files."""

    def node(local_id: str, file: str, origin: str | None = "ast") -> dict:
        record: dict = {"id": f"widgets::{local_id}", "label": local_id, "source_file": file}
        if origin is not None:
            record["_origin"] = origin
        if repo is not None:
            record["repo"] = repo
        return record

    def link(source: str, target: str, file: str) -> dict:
        return {
            "source": f"widgets::{source}",
            "target": f"widgets::{target}",
            "relation": "contains",
            "source_file": file,
        }

    nodes = [
        node("widget_created", source_file),
        node("widget_created_properties", source_file),
        node("widget_created_properties_widgetid", source_file),
        node("app_run", "app.py"),
        node("package_dependencies", "package.json"),
    ]
    if extra_semantic:
        nodes.append(node("widget_rationale", source_file, origin=None))
    return {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": nodes,
        "links": [
            link("widget_created", "widget_created_properties", source_file),
            link("widget_created_properties", "widget_created_properties_widgetid", source_file),
        ],
    }


def graph_after(*owned: refs.Owned) -> dict:
    graph = graph_with_keyword_nodes()
    layer.add_layer(graph, list(owned), NO_FORKS)
    return graph


def layer_links(graph: dict) -> list[dict]:
    return [e for e in graph["links"] if "jsonschema:" in e["source"]]


class AddLayerTest(SettingsIsolated):
    def test_exact_nodes_and_edges_for_two_repositories(self):
        """Breaks if an id, a field, or the cross-repository edge changes shape."""
        graph = graph_with_keyword_nodes()
        report = layer.add_layer(graph, [W, P], NO_FORKS)
        ours = sorted(n["id"] for n in graph["nodes"] if n.get("_origin") == "json-schema")
        self.assertEqual(
            ours,
            sorted(
                [
                    "common::jsonschema:person.json#",
                    "common::jsonschema:person.json#/properties/name",
                    "widgets::jsonschema:schemas/widget-created.json#",
                    "widgets::jsonschema:schemas/widget-created.json#/definitions/Part",
                    "widgets::jsonschema:schemas/widget-created.json#/definitions/Part/properties/sku",
                    "widgets::jsonschema:schemas/widget-created.json#/properties/owner",
                    "widgets::jsonschema:schemas/widget-created.json#/properties/parts",
                    "widgets::jsonschema:schemas/widget-created.json#/properties/widgetId",
                ]
            ),
        )
        self.assertIn(
            {
                "source": "widgets::jsonschema:schemas/widget-created.json#/properties/owner",
                "target": "common::jsonschema:person.json#",
                "relation": "references",
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_file": "schemas/widget-created.json",
                "source_location": "L8",
                "weight": 1.0,
                "context": "properties/owner",
            },
            graph["links"],
        )
        self.assertEqual(report.cross_repository, 1)
        self.assertEqual(report.edges_by_relation, {"contains": 6, "references": 2})

    def test_node_record_shape(self):
        """Breaks if a node field, the metadata key order or a value changes."""
        node = next(
            n for n in graph_after(W, P)["nodes"] if n["id"].endswith("#/properties/widgetId")
        )
        self.assertEqual(
            node,
            {
                "id": "widgets::jsonschema:schemas/widget-created.json#/properties/widgetId",
                "label": "widgetId",
                "norm_label": "widgetid",
                "file_type": "code",
                "source_file": "schemas/widget-created.json",
                "source_location": "L7",
                "repo": "widgets",
                "_origin": "json-schema",
                "local_id": "jsonschema:schemas/widget-created.json#/properties/widgetId",
                "metadata": {
                    "kind": "field",
                    "format": "json-schema",
                    "pointer": "/properties/widgetId",
                    "type": "string",
                    "required": True,
                    "description": "The widget.",
                },
            },
        )
        self.assertEqual(
            list(node["metadata"]),
            ["kind", "format", "pointer", "description", "type", "required"],
        )

    def test_schema_node_carries_its_basis_last(self):
        """Breaks if the root loses its recognition basis or it moves ahead of other keys."""
        root = next(n for n in graph_after(W)["nodes"] if n["id"].endswith("widget-created.json#"))
        self.assertEqual(root["metadata"]["basis"], "meta-schema")
        self.assertEqual(list(root["metadata"])[-1], "basis")

    def test_a_format_keyword_does_not_replace_the_stage_format(self):
        """Breaks if a property's `format: date-time` overwrites `metadata.format`."""
        graph = graph_with_keyword_nodes()
        layer.add_layer(graph, [make_owned("widgets", "dated.json", DATE_FIELD)], NO_FORKS)
        node = next(n for n in graph["nodes"] if n["id"].endswith("#/properties/at"))
        self.assertEqual(node["metadata"]["format"], "json-schema")
        self.assertEqual(node["metadata"]["schema_format"], "date-time")

    def test_keyword_nodes_of_owned_files_are_removed_with_their_edges(self):
        """Breaks if graphify's scaffolding survives beside the layer."""
        graph = graph_with_keyword_nodes()
        report = layer.add_layer(graph, [W, P], NO_FORKS)
        ast_left = [
            n
            for n in graph["nodes"]
            if n.get("_origin") == "ast" and n.get("source_file") == WIDGET_FILE
        ]
        self.assertEqual(ast_left, [])
        self.assertEqual(report.graphify_removed, (3, 2, 0))  # hand-count of the literal
        self.assertIn("widgets::app_run", {n["id"] for n in graph["nodes"]})
        self.assertEqual([e for e in graph["links"] if "jsonschema:" not in e["source"]], [])

    def test_removes_keyword_nodes_in_the_store_relative_form(self):
        """Breaks if an extract-ast graph (no `repo`, `repositories/<name>/...`) keeps its nodes."""
        graph = graph_with_keyword_nodes(
            source_file="repositories/widgets/schemas/widget-created.json", repo=None
        )
        layer.add_layer(graph, [W], NO_FORKS)
        self.assertFalse(
            [
                n
                for n in graph["nodes"]
                if n.get("_origin") == "ast" and "widget-created" in n.get("source_file", "")
            ]
        )

    def test_semantic_nodes_on_owned_files_are_kept_and_counted(self):
        """Breaks if the semantic pass's node on a schema file is deleted with the scaffolding."""
        graph = graph_with_keyword_nodes(extra_semantic=True)
        report = layer.add_layer(graph, [W], NO_FORKS)
        self.assertEqual(report.graphify_removed[2], 1)
        self.assertIn("widgets::widget_rationale", {n["id"] for n in graph["nodes"]})

    def test_unrecognised_json_keeps_its_graphify_nodes(self):
        """Breaks if removal reaches a JSON file the layer does not own."""
        graph = graph_with_keyword_nodes()  # also holds package.json nodes
        layer.add_layer(graph, [W], NO_FORKS)
        self.assertIn("widgets::package_dependencies", {n["id"] for n in graph["nodes"]})

    def test_rerun_is_byte_identical(self):
        """Breaks if the layer is appended twice or edge order depends on the run."""
        graph = graph_with_keyword_nodes()
        layer.add_layer(graph, [W, P], NO_FORKS)
        first = json.dumps(graph)
        layer.add_layer(graph, [W, P], NO_FORKS)
        self.assertEqual(json.dumps(graph), first)

    def test_input_order_does_not_change_the_bytes(self):
        """Breaks if output order follows the order the content set listed the files."""
        a, b = graph_with_keyword_nodes(), graph_with_keyword_nodes()
        layer.add_layer(a, [W, P], NO_FORKS)
        layer.add_layer(b, [P, W], NO_FORKS)
        self.assertEqual(json.dumps(a), json.dumps(b))

    def test_layer_edges_are_written_in_key_order(self):
        """Breaks if edges are written in discovery order, not (source, target, relation, context)."""
        graph = graph_after(W, P)
        root = "widgets::jsonschema:schemas/widget-created.json#"
        found = [
            (e["source"], e["target"], e["relation"], e.get("context")) for e in layer_links(graph)
        ]
        self.assertEqual(
            found,
            [
                (
                    "common::jsonschema:person.json#",
                    "common::jsonschema:person.json#/properties/name",
                    "contains",
                    None,
                ),
                (root, root + "/definitions/Part", "contains", None),
                (root, root + "/properties/owner", "contains", None),
                (root, root + "/properties/parts", "contains", None),
                (root, root + "/properties/widgetId", "contains", None),
                (
                    root + "/definitions/Part",
                    root + "/definitions/Part/properties/sku",
                    "contains",
                    None,
                ),
                (
                    root + "/properties/owner",
                    "common::jsonschema:person.json#",
                    "references",
                    "properties/owner",
                ),
                (
                    root + "/properties/parts",
                    root + "/definitions/Part",
                    "references",
                    "properties/parts/items",
                ),
            ],
        )

    def test_rerun_after_file_removed_leaves_no_nodes_for_it(self):
        """Breaks if a deleted schema's nodes survive the next run."""
        graph = graph_with_keyword_nodes()
        layer.add_layer(graph, [W, P], NO_FORKS)
        layer.add_layer(graph, [W], NO_FORKS)
        self.assertFalse([n for n in graph["nodes"] if n["id"].startswith("common::jsonschema:")])
        self.assertFalse([e for e in graph["links"] if "common::jsonschema:" in e["target"]])

    def test_ambiguous_ref_writes_no_edge_and_is_listed(self):
        """Breaks if a tied id is resolved by guessing, or is dropped without being listed."""
        graph = graph_with_keyword_nodes()
        report = layer.add_layer(graph, [W, P, P_FORK], NO_FORKS)
        self.assertEqual(report.unresolved[0]["reason"], "ambiguous")
        self.assertEqual(
            report.unresolved[0]["candidates"], ["common:person.json", "widgets-fork:person.json"]
        )
        self.assertFalse([e for e in graph["links"] if e.get("context") == "properties/owner"])

    def test_self_reference_is_counted_and_writes_no_edge(self):
        """Breaks if `#` or a node's own pointer becomes a loop edge, or goes uncounted."""
        graph = graph_with_keyword_nodes()
        item = make_owned("widgets", "self.json", SELF_REFERENCE)
        report = layer.add_layer(graph, [item], NO_FORKS)
        root = "widgets::jsonschema:self.json#"
        self.assertEqual(report.self_references, 1)
        self.assertEqual(
            [(e["source"], e["target"]) for e in graph["links"] if e["relation"] == "references"],
            [
                (root + "/definitions/Node/properties/next", root + "/definitions/Node"),
                (root + "/properties/head", root + "/definitions/Node"),
            ],
        )


class OwnedKeyTest(unittest.TestCase):
    def test_repository_relative_path_that_looks_store_relative_stays_in_its_repository(self):
        """Breaks if `repositories/foo/bar.json` inside repo `widgets` is read as repo `foo`."""
        node = {"id": "n", "repo": "widgets", "source_file": "repositories/foo/bar.json"}
        self.assertEqual(layer.owned_key(node), ("widgets", "repositories/foo/bar.json"))

    def test_store_relative_path_naming_the_nodes_own_repository_is_unwrapped(self):
        """Breaks if a store-relative path on a node that also carries `repo` keeps its prefix."""
        node = {"id": "n", "repo": "widgets", "source_file": "repositories/widgets/a/b.json"}
        self.assertEqual(layer.owned_key(node), ("widgets", "a/b.json"))

    def test_the_wrong_repository_is_not_touched_by_a_look_alike_path(self):
        """Breaks if owning (`foo`, `bar.json`) deletes a node of repo `widgets`, or misses its own."""
        graph = {
            "nodes": [
                {
                    "id": "widgets::x",
                    "repo": "widgets",
                    "_origin": "ast",
                    "source_file": "repositories/foo/bar.json",
                }
            ],
            "links": [],
        }
        self.assertEqual(layer.remove_graphify_nodes(graph, {("foo", "bar.json")}), (0, 0, 0))
        self.assertEqual(
            layer.remove_graphify_nodes(graph, {("widgets", "repositories/foo/bar.json")}),
            (1, 0, 0),
        )


class AddLayerAccountingTest(SettingsIsolated):
    def test_a_repeated_reference_from_one_owner_writes_one_edge(self):
        """Breaks if the same (source, target, relation, context) is written twice."""
        item = make_owned("widgets", WIDGET_FILE, fixtures.WIDGET_CREATED)
        first = item.walked.refs[0]
        item.walked.refs.append(dataclasses.replace(first, line=first.line + 40))
        graph = graph_with_keyword_nodes()
        layer.add_layer(graph, [item, P], NO_FORKS)
        owner_edges = [e for e in graph["links"] if e.get("context") == "properties/owner"]
        self.assertEqual(len(owner_edges), 1)
        self.assertEqual(owner_edges[0]["source_location"], "L8")

    def test_a_landing_outside_the_layer_is_unresolved_and_writes_no_edge(self):
        """Breaks if a resolution onto an id with no node becomes a dangling edge.

        Unreachable through real inputs, since the index and the nodes both come from
        the same walk, so the resolver is the seam that is replaced.
        """
        stray = refs.Resolution(refs.Target("widgets", "gone.json", "", ""), "by-id")
        graph = graph_with_keyword_nodes()
        with unittest.mock.patch.object(layer, "resolve", lambda *_a: stray):
            report = layer.add_layer(graph, [W], NO_FORKS)
        self.assertEqual(
            [(u["pointer"], u["reason"]) for u in report.unresolved],
            [("/properties/owner", "no-such-pointer"), ("/properties/parts", "no-such-pointer")],
        )
        self.assertEqual([e for e in graph["links"] if e["relation"] == "references"], [])

    def test_unresolved_are_listed_in_repo_file_pointer_order(self):
        """Breaks if the report lists unresolved references in discovery order."""
        graph = graph_with_keyword_nodes()
        report = layer.add_layer(
            graph,
            [make_owned("widgets", "externals.json", TWO_EXTERNALS), W],
            NO_FORKS,
        )
        self.assertEqual(
            report.unresolved,
            [
                {
                    "repo": "widgets",
                    "source_file": "externals.json",
                    "pointer": "/properties/alpha",
                    "ref": "http://elsewhere.example/alpha.json",
                    "reason": "external",
                    "candidates": [],
                },
                {
                    "repo": "widgets",
                    "source_file": "externals.json",
                    "pointer": "/properties/zeta",
                    "ref": "http://elsewhere.example/zeta.json",
                    "reason": "external",
                    "candidates": [],
                },
                {
                    "repo": "widgets",
                    "source_file": "schemas/widget-created.json",
                    "pointer": "/properties/owner",
                    "ref": "http://schemas.example/common/person.json",
                    "reason": "external",
                    "candidates": [],
                },
            ],
        )

    def test_resolved_references_are_counted_by_how(self):
        """Breaks if the report miscounts how references resolved (one local, one by-id)."""
        report = layer.add_layer(graph_with_keyword_nodes(), [W, P], NO_FORKS)
        self.assertEqual(report.resolved_by_how, {"local": 1, "by-id": 1})
        self.assertEqual(report.unresolved, [])

    def test_hyperedge_members_removed_with_their_nodes_in_both_slots(self):
        """Breaks if a hyperedge keeps naming a removed node, or a memberless one survives."""
        graph = graph_with_keyword_nodes()
        he_mixed = {"id": "h1", "nodes": ["widgets::widget_created", "widgets::app_run"]}
        he_gone = {"id": "h2", "nodes": ["widgets::widget_created_properties"]}
        graph["hyperedges"] = [he_mixed, he_gone]
        graph["graph"]["hyperedges"] = [he_mixed, he_gone]
        layer.add_layer(graph, [W], NO_FORKS)
        expected = [{"id": "h1", "nodes": ["widgets::app_run"]}]
        self.assertEqual(graph["hyperedges"], expected)
        self.assertEqual(graph["graph"]["hyperedges"], expected)
