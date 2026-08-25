"""The layer merge must not re-point a relationship at an unrelated entity (#129).

The documented route keeps the AST node when the two layers share an id and
concatenates the semantic layer's edges anyway. Those edges still name the
discarded id, which now resolves to the AST node — so relationships the semantic
layer asserted about one entity become assertions about another. The graph builds,
nothing dangles, and it states things the corpus never contained.

Measured on one estate: 98 colliding ids, all with disagreeing labels, all
describing different files, carrying 311 semantic edges.

The central test here is `test_a_colliding_edge_never_points_at_the_ast_node`.
Every other test in this file exists to stop that one passing for the wrong
reason — a merge that dropped the semantic layer entirely would satisfy it, and so
would one that dropped every edge.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledgestore import merge_layers  # noqa: E402


def _layer(nodes, edges=None):
    return {
        "directed": True,
        "multigraph": False,
        "graph": {},
        "nodes": nodes,
        "edges": edges or [],
    }


def _node(node_id, label, **extra):
    return {"id": node_id, "label": label, **extra}


class MergeLayersTest(unittest.TestCase):
    # --- the fabrication this stage exists to prevent ------------------------

    def test_a_colliding_edge_never_points_at_the_ast_node(self):
        """Breaks if the defect returns: a semantic edge resolving to an AST entity
        it was never about. This is the whole point of the stage."""
        ast = _layer([_node("x", "Component")], [])
        semantic = _layer(
            [_node("x", "Template"), _node("y", "Other")],
            [{"source": "x", "target": "y", "type": "renders"}],
        )

        merged, counters = merge_layers.merge(ast, semantic)

        labels = {n["id"]: n["label"] for n in merged["nodes"]}
        self.assertEqual(labels["x"], "Component", "the AST node must keep its id")
        edge = next(e for e in merged["edges"] if e["type"] == "renders")
        self.assertNotEqual(
            edge["source"], "x", "the semantic edge was re-pointed at the AST entity"
        )
        self.assertEqual(labels[edge["source"]], "Template")
        self.assertEqual(counters["collisions_different_label"], 1)

    def test_the_semantic_node_is_kept_not_discarded(self):
        """Breaks if the fix becomes "drop the edges" instead of "keep the node".

        Dropping would remove the fabrication and lose real relationships with it.
        The renamed node carries `original_id` so the collision stays auditable.
        """
        ast = _layer([_node("x", "Component")])
        semantic = _layer([_node("x", "Template")])

        merged, _counters = merge_layers.merge(ast, semantic)

        kept = [n for n in merged["nodes"] if n["label"] == "Template"]
        self.assertEqual(len(kept), 1, "the semantic node was discarded")
        self.assertEqual(kept[0]["original_id"], "x")
        self.assertNotEqual(kept[0]["id"], "x")

    def test_a_same_label_collision_is_a_duplicate_not_a_rename(self):
        """Breaks if benign duplicates are renamed too.

        Both layers finding one entity is not a collision to resolve; renaming it
        would split one thing into two and invent a distinction.
        """
        ast = _layer([_node("x", "Service")], [])
        semantic = _layer([_node("x", "Service")], [{"source": "x", "target": "x", "type": "self"}])

        merged, counters = merge_layers.merge(ast, semantic)

        self.assertEqual(counters["collisions_same_label"], 1)
        self.assertEqual(counters["collisions_different_label"], 0)
        self.assertEqual(len([n for n in merged["nodes"] if n["id"] == "x"]), 1)
        self.assertEqual(
            [e for e in merged["edges"] if e["type"] == "self"][0]["source"],
            "x",
            "a same-label edge already resolves correctly and must not move",
        )

    # --- guards against the central test passing for the wrong reason -------

    def test_non_colliding_semantic_nodes_and_edges_survive(self):
        """Breaks if the merge drops the semantic layer, which would satisfy the
        fabrication test while losing everything the layer found."""
        ast = _layer([_node("a", "A")], [{"source": "a", "target": "a", "type": "ast"}])
        semantic = _layer([_node("b", "B")], [{"source": "b", "target": "b", "type": "sem"}])

        merged, counters = merge_layers.merge(ast, semantic)

        self.assertEqual({n["id"] for n in merged["nodes"]}, {"a", "b"})
        self.assertEqual({e["type"] for e in merged["edges"]}, {"ast", "sem"})
        self.assertEqual(counters["collisions_different_label"], 0)
        self.assertEqual(counters["edges_dropped"], 0)

    def test_an_edge_with_no_endpoint_in_either_layer_is_dropped_and_counted(self):
        """Breaks if a dangling endpoint is guessed at rather than dropped.

        Guessing is how a concatenation invents relationships. Dropping silently
        would be the same defect one step on, so the count is asserted too.
        """
        ast = _layer([_node("a", "A")])
        semantic = _layer([_node("b", "B")], [{"source": "b", "target": "gone", "type": "sem"}])

        merged, counters = merge_layers.merge(ast, semantic)

        self.assertEqual(counters["edges_dropped"], 1)
        self.assertEqual(merged["edges"], [])

    def test_a_node_without_a_label_does_not_crash_the_merge(self):
        """Breaks if the merge assumes every node carries a label.

        Newer graphify emits package-hierarchy nodes with neither `label` nor
        `source_file`, and anything walking nodes has to tolerate that.
        """
        ast = _layer([{"id": "p"}])
        semantic = _layer([{"id": "p"}])

        _merged, counters = merge_layers.merge(ast, semantic)

        self.assertEqual(counters["collisions_same_label"], 1, "two absent labels are equal")

    def test_it_reads_links_as_well_as_edges(self):
        """Breaks if a layer written in node-link form silently contributes no edges.

        graphify writes `links` in node-link JSON and `edges` in its extract files,
        and both reach this stage. Reading one key and seeing zero edges is
        indistinguishable from a layer that had none.
        """
        ast = {"nodes": [_node("a", "A")], "links": [{"source": "a", "target": "a", "type": "ast"}]}
        semantic = _layer([_node("b", "B")])

        merged, _counters = merge_layers.merge(ast, semantic)

        self.assertEqual([e["type"] for e in merged["edges"]], ["ast"])

    def test_the_merge_is_deterministic(self):
        """Breaks if output depends on set or dict iteration order.

        Stage outputs are committed artefacts, so two runs on the same inputs must
        be byte-identical. Hash randomisation across processes has broken this
        before and it is invisible until someone diffs two builds.
        """
        ast = _layer([_node(f"n{i}", f"L{i}") for i in range(20)])
        semantic = _layer([_node(f"n{i}", f"Other{i}") for i in range(20)])

        first, _ = merge_layers.merge(ast, semantic)
        second, _ = merge_layers.merge(ast, semantic)

        self.assertEqual(json.dumps(first), json.dumps(second))

    def test_the_report_separates_the_two_collision_kinds(self):
        """Breaks if the counts are summed into one total.

        Same-label is a duplicate; different-label would have fused two entities.
        A single figure hides which one an estate has, and that ratio is the finding.
        """
        counters = {
            "ast_nodes": 10,
            "semantic_nodes": 5,
            "nodes": 14,
            "edges": 3,
            "collisions_same_label": 1,
            "collisions_different_label": 2,
            "edges_repointed": 3,
            "edges_dropped": 0,
        }

        text = merge_layers.report(counters)

        self.assertIn("1 id collisions with the same label", text)
        self.assertIn("2 id collisions with DIFFERENT labels", text)
        self.assertIn("would have been discarded", text)

    # --- the stage's own refusal --------------------------------------------

    def test_it_refuses_an_empty_layer(self):
        """Breaks if an upstream failure merges to a smaller graph that looks fine.

        Every stage in this library that has shipped doing nothing did so with a
        passing suite; an empty input is the shape that produces it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "ast.json").write_text(json.dumps(_layer([_node("a", "A")])))
            (directory / "sem.json").write_text(json.dumps(_layer([])))
            code = merge_layers.main(
                [
                    "--ast",
                    str(directory / "ast.json"),
                    "--semantic",
                    str(directory / "sem.json"),
                    "--out",
                    str(directory / "out.json"),
                ]
            )
        self.assertEqual(code, 1)

    def test_main_writes_the_merged_extract(self):
        """Breaks if the stage computes correctly and writes nothing, which is the
        wiring escape this repository's mutation gate has caught four times."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "ast.json").write_text(json.dumps(_layer([_node("x", "Component")])))
            (directory / "sem.json").write_text(
                json.dumps(
                    _layer(
                        [_node("x", "Template")],
                        [{"source": "x", "target": "x", "type": "renders"}],
                    )
                )
            )
            out = directory / "out.json"
            code = merge_layers.main(
                [
                    "--ast",
                    str(directory / "ast.json"),
                    "--semantic",
                    str(directory / "sem.json"),
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(code, 0)
            written = json.loads(out.read_text(encoding="utf-8"))

        labels = {n["id"]: n.get("label") for n in written["nodes"]}
        self.assertEqual(labels["x"], "Component")
        edge = written["edges"][0]
        self.assertNotEqual(edge["source"], "x")
        self.assertEqual(labels[edge["source"]], "Template")


if __name__ == "__main__":
    unittest.main()


class NamespaceByRepositoryTest(unittest.TestCase):
    """AST ids must not be shared between repositories (#115).

    graphify drops the `repositories/<repo>/` segment for declarations inside a
    file, so a Terraform variable declared in `<repo>/infrastructure/variables.tf`
    gets the same id in every repository that has one. On one estate
    `infrastructure_var_product` appeared 114 times across 114 repositories.

    The consequence is not a duplicate: a build that dedupes by id keeps one record
    and re-points every edge at it, so that node becomes adjacent to 114 unrelated
    services and is immediately the highest-degree node in the graph. Community
    detection then reports 114 independent services as one cluster, and topics,
    summaries and the explorer are all generated downstream of clusters.
    """

    def _counters(self):
        return {
            "ast_namespaced": 0,
            "ast_not_namespaced": 0,
            "ast_edges_spanning_repositories": 0,
        }

    def test_the_same_declaration_in_two_repositories_stays_two_nodes(self):
        """Breaks if the reported defect returns. This is the exact shape measured:
        one id, many repositories, fused into a false hub by any dedupe."""
        nodes = [
            _node(
                "infrastructure_var_product",
                "var.product",
                source_file="repositories/service-one/infrastructure/variables.tf",
            ),
            _node(
                "infrastructure_var_product",
                "var.product",
                source_file="repositories/service-two/infrastructure/variables.tf",
            ),
        ]
        counters = self._counters()

        renamed, _edges = merge_layers.namespace_by_repository(nodes, [], counters)

        self.assertEqual(
            {n["id"] for n in renamed},
            {"service-one::infrastructure_var_product", "service-two::infrastructure_var_product"},
        )
        self.assertEqual(counters["ast_namespaced"], 2)

    def test_without_namespacing_those_two_ids_are_identical(self):
        """The precondition, asserted so the test above cannot pass vacuously.

        If graphify ever stops sharing the id, the test above would pass whatever
        this function did. This states the defect is still reproducible.
        """
        nodes = [
            _node(
                "infrastructure_var_product",
                "var.product",
                source_file="repositories/service-one/infrastructure/variables.tf",
            ),
            _node(
                "infrastructure_var_product",
                "var.product",
                source_file="repositories/service-two/infrastructure/variables.tf",
            ),
        ]
        self.assertEqual(nodes[0]["id"], nodes[1]["id"])

    def test_a_node_with_no_source_file_is_left_alone_and_counted(self):
        """Breaks if a repository is guessed for a node that cannot be attributed.

        Newer graphify emits package-hierarchy nodes with neither label nor
        `source_file`. Attributing one to a repository would be the invention this
        function exists to prevent, so it is skipped and reported.
        """
        counters = self._counters()

        renamed, _edges = merge_layers.namespace_by_repository([_node("pkg", "pkg")], [], counters)

        self.assertEqual(renamed[0]["id"], "pkg")
        self.assertEqual(counters["ast_not_namespaced"], 1)
        self.assertEqual(counters["ast_namespaced"], 0)

    def test_an_already_namespaced_id_is_not_namespaced_twice(self):
        """Breaks if the double-namespacing hazard returns.

        Re-namespacing produces `repo::repo::id` and sets every repository
        attribute to the wrong value. This estate has already met that from running
        a merge on an already-merged graph, which is why it is guarded rather than
        assumed not to happen.
        """
        counters = self._counters()

        renamed, _edges = merge_layers.namespace_by_repository(
            [_node("repo-a::thing", "thing", source_file="repositories/repo-a/x.tf")],
            [],
            counters,
        )

        self.assertEqual(renamed[0]["id"], "repo-a::thing")
        self.assertEqual(counters["ast_not_namespaced"], 1)

    def test_an_edge_inside_one_repository_is_rewritten(self):
        """Breaks if nodes are namespaced and edges are not, which would leave every
        rewritten edge dangling — a worse artefact than the fused hub."""
        nodes = [
            _node("a", "A", source_file="repositories/one/x.tf"),
            _node("b", "B", source_file="repositories/one/y.tf"),
        ]
        counters = self._counters()

        _renamed, edges = merge_layers.namespace_by_repository(
            nodes, [{"source": "a", "target": "b"}], counters
        )

        self.assertEqual(edges[0]["source"], "one::a")
        self.assertEqual(edges[0]["target"], "one::b")

    def test_an_edge_spanning_two_repositories_is_left_alone_and_counted(self):
        """Breaks if a cross-repository AST edge is attributed to one side.

        Condition 1 of the fix — an AST edge is produced from a single file, so both
        endpoints belong to that file's repository — does not hold for such an edge.
        Attributing it would be a guess, so it is reported instead.
        """
        nodes = [
            _node("a", "A", source_file="repositories/one/x.tf"),
            _node("b", "B", source_file="repositories/two/y.tf"),
        ]
        counters = self._counters()

        _renamed, edges = merge_layers.namespace_by_repository(
            nodes, [{"source": "a", "target": "b"}], counters
        )

        self.assertEqual(edges[0]["source"], "a", "the edge must not be attributed to one side")
        self.assertEqual(counters["ast_edges_spanning_repositories"], 1)

    def test_repository_of_reads_the_segment_exactly(self):
        """Breaks if attribution becomes a guess rather than a path read."""
        self.assertEqual(
            merge_layers.repository_of("repositories/my-service/infrastructure/x.tf"), "my-service"
        )
        self.assertEqual(merge_layers.repository_of("infrastructure/x.tf"), "")
        self.assertEqual(merge_layers.repository_of(""), "")
