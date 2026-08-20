"""Merging chunk extractions: what must fuse, what must not, and what must be recorded.

The two directions this guards are opposite and both silent. Concatenation fuses
unrelated entities that collided on a slug, re-pointing every edge at whichever won
- fabricating relationships indistinguishable from extracted ones. And it leaves a
genuinely shared entity scattered across one id per chunk that saw it, losing the
cross-file linking the layer exists to produce.

`AmbiguousEndpoints` covers a case the reporting estate never exercised: it recorded
0 endpoints dropped as ambiguous, which is a rule with no evidence rather than a
rule that works, so the ambiguous case is constructed here deliberately.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import config, merge_chunks


def node(nid: str, label: str, source: str, kind: str = "document") -> dict:
    return {"id": nid, "label": label, "file_type": kind, "source_file": source}


class TheThreeTreatments(unittest.TestCase):
    def test_same_id_and_label_is_one_entity_with_sources_unioned(self):
        """The cross-file linking the layer exists for. Namespacing this destroys it."""
        chunks = [
            ("c1", {"nodes": [node("registry", "shared registry name", "a/one.yaml")]}),
            ("c2", {"nodes": [node("registry", "shared registry name", "b/two.yaml")]}),
        ]
        nodes, remap, counters = merge_chunks.merge_nodes(chunks)
        self.assertEqual(counters["merged"], 1)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes["registry"]["source_files"], ["a/one.yaml", "b/two.yaml"])
        self.assertEqual(remap[("c1", "registry")], remap[("c2", "registry")])

    def test_same_id_different_label_is_kept_apart(self):
        """Merging these asserts a relationship that was never in the corpus."""
        chunks = [
            ("c1", {"nodes": [node("sops_key", "SOPS age key", "a/one.yaml")]}),
            ("c2", {"nodes": [node("sops_key", "Key Vault SOPS secret", "b/two.yaml")]}),
        ]
        nodes, remap, counters = merge_chunks.merge_nodes(chunks)
        self.assertEqual(counters["namespaced"], 2)
        self.assertEqual(len(nodes), 2)
        self.assertNotEqual(remap[("c1", "sops_key")], remap[("c2", "sops_key")])

    def test_every_renamed_node_records_what_it_was(self):
        """Not retained, not recoverable, not recorded as lost is the worst of three.

        One estate's merger overwrites `id` and discards its remap at exit, so a
        consumer resolving the extractor's id finds nothing and a consumer holding
        the synthesised id cannot get back.
        """
        chunks = [
            ("c1", {"nodes": [node("dup", "one thing", "a/one.yaml")]}),
            ("c2", {"nodes": [node("dup", "a different thing", "b/two.yaml")]}),
        ]
        nodes, _remap, _counters = merge_chunks.merge_nodes(chunks)
        for entry in nodes.values():
            self.assertEqual(entry["original_id"], "dup")

    def test_the_namespace_never_carries_a_chunk_number(self):
        """Forbidden by the spec outright, and unstable: chunk numbering is not
        reproducible, so such an id changes on any re-plan."""
        chunks = [
            ("c0042", {"nodes": [node("slug", "first", "a/one.yaml")]}),
            ("c0043", {"nodes": [node("slug", "second", "b/two.yaml")]}),
        ]
        nodes, _remap, _counters = merge_chunks.merge_nodes(chunks)
        self.assertEqual(merge_chunks.spec_breaches(nodes), [])
        for nid in nodes:
            self.assertNotIn("0042", nid)
            self.assertNotIn("0043", nid)


class Consolidation(unittest.TestCase):
    def test_a_global_identifier_scattered_across_ids_is_collapsed(self):
        chunks = [
            ("c1", {"nodes": [node("a_one_img", "registry.example.io/api:1.2", "a/one.yaml")]}),
            ("c2", {"nodes": [node("b_two_img", "registry.example.io/api:1.2", "b/two.yaml")]}),
        ]
        nodes, remap, counters = merge_chunks.merge_nodes(chunks)
        merge_chunks.consolidate(nodes, remap, counters | {"consolidated": 0, "fragmented_left": 0})
        self.assertEqual(len(nodes), 1)

    def test_a_generic_kind_is_left_fragmented_and_counted(self):
        """`Kustomization` alone carried 109 distinct ids on one estate, and those are
        109 different resources. Merging them invents relationships between unrelated
        environments, and a fabricated edge is worse than a missing one."""
        chunks = [
            ("c1", {"nodes": [node("a_one_k", "Kustomization", "a/one.yaml")]}),
            ("c2", {"nodes": [node("b_two_k", "Kustomization", "b/two.yaml")]}),
        ]
        nodes, remap, base = merge_chunks.merge_nodes(chunks)
        counters = base | {"consolidated": 0, "fragmented_left": 0}
        merge_chunks.consolidate(nodes, remap, counters)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(counters["consolidated"], 0)
        self.assertEqual(counters["fragmented_left"], 1, "the residue must be counted, not hidden")

    def test_the_global_test_rejects_prose(self):
        for label in ("Kustomization", "the payment service", "short", "a b/c"):
            with self.subTest(label=label):
                self.assertFalse(merge_chunks.is_global_identifier(label))
        for label in ("registry.example.io/api:1.2", "https://vault.example.net/x"):
            with self.subTest(label=label):
                self.assertTrue(merge_chunks.is_global_identifier(label))

    def test_an_ordinary_filename_is_not_a_global_identifier(self):
        """The defect this fixes, and it fired on every estate rather than infra ones.

        The test accepted a bare dot, so `values.yaml`, `README.md`, `package.json`
        and `index.ts` all qualified - meaning every one of them in the corpus would
        consolidate into a single node, fabricating relationships between unrelated
        files. That is exactly what excluding `Kustomization` by name exists to
        prevent, arriving through the separator rule instead of the stop-list.
        """
        for label in (
            "values.yaml",
            "README.md",
            "index.ts",
            "Chart.yaml",
            "package.json",
            "kustomization.yaml",
            "main.tf",
            "docker-compose.yml",
        ):
            with self.subTest(label=label):
                self.assertFalse(
                    merge_chunks.is_global_identifier(label),
                    f"{label} would fuse every file of that name in the estate",
                )

    def test_an_address_still_qualifies(self):
        """The sensitivity check on the test above: a rule rejecting everything would
        satisfy it while consolidating nothing, which loses the 1,051 real fragments."""
        for label in (
            "registry.example.io/api:1.2",
            "https://vault.example.net/secret",
            "user@host.example",
            "ghcr.io/org/image:sha-abc123",
        ):
            with self.subTest(label=label):
                self.assertTrue(merge_chunks.is_global_identifier(label))

    def test_files_of_the_same_name_are_left_fragmented_and_counted(self):
        """End to end: the residue must be visible, not silently fused."""
        chunks = [
            ("c1", {"nodes": [node("a_one_values", "values.yaml", "a/one/values.yaml")]}),
            ("c2", {"nodes": [node("b_two_values", "values.yaml", "b/two/values.yaml")]}),
        ]
        nodes, remap, base = merge_chunks.merge_nodes(chunks)
        counters = base | {"consolidated": 0, "fragmented_left": 0}
        merge_chunks.consolidate(nodes, remap, counters)
        self.assertEqual(len(nodes), 2, "two different files were fused into one")
        self.assertEqual(counters["consolidated"], 0)
        self.assertEqual(counters["fragmented_left"], 1)


class Edges(unittest.TestCase):
    def _resolved(self, chunks):
        nodes, remap, _c = merge_chunks.merge_nodes(chunks)
        return merge_chunks.resolve_edges(chunks, nodes, remap)

    def test_a_cross_chunk_endpoint_is_recovered(self):
        """37 of 43 such endpoints on one estate resolved to exactly one other chunk;
        a concatenating merge dropped all 43 as dangling."""
        chunks = [
            (
                "c1",
                {
                    "nodes": [node("here", "here", "a/one.yaml")],
                    "edges": [{"source": "here", "target": "elsewhere", "relation": "references"}],
                },
            ),
            ("c2", {"nodes": [node("elsewhere", "elsewhere", "b/two.yaml")], "edges": []}),
        ]
        edges, counters = self._resolved(chunks)
        self.assertEqual(counters["recovered"], 1)
        self.assertEqual([(e["source"], e["target"]) for e in edges], [("here", "elsewhere")])

    def test_an_invented_endpoint_is_dropped_and_counted(self):
        chunks = [
            (
                "c1",
                {
                    "nodes": [node("here", "here", "a/one.yaml")],
                    "edges": [{"source": "here", "target": "nowhere", "relation": "references"}],
                },
            )
        ]
        edges, counters = self._resolved(chunks)
        self.assertEqual(edges, [])
        self.assertEqual(counters["dangling"], 1)

    def test_duplicate_edges_are_collapsed(self):
        edge = {"source": "a", "target": "b", "relation": "references"}
        chunks = [
            (
                "c1",
                {
                    "nodes": [node("a", "a", "x.yaml"), node("b", "b", "x.yaml")],
                    "edges": [edge, dict(edge)],
                },
            )
        ]
        edges, counters = self._resolved(chunks)
        self.assertEqual(len(edges), 1)
        self.assertEqual(counters["duplicate"], 1)


class AmbiguousEndpoints(unittest.TestCase):
    """The case the reporting estate never exercised.

    It recorded 0 endpoints dropped as ambiguous, which is a rule with no evidence
    rather than a rule that works - so the ambiguous case is built here on purpose:
    one id that resolves to two different final ids, named by a third chunk that
    defined neither.
    """

    def test_an_ambiguous_endpoint_is_dropped_rather_than_guessed(self):
        chunks = [
            ("c1", {"nodes": [node("slug", "first thing", "a/one.yaml")], "edges": []}),
            ("c2", {"nodes": [node("slug", "second thing", "b/two.yaml")], "edges": []}),
            (
                "c3",
                {
                    "nodes": [node("caller", "caller", "c/three.yaml")],
                    "edges": [{"source": "caller", "target": "slug", "relation": "references"}],
                },
            ),
        ]
        nodes, remap, _c = merge_chunks.merge_nodes(chunks)
        edges, counters = merge_chunks.resolve_edges(chunks, nodes, remap)
        self.assertEqual(counters["ambiguous"], 1, "guessing here fabricates a relationship")
        self.assertEqual(edges, [], "the edge must be dropped, not attached to either")


class TheOutputGate(SettingsIsolated):
    """Asserted on this stage's output, because a gate on the input cannot see it.

    One estate's chunk-file gate forbids chunk-numbered ids at zero tolerance and
    reported 0 errors across 1,556 files, while its merger emitted 187 of them.
    """

    def test_a_chunk_suffixed_id_refuses_to_be_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "graphify-out").mkdir()
            config.configure(root=str(root))
            (root / "graphify-out" / ".graphify_chunk_0001.json").write_text(
                json.dumps({"nodes": [node("already_c0001", "leaked", "a/one.yaml")], "edges": []})
            )
            out = _io.StringIO()
            with contextlib.redirect_stdout(out):
                code = merge_chunks.main([])
            self.assertEqual(code, 1)
            self.assertIn("REFUSING", out.getvalue())
            self.assertFalse((root / "graphify-out" / ".graphify_semantic_new.json").exists())

    def test_a_clean_merge_is_written(self):
        """The sensitivity check on the refusal: if it fired either way, the test above
        would pass while saying nothing about chunk suffixes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "graphify-out").mkdir()
            config.configure(root=str(root))
            (root / "graphify-out" / ".graphify_chunk_0001.json").write_text(
                json.dumps({"nodes": [node("clean_id", "fine", "a/one.yaml")], "edges": []})
            )
            out = _io.StringIO()
            with contextlib.redirect_stdout(out):
                code = merge_chunks.main([])
            self.assertEqual(code, 0, out.getvalue())
            self.assertTrue((root / "graphify-out" / ".graphify_semantic_new.json").exists())

    def test_no_chunks_at_all_is_a_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            (Path(tmp) / "graphify-out").mkdir()
            with contextlib.redirect_stdout(_io.StringIO()):
                self.assertEqual(merge_chunks.main([]), 2)


if __name__ == "__main__":
    unittest.main()
