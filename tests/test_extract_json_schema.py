"""Tests for knowledgestore/extract_json_schema.py - the layer in the merged graph."""

from __future__ import annotations

import contextlib
import dataclasses
import gzip
import io
import json
import os
import sys
import tempfile
import time
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import json_schema_fixtures as fixtures  # noqa: E402
from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import config, extract_ast, telemetry  # noqa: E402
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


@contextlib.contextmanager
def python_default_recursion_limit():
    """Run at Python's default recursion limit, as the stage's own process does.

    graphify's extractor raises the process-wide limit to 10,000 and leaves it
    there, so a test that ran it earlier in the suite would otherwise decide how
    deep the pure-Python scanner can parse here.
    """
    before = sys.getrecursionlimit()
    sys.setrecursionlimit(1_000)
    try:
        yield
    finally:
        sys.setrecursionlimit(before)


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

    def test_the_same_file_given_twice_adds_its_nodes_once(self):
        """Breaks if two `Owned` for one (repo, path) each write the file's nodes, so
        the graph holds duplicate ids and the file is its own rival for its id."""
        once = graph_after(W, P)
        twice = graph_after(W, P, W)
        self.assertEqual(twice["nodes"], once["nodes"])
        self.assertEqual(twice["links"], once["links"])


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


class Run:
    def __init__(self, code: int, stdout: str, stderr: str):
        self.code, self.stdout, self.stderr = code, stdout, stderr


def run_capturing(main, argv: list[str]) -> Run:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return Run(code, out.getvalue(), err.getvalue())


class StoreCase(SettingsIsolated):
    """A real store on disk: clones, a content set, a graph."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Telemetry prints its movement and warnings; tests that do not read it stay quiet.
        quiet = contextlib.ExitStack()
        quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(quiet.close)
        self.root = Path(self._tmp.name)
        self.widgets = self.root / "repositories" / "widgets"
        self.common = self.root / "repositories" / "common"
        for clone in (self.widgets, self.common):
            (clone / ".git").mkdir(parents=True)
        (self.widgets / "schemas").mkdir()
        self.widget_file = self.widgets / WIDGET_FILE
        self.widget_file.write_text(fixtures.WIDGET_CREATED, encoding="utf-8")
        self.person_file = self.common / "person.json"
        self.person_file.write_text(fixtures.PERSON, encoding="utf-8")
        (self.root / "graphify-out").mkdir()
        config.configure(root=str(self.root))
        config.GRAPH_PATH.write_text(json.dumps(graph_with_keyword_nodes()), encoding="utf-8")
        self.detect: list[str] = []
        self.add_detect_entry(self.widget_file)
        self.add_detect_entry(self.person_file)

    def add_detect_entry(self, path: Path) -> None:
        self.detect.append(str(path))
        config.DETECT_PATH.write_text(
            json.dumps({"files": {"code": self.detect}}), encoding="utf-8"
        )

    def graph(self) -> dict:
        return json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))


class StageTest(StoreCase):
    """`main` against a real store on disk."""

    def test_main_writes_both_files_and_records_telemetry(self):
        """Breaks if the plain graph and the committed archive diverge, or a metric
        is recorded under another name or value (hand-counted: 8 nodes, 8 edges)."""
        self.assertEqual(layer.main([]), 0)
        plain = self.graph()
        with gzip.open(config.GRAPH_PATH.with_suffix(".json.gz"), "rt", encoding="utf-8") as h:
            self.assertEqual(json.load(h), plain)
        self.assertEqual(
            telemetry.read(),
            {
                "json_schema.files_recognised": 2,
                "json_schema.files_skipped": 0,
                "json_schema.nodes": 8,
                "json_schema.edges": 8,
                "json_schema.refs_resolved": 2,
                "json_schema.refs_unresolved": 0,
                "json_schema.refs_cross_repository": 1,
                "json_schema.graphify_nodes_removed": 3,
            },
        )

    def test_report_names_what_it_counted(self):
        """Breaks if the printed report loses a line the operator reads: recognition
        bases, nodes by kind, resolved by how, cross-repository."""
        out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 0)
        for expected in (
            "json-schema: 2 .json files in the content set across 2 repositories",
            "(meta-schema 2)",
            "(schema 2, definition 1, field 5)",
            "(contains 6, references 2)",
            "(local 1, by-id 1)",
            "cross-repository 1",
            "0 semantic nodes kept",
        ):
            self.assertIn(expected, out.stdout)

    def test_malformed_file_is_named_and_the_run_continues(self):
        """Breaks if one broken file stops the estate, or goes unnamed."""
        broken = self.widgets / "broken.json"
        broken.write_text('{"$schema": ')
        self.add_detect_entry(broken)
        out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 0)
        self.assertIn("widgets/broken.json", out.stdout)
        self.assertIn("malformed 1", out.stdout)
        self.assertEqual(telemetry.read()["json_schema.files_skipped"], 1)
        self.assertEqual(telemetry.read()["json_schema.files_recognised"], 2)

    def test_undecodable_file_is_named_and_the_run_continues(self):
        """Breaks if a non-UTF-8 file raises UnicodeDecodeError out of the stage."""
        latin = self.widgets / "latin.json"
        latin.write_bytes(b'{"$schema": "\xff"}')
        self.add_detect_entry(latin)
        out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 0)
        self.assertIn("widgets/latin.json", out.stdout)

    def test_file_over_max_bytes_is_skipped_by_name(self):
        """Breaks if the size cap is ignored or its wording hides the limit."""
        out = run_capturing(layer.main, ["--max-bytes", "10"])
        self.assertEqual(out.code, 0)
        self.assertIn("over 10 bytes", out.stdout)
        self.assertIn("widgets/schemas/widget-created.json", out.stdout)
        self.assertIn("common/person.json", out.stdout)
        self.assertEqual(telemetry.read()["json_schema.files_recognised"], 0)

    def test_default_size_is_worded_in_mebibytes(self):
        """Breaks if a whole-MiB limit is printed as a raw byte count."""
        self.assertEqual(layer._size_wording(8 * 1024 * 1024), "over 8 MiB")
        self.assertEqual(layer._size_wording(10), "over 10 bytes")

    def test_input_from_graphify_out_is_refused(self):
        """Breaks if the pipeline's own JSON can be read back as a schema."""
        self.add_detect_entry(self.root / "repositories/widgets/graphify-out/graph.json")
        before = config.GRAPH_PATH.read_bytes()
        self.assertEqual(layer.main([]), 1)
        self.assertEqual(config.GRAPH_PATH.read_bytes(), before)

    def test_unresolved_list_is_empty_for_a_fully_resolved_estate(self):
        """Breaks if the flag writes something other than a JSON list for a clean estate."""
        out_path = self.root / "unresolved.json"
        layer.main(["--unresolved", str(out_path)])
        self.assertEqual(json.loads(out_path.read_text()), [])

    def test_unresolved_list_is_written_sorted(self):
        """Breaks if the list is written in discovery order: zeta is met before alpha."""
        externals = self.widgets / "externals.json"
        externals.write_text(TWO_EXTERNALS, encoding="utf-8")
        self.add_detect_entry(externals)
        out_path = self.root / "unresolved.json"
        layer.main(["--unresolved", str(out_path)])
        text = out_path.read_text()
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(
            json.loads(text),
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
            ],
        )

    def test_unresolved_references_are_printed_one_per_line(self):
        """Breaks if the report names the count of unresolved references but not which."""
        externals = self.widgets / "externals.json"
        externals.write_text(TWO_EXTERNALS, encoding="utf-8")
        self.add_detect_entry(externals)
        out = run_capturing(layer.main, [])
        lines = [line.strip() for line in out.stdout.splitlines()]
        alpha = "widgets:externals.json #/properties/alpha http://elsewhere.example/alpha.json"
        zeta = "widgets:externals.json #/properties/zeta http://elsewhere.example/zeta.json"
        self.assertEqual(
            lines.index(alpha + " -> external") + 1, lines.index(zeta + " -> external")
        )
        self.assertIn("meta-schema 0, external 2", out.stdout)

    def test_graph_report_note_is_replaced_not_appended(self):
        """Breaks if a re-run turns GRAPH_REPORT.md into a changelog."""
        config.GRAPH_REPORT_PATH.write_text("# Report\n")
        layer.main([])
        layer.main([])
        text = config.GRAPH_REPORT_PATH.read_text()
        self.assertEqual(text.count(layer.REPORT_NOTE_MARKER), 1)
        self.assertTrue(text.startswith("# Report\n"))

    def test_no_graph_report_is_not_created(self):
        """Breaks if the stage invents a GRAPH_REPORT.md graphify never wrote."""
        layer.main([])
        self.assertFalse(config.GRAPH_REPORT_PATH.exists())

    def test_no_graph_exits_1(self):
        """Breaks if the stage runs, or creates a graph, with nothing to add to, or
        says so on stdout where the other refusals use stderr."""
        config.GRAPH_PATH.unlink()
        out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 1)
        self.assertIn("Graph not found", out.stderr)
        self.assertNotIn("Graph not found", out.stdout)
        self.assertFalse(config.GRAPH_PATH.exists())

    def test_a_refused_unresolved_target_writes_nothing(self):
        """Breaks if `--unresolved` is validated only after the graph is rewritten:
        the run then fails with the graph changed and no report printed."""
        before = config.GRAPH_PATH.read_bytes()
        target = self.root / "out" / ".." / "unresolved.json"
        out = run_capturing(layer.main, ["--unresolved", str(target)])
        self.assertEqual(out.code, 2)
        self.assertIn("Not writing --unresolved", out.stderr)
        self.assertEqual(config.GRAPH_PATH.read_bytes(), before)
        self.assertFalse(config.GRAPH_PATH.with_suffix(".json.gz").exists())
        self.assertFalse((self.root / "unresolved.json").exists())

    def test_no_content_set_exits_1(self):
        """Breaks if a missing content set is read as an empty estate and the layer wiped."""
        config.DETECT_PATH.unlink()
        before = config.GRAPH_PATH.read_bytes()
        out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 1)
        self.assertIn("Content set not found", out.stderr)
        self.assertEqual(config.GRAPH_PATH.read_bytes(), before)

    def test_content_set_without_json_removes_the_layer_and_exits_0(self):
        """Breaks if an estate with no schemas leaves the previous run's layer in the graph."""
        layer.main([])
        self.assertTrue(any(n.get("_origin") == "json-schema" for n in self.graph()["nodes"]))
        config.DETECT_PATH.write_text(
            json.dumps({"files": {"code": [str(self.widgets / "app.py")]}}), encoding="utf-8"
        )
        self.assertEqual(layer.main([]), 0)
        self.assertFalse(any(n.get("_origin") == "json-schema" for n in self.graph()["nodes"]))
        with gzip.open(config.GRAPH_PATH.with_suffix(".json.gz"), "rt", encoding="utf-8") as h:
            self.assertEqual(json.load(h), self.graph())

    def test_file_outside_repositories_is_named_and_skipped(self):
        """Breaks if a file with no repository to namespace under is read or dropped unnamed."""
        stray = self.root / "stray.json"
        stray.write_text(fixtures.PERSON, encoding="utf-8")
        self.add_detect_entry(stray)
        out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 0)
        self.assertIn("outside-repositories 1", out.stdout)
        self.assertIn(str(stray), out.stdout)
        self.assertEqual(telemetry.read()["json_schema.files_recognised"], 2)

    def test_data_json_is_counted_but_not_named_or_skipped(self):
        """Breaks if data JSON is skipped (hiding it from graphify) or named as a failure."""
        data = self.widgets / "data.json"
        data.write_text('{"colour": "red"}', encoding="utf-8")
        self.add_detect_entry(data)
        out = run_capturing(layer.main, [])
        self.assertRegex(out.stdout, r"not a schema +1 +\(left to graphify\)")
        self.assertNotIn("widgets/data.json", out.stdout)
        self.assertEqual(telemetry.read()["json_schema.files_skipped"], 0)

    def test_too_deep_file_is_kept_and_named_as_truncated(self):
        """Breaks if a depth-truncated walk drops the file from the layer or hides it."""
        depth = json_schema.MAX_DEPTH + 4
        inner = '{"type": "string"}'
        for _ in range(depth):
            inner = '{"properties": {"a": ' + inner + "}}"
        text = '{"$schema": "http://json-schema.org/draft-04/schema#", "properties": {"a": '
        text += inner + "}}"
        deep = self.widgets / "deep.json"
        deep.write_text(text, encoding="utf-8")
        self.add_detect_entry(deep)
        out = run_capturing(layer.main, [])
        self.assertIn("widgets/deep.json", out.stdout)
        self.assertIn("truncated", out.stdout)
        self.assertEqual(telemetry.read()["json_schema.files_recognised"], 3)
        self.assertEqual(telemetry.read()["json_schema.files_skipped"], 0)

    def owner_edges(self) -> list[tuple[str, str]]:
        return [
            (e["source"], e["target"])
            for e in self.graph()["links"]
            if e.get("context") == "properties/owner"
        ]

    def add_fork_copy(self) -> None:
        """A second repository declaring the person id, the shape a fork leaves."""
        fork = self.root / "repositories" / "widgets-fork"
        (fork / ".git").mkdir(parents=True)
        copy = fork / "person.json"
        copy.write_text(fixtures.PERSON, encoding="utf-8")
        self.add_detect_entry(copy)

    def test_declared_fork_settles_an_id_two_repositories_declare(self):
        """Breaks if the stage ignores KSB_FORK_REPOS: the owner reference stays
        ambiguous and writes no edge although the copy is declared a fork."""
        self.add_fork_copy()
        out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 0)
        self.assertIn("-> ambiguous", out.stdout)
        self.assertEqual(telemetry.read()["json_schema.refs_unresolved"], 1)
        self.assertEqual(self.owner_edges(), [])

        config.configure(FORK_REPOS={"widgets-fork"})
        out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 0)
        self.assertNotIn("ambiguous", out.stdout)
        self.assertEqual(telemetry.read()["json_schema.refs_unresolved"], 0)
        self.assertEqual(
            self.owner_edges(),
            [
                (
                    "widgets::jsonschema:schemas/widget-created.json#/properties/owner",
                    "common::jsonschema:person.json#",
                )
            ],
        )

    def test_two_runs_are_byte_identical(self):
        """Breaks if output depends on run order, hash seed, or a gzip timestamp."""
        layer.main([])
        first = (
            config.GRAPH_PATH.read_bytes(),
            config.GRAPH_PATH.with_suffix(".json.gz").read_bytes(),
        )
        layer.main([])
        second = (
            config.GRAPH_PATH.read_bytes(),
            config.GRAPH_PATH.with_suffix(".json.gz").read_bytes(),
        )
        self.assertEqual(first, second)

    def test_self_references_are_reported_beside_the_resolved_count(self):
        """Breaks if a self-reference is counted but never printed, so resolved plus
        unresolved falls short of the references in the files by an unexplained gap.

        Hand-counted: widget-created has 2 resolved refs (owner by-id, parts local);
        self.json has 3, `next` and `head` local and the root's `#` a self-reference.
        """
        mine = self.widgets / "self.json"
        mine.write_text(SELF_REFERENCE, encoding="utf-8")
        self.add_detect_entry(mine)
        out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 0)
        self.assertRegex(
            out.stdout,
            r"\$ref resolved +4 +\(local 3, by-id 1\); self-references 1 \(no edge\)\n",
        )
        self.assertRegex(out.stdout, r"\$ref unresolved +0 ")

    def test_a_large_data_file_is_left_to_graphify_without_a_line_parse(self):
        """Breaks if every `.json` is parsed with line tracking before recognition.

        Two observable outcomes. Nesting 2,000 deep is within the C parser's limit
        and past the pure-Python scanner's, so a data file that deep is counted
        `not-a-schema` only if it never reached the line-tracking parse; otherwise
        it is named `malformed`. And a 5 MB lock-file-shaped data file, which took
        54 s when every object's line was found by a rescan, is counted well inside
        a generous bound."""
        deep = self.widgets / "deep-data.json"
        deep.write_text('{"a": ' * 2_000 + "1" + "}" * 2_000, encoding="utf-8")
        self.add_detect_entry(deep)
        with python_default_recursion_limit():
            out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 0)
        self.assertRegex(out.stdout, r"not a schema +1 +\(left to graphify\)")
        self.assertNotIn("malformed", out.stdout)

        packages = {
            f"pkg{i}": {"version": "1.0.0", "resolved": "https://registry.example/x.tgz",
                        "integrity": "sha512-" + "a" * 80, "requires": {"b": "1"}}
            for i in range(20_000)
        }  # fmt: skip
        lock = self.widgets / "package-lock.json"
        lock.write_text(json.dumps({"name": "x", "packages": packages}, indent=2))
        self.assertGreater(lock.stat().st_size, 5_000_000)
        groups = extract_ast.by_repository([lock], config.REPOSITORIES_DIR)
        started = time.monotonic()
        owned, info = layer.read_owned(groups, config.REPOSITORIES_DIR, layer.DEFAULT_MAX_BYTES)
        elapsed = time.monotonic() - started
        self.assertEqual((owned, info["not-a-schema"]["count"]), ([], 1))
        self.assertLess(elapsed, 5.0)

    def test_nesting_past_the_recursion_limit_is_named_malformed(self):
        """Breaks if a schema nested past the line-tracking parse's recursion limit
        raises RecursionError out of the stage instead of being named."""
        deep = self.widgets / "deep-schema.json"
        deep.write_text(
            '{"$schema": "http://json-schema.org/draft-04/schema#", "default": '
            + '{"a": ' * 2_000
            + "1"
            + "}" * 2_000
            + "}",
            encoding="utf-8",
        )
        self.add_detect_entry(deep)
        with python_default_recursion_limit():
            out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 0)
        self.assertIn("malformed 1", out.stdout)
        self.assertIn("  widgets/deep-schema.json", out.stdout)
        self.assertEqual(telemetry.read()["json_schema.files_recognised"], 2)

    def test_an_integer_past_the_digit_limit_is_named_malformed(self):
        """Breaks if an integer over 4,300 digits raises ValueError out of the stage."""
        huge = self.widgets / "huge.json"
        huge.write_text(
            '{"$schema": "http://json-schema.org/draft-04/schema#", "maximum": '
            + "9" * 5_000
            + "}",
            encoding="utf-8",
        )
        self.add_detect_entry(huge)
        out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 0)
        self.assertIn("malformed 1", out.stdout)
        self.assertIn("  widgets/huge.json", out.stdout)


V2 = (
    '{"$schema": "http://json-schema.org/draft-04/schema#",'
    ' "id": "http://schemas.example/widgets/v2.json",'
    ' "properties": {"colour": {"type": "string"}}}'
)

USES_V2 = (
    '{"$schema": "http://json-schema.org/draft-04/schema#",'
    ' "id": "http://schemas.example/widgets/uses.json",'
    ' "properties": {"paint": {"$ref": "http://schemas.example/widgets/v2.json"}}}'
)


class SymlinkTest(StoreCase):
    """A content-set path that is a symlink, read through a real link in the clone."""

    def link(self, link: Path, target: str) -> None:
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as error:  # pragma: no cover - platform
            self.skipTest(f"this platform cannot create symlinks: {error}")

    def write_graph(self, *files: str) -> None:
        """graphify's keyword nodes on each named widgets file, as it leaves them."""
        nodes = [
            {"id": f"widgets::{f}_kw", "label": "properties", "source_file": f,
             "repo": "widgets", "_origin": "ast"}
            for f in files
        ]  # fmt: skip
        graph = {"directed": False, "multigraph": False, "graph": {}, "nodes": nodes, "links": []}
        config.GRAPH_PATH.write_text(json.dumps(graph), encoding="utf-8")

    def layer_nodes(self) -> list[dict]:
        return [n for n in self.graph()["nodes"] if n.get("_origin") == "json-schema"]

    def test_a_link_to_a_schema_in_the_same_repository_is_owned_by_the_real_file(self):
        """Breaks if a link is read under its target's path: the target's nodes are
        written twice, a reference to its id is a false `ambiguous` between the file
        and itself, and graphify's keyword nodes on the link path survive."""
        real = self.widgets / "schemas" / "v2.json"
        real.write_text(V2, encoding="utf-8")
        latest = self.widgets / "schemas" / "latest.json"
        self.link(latest, "v2.json")
        uses = self.widgets / "uses.json"
        uses.write_text(USES_V2, encoding="utf-8")
        for path in (latest, real, uses):
            self.add_detect_entry(path)
        self.write_graph("schemas/latest.json", "schemas/v2.json", "app.py")

        out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 0)
        ids = [n["id"] for n in self.layer_nodes()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            sorted(n["id"] for n in self.layer_nodes() if n["source_file"].startswith("schemas/v")),
            [
                "widgets::jsonschema:schemas/v2.json#",
                "widgets::jsonschema:schemas/v2.json#/properties/colour",
            ],
        )
        self.assertFalse([n for n in self.layer_nodes() if "latest" in n["source_file"]])
        self.assertEqual(
            [n["id"] for n in self.graph()["nodes"] if n.get("_origin") == "ast"],
            ["widgets::app.py_kw"],
        )
        self.assertNotIn("ambiguous", out.stdout)
        self.assertIn(
            (
                "widgets::jsonschema:uses.json#/properties/paint",
                "widgets::jsonschema:schemas/v2.json#",
            ),
            [(e["source"], e["target"]) for e in self.graph()["links"]],
        )
        self.assertEqual(telemetry.read()["json_schema.files_recognised"], 4)
        self.assertRegex(out.stdout, r"symlinked to an owned file +1 ")

    def test_a_link_to_another_repository_is_read_under_its_own_path(self):
        """Breaks if a link into another clone is keyed by the target's repository
        and path, which writes that file's node ids a second time, rather than by
        the path graphify gives it in the repository that holds the link."""
        shared = self.widgets / "shared-person.json"
        self.link(shared, "../common/person.json")
        self.add_detect_entry(shared)
        self.write_graph("shared-person.json")

        self.assertEqual(run_capturing(layer.main, []).code, 0)
        ids = [n["id"] for n in self.layer_nodes()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("widgets::jsonschema:shared-person.json#", ids)
        self.assertIn("common::jsonschema:person.json#", ids)
        self.assertEqual([n for n in self.graph()["nodes"] if n.get("_origin") == "ast"], [])

    def test_a_link_out_of_the_store_is_named_and_never_read(self):
        """Breaks if a link whose target lies outside the store is read: its content
        would enter the graph under a repository that does not hold it."""
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / "secret.json"
            outside.write_text(V2, encoding="utf-8")
            escape = self.widgets / "escape.json"
            self.link(escape, str(outside))
            self.add_detect_entry(escape)
            out = run_capturing(layer.main, [])
        self.assertEqual(out.code, 0)
        self.assertIn("outside-repositories 1", out.stdout)
        self.assertIn(str(escape), out.stdout)
        self.assertEqual(telemetry.read()["json_schema.files_recognised"], 2)
        self.assertFalse([n for n in self.layer_nodes() if "v2.json" in json.dumps(n)])
