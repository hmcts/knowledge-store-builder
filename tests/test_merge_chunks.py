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
        self.assertEqual(merge_chunks.spec_breaches(nodes, {"0042", "0043"}), [])
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


class DefectsFoundOnARealEstate(unittest.TestCase):
    """The four defects an operator found by running this against 1,556 chunks.

    All four were reported on the PR before it merged and were still on `main`.
    Every expected number here is from that run, diffed against that store's own
    committed merge output rather than a reimplementation of this logic.
    """

    def test_a_form_code_is_not_a_chunk_suffix(self):
        """The blocking one. `_c\\d{2,}` matched 34 ids on that estate, every one a
        family court form code - C21, C43, C51, C63, C100, the last in 40 files. The
        refusal is hard, so a false positive was not a warning but total
        unavailability: the stage could not run there at all.

        Tested against the run's real chunk numbers, which is exact in both
        directions. Widening to `\\d{4}` would have cleared these and been wrong for
        the reason it worked - 4-digit padding is one store's convention.
        """
        chunk_numbers = {f"{i:04d}" for i in range(1, 1557)}
        # The form code lands at the END of the id, because it is at the end of the
        # label it was normalised from - 'Blank order or directions (C21)'. An earlier
        # version of this test put the code at the start, where the anchored pattern
        # could never match it, so the test passed whether or not the chunk numbers
        # were consulted. Vacuous, and only a mutation showed it.
        ids = {
            "blank_order_or_directions_c21": {},
            "child_arrangements_specific_issue_order_c43": {},
            "applicant_details_c100": {},
            "supporting_documents_c51": {},
            "statement_of_service_c63": {},
        }
        for nid in ids:
            self.assertRegex(nid, r"_c\d+$", "the fixture must actually match the pattern")
        self.assertEqual(merge_chunks.spec_breaches(ids, chunk_numbers), [])

    def test_a_real_chunk_suffix_is_still_caught(self):
        """The sensitivity check: a rule that flagged nothing would satisfy the test
        above while letting the spec breach through, which is what the output-side
        assertion exists to catch - it found 187 real ones an input gate could not."""
        chunk_numbers = {"0042", "0007"}
        ids = {"slug_c0042": {}, "other_chunk0007": {}, "c21_form": {}}
        self.assertEqual(
            merge_chunks.spec_breaches(ids, chunk_numbers), ["other_chunk0007", "slug_c0042"]
        )

    def test_two_identities_sharing_stem_and_original_both_survive(self):
        """13 of 47,653 nodes were lost silently, and this is the collision the
        namespacing exists to resolve, reintroduced by the step that resolves it.

        `namespaced` reported 367 kept apart while 13 were dropped - a counter
        overstating success, which is the reassuring direction and the dangerous one.
        """
        chunks = [
            (
                "c1",
                {
                    "nodes": [
                        node("end_ga_hwf", "End Ga Hwf Notify Process", "pmn/diagrams.bpmn"),
                        node("end_ga_hwf", "END_GA_HWF_NOTIFY_PROCESS", "pmn/diagrams.bpmn"),
                    ]
                },
            )
        ]
        nodes, remap, counters = merge_chunks.merge_nodes(chunks)
        self.assertEqual(len(nodes), 2, "one identity was dropped on an id collision")
        self.assertEqual(counters["disambiguated"], 1)
        self.assertEqual(len({remap[("c1", "end_ga_hwf")]}), 1)
        self.assertEqual(len({n["label"] for n in nodes.values()}), 2, "both labels survive")

    def test_a_templated_label_is_not_a_global_identifier(self):
        """`${ENVIRONMENT}` resolves differently per environment, so consolidating on
        it collapses every environment's resource into one node and every edge
        follows. 25 such labels on that estate; rejecting them moved `consolidated`
        from 1,070 to 1,051, exactly reproducing the committed count.
        """
        for label in (
            "${SERVICE_NAME}-documents-api.preview.platform.example.net",
            "./apps/pdm/${ENVIRONMENT}/base",
            "/case/X/Y/${[CASE_REFERENCE]}/trigger/*",
            "{{ .Release.Name }}/api",
            "%(env)s/service",
        ):
            with self.subTest(label=label):
                self.assertFalse(merge_chunks.is_global_identifier(label))

    def test_consolidate_accepts_merge_nodes_counters(self):
        """Both are public, and only `main` injected the two keys, so any other caller
        got a KeyError."""
        chunks = [("c1", {"nodes": [node("a", "registry.example.io/x:1", "a.yaml")]})]
        nodes, remap, counters = merge_chunks.merge_nodes(chunks)
        merge_chunks.consolidate(nodes, remap, counters)
        for key in ("consolidated", "fragmented_left", "disambiguated"):
            self.assertIn(key, counters)
