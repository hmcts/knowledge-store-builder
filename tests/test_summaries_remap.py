"""The remap that survives a re-cluster.

Adding repositories moves community ids, so every committed summary is stranded
against ids that no longer mean the same thing. The recovery is to carry a
summary across only when the new cluster holding most of its old members holds a
convincing majority of them, and to drop it otherwise — prose attached to the
wrong cluster is worse than no prose, because it reads as authoritative.

Each test names the break it catches. The two guards exist because both failures
happened to hand-rolled versions of this: a mis-specified path silently produced
an empty remap, and a stale snapshot silently dropped everything.
"""

from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import config  # noqa: E402


def _graph(members: dict[str, list[str]]) -> dict:
    """A node-link graph where `members` maps community id -> node ids."""
    nodes = [
        {"id": node, "label": node, "community": int(cid)}
        for cid, ids in members.items()
        for node in ids
    ]
    return {"directed": False, "multigraph": False, "graph": {}, "nodes": nodes, "links": []}


class RemapTest(SettingsIsolated):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        (self.root / "graphify-out").mkdir(parents=True)
        self._old_root = config.ROOT
        config.configure(root=str(self.root))

    def tearDown(self):
        config.configure(root=str(self._old_root))
        self._tmp.cleanup()

    # --- helpers -------------------------------------------------------------

    def write_graph(self, members):
        config.GRAPH_PATH.write_text(json.dumps(_graph(members)), encoding="utf-8")

    def write_partly_clustered_graph(self, clustered: int, unclustered: int):
        """A graph where only some nodes carry a community, as a failed write leaves it.

        Writes through `config.GRAPH_PATH` — the path the code under test
        actually reads — rather than `config.GRAPH_PATH`, which the two can
        disagree about once another module has reconfigured the root.
        """
        graph = _graph({str(i): [f"n{i}"] for i in range(clustered)})
        graph["nodes"] += [
            {"id": f"n{i}", "label": f"n{i}"} for i in range(clustered, clustered + unclustered)
        ]
        config.GRAPH_PATH.write_text(json.dumps(graph), encoding="utf-8")

    def write_snapshot(self, members):
        with gzip.open(config.SUMMARIES_SNAPSHOT_PATH, "wt", encoding="utf-8") as f:
            json.dump(members, f)

    def write_summaries(self, mapping):
        config.SUMMARIES_PATH.write_text(json.dumps(mapping), encoding="utf-8")

    def read_summaries(self):
        return json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8"))

    def many(self, n, prefix="c"):
        """n summaries, so the plausibility guard does not fire in other tests."""
        return {str(i): f"{prefix} summary {i}" for i in range(1000, 1000 + n)}

    # --- snapshot ------------------------------------------------------------

    def test_snapshot_records_community_membership_from_the_graph(self):
        # without this, a remap after re-clustering has nothing to compare against
        self.write_graph({"1": ["a", "b"], "2": ["c"]})
        self.assertEqual(summaries.snapshot(), 0)
        with gzip.open(config.SUMMARIES_SNAPSHOT_PATH, "rt", encoding="utf-8") as f:
            recorded = json.load(f)
        self.assertEqual(recorded, {"1": ["a", "b"], "2": ["c"]})

    def test_snapshot_refuses_when_the_graph_has_no_communities(self):
        # a graph that has not been clustered yet would snapshot as empty and
        # silently make every later remap drop everything
        config.GRAPH_PATH.write_text(
            json.dumps({"nodes": [{"id": "a", "label": "a"}], "links": []}), encoding="utf-8"
        )
        self.assertEqual(summaries.snapshot(), 1)
        self.assertFalse(config.SUMMARIES_SNAPSHOT_PATH.exists())

    # --- the overlap bar -----------------------------------------------------

    def test_summary_is_carried_when_the_dominant_cluster_holds_enough_members(self):
        old = {"7": [f"n{i}" for i in range(10)]}
        # seven of ten land in new cluster 42, three scatter
        new = {"42": [f"n{i}" for i in range(7)], "43": [f"n{i}" for i in range(7, 10)]}
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"7": "the seven-of-ten cluster"} | self.many(30))
        self.assertEqual(summaries.remap(bar=0.6), 0)
        result = self.read_summaries()
        self.assertEqual(result.get("42"), "the seven-of-ten cluster")
        self.assertNotIn("7", result)

    def test_the_same_summary_is_dropped_at_a_higher_bar(self):
        # the issue's defining case: 7/10 carries at 0.6 and does not at 0.8
        old = {"7": [f"n{i}" for i in range(10)]}
        new = {"42": [f"n{i}" for i in range(7)], "43": [f"n{i}" for i in range(7, 10)]}
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"7": "the seven-of-ten cluster"} | self.many(30))
        self.assertEqual(summaries.remap(bar=0.8), 0)
        self.assertNotIn("42", self.read_summaries())

    def test_a_split_cluster_with_no_majority_is_dropped_not_guessed(self):
        # prose on the wrong cluster reads as authoritative, so refuse to place it
        old = {"7": [f"n{i}" for i in range(10)]}
        new = {
            "50": [f"n{i}" for i in range(4)],
            "51": [f"n{i}" for i in range(4, 7)],
            "52": [f"n{i}" for i in range(7, 10)],
        }
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"7": "no majority anywhere"} | self.many(30))
        self.assertEqual(summaries.remap(), 0)
        result = self.read_summaries()
        for cid in ("50", "51", "52"):
            self.assertNotIn(cid, result)

    # --- edge cases that produced wrong output in hand-rolled versions -------

    def test_two_old_clusters_merging_into_one_keeps_a_single_summary(self):
        # both would map to the same new id; writing both loses one silently and
        # the choice must be deterministic, not dict-ordering luck
        old = {"1": ["a", "b", "c"], "2": ["d", "e", "f"]}
        new = {"99": ["a", "b", "c", "d", "e", "f"]}
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"1": "from one", "2": "from two"} | self.many(30))
        self.assertEqual(summaries.remap(), 0)
        result = self.read_summaries()
        self.assertEqual(result.get("99"), "from one", "lowest old id wins, deterministically")

    def test_a_summary_whose_members_have_all_gone_is_dropped(self):
        old = {"7": ["gone1", "gone2"]}
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph({str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"7": "orphaned"} | self.many(30))
        self.assertEqual(summaries.remap(), 0)
        self.assertNotIn("7", self.read_summaries())

    def test_a_summary_with_no_snapshot_entry_is_dropped_not_carried_forward(self):
        # an id present in communities.json but absent from the snapshot cannot
        # be placed; keeping it would leave prose on an unrelated new cluster
        self.write_snapshot({str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph({str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"7": "never snapshotted"} | self.many(30))
        self.assertEqual(summaries.remap(), 0)
        self.assertNotIn("7", self.read_summaries())

    # --- guards --------------------------------------------------------------

    def test_refuses_when_snapshot_and_graph_share_no_nodes(self):
        # the wrong snapshot: proceeding would drop every summary and report it
        # as a legitimate 0% retention
        self.write_snapshot({"1": ["old-a", "old-b"]})
        self.write_graph({"1": ["new-a", "new-b"]})
        before = {"1": "should survive this"} | self.many(30)
        self.write_summaries(before)
        self.assertEqual(summaries.remap(), 1)
        self.assertEqual(before, self.read_summaries(), "must not write on a refused run")

    def test_refuses_when_the_graph_is_effectively_unclustered(self):
        # Catches a clustering step that reported success without persisting its
        # result: the graph is readable and almost entirely unclustered, so every
        # summary would be dropped and reported as legitimate churn. The
        # wrong-snapshot guard cannot see this, because the few communities that
        # did survive still share node ids with the snapshot.
        self.write_snapshot({str(i): [f"n{i}"] for i in range(40)})
        self.write_partly_clustered_graph(clustered=1, unclustered=39)
        before = {str(i): f"summary {i}" for i in range(40)}
        self.write_summaries(before)
        self.assertEqual(summaries.remap(), 1)
        self.assertEqual(before, self.read_summaries(), "must not overwrite committed prose")

    def test_the_coverage_guard_can_be_lowered_for_a_deliberately_sparse_graph(self):
        self.write_snapshot({str(i): [f"n{i}"] for i in range(40)})
        self.write_partly_clustered_graph(clustered=1, unclustered=39)
        self.write_summaries({str(i): f"summary {i}" for i in range(40)})
        self.assertEqual(summaries.remap(coverage=0.0), 0)

    def test_the_coverage_guard_counts_nodes_not_distinct_ids(self):
        # A merged graph can repeat a node id. Measuring coverage by the size of
        # an id-keyed dict collapses those repeats and understates it — here 2
        # distinct ids against 31 nodes, which would refuse a graph in which
        # every node is clustered.
        nodes = [{"id": "dup", "label": "dup", "community": 1} for _ in range(30)]
        nodes.append({"id": "other", "label": "other", "community": 2})
        config.GRAPH_PATH.write_text(json.dumps({"nodes": nodes, "links": []}), encoding="utf-8")
        self.write_snapshot({"1": ["dup"], "2": ["other"]})
        self.write_summaries({str(i): f"summary {i}" for i in range(1, 21)})
        self.assertEqual(summaries.remap(), 0, "a fully clustered graph must not be refused")

    def test_refuses_when_there_are_implausibly_few_summaries_to_remap(self):
        # a mis-specified path reads as "almost nothing to do" rather than failing
        self.write_snapshot({"1": ["a"]})
        self.write_graph({"1": ["a"]})
        self.write_summaries({"1": "the only one"})
        self.assertEqual(summaries.remap(floor=10), 1)

    def test_the_floor_guard_can_be_lowered_for_a_genuinely_small_store(self):
        self.write_snapshot({"1": ["a"]})
        self.write_graph({"1": ["a"]})
        self.write_summaries({"1": "the only one"})
        self.assertEqual(summaries.remap(floor=1), 0)
        self.assertEqual(self.read_summaries(), {"1": "the only one"})

    # --- reporting -----------------------------------------------------------

    def test_retention_is_reported_so_the_cost_of_a_recluster_is_visible(self):
        old = {"1": ["a", "b"], "2": ["c", "d"]}
        new = {"10": ["a", "b"]}  # cluster 2's members are gone
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"1": "kept", "2": "lost"} | self.many(30))
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            summaries.remap()
        printed = buffer.getvalue()
        self.assertIn("retained", printed.lower())
        self.assertIn("31 of 32", printed, "counts must be explicit, not a percentage alone")


class RemapCliTest(RemapTest):
    """Dispatch, driven through main - a stage nobody can invoke gets hand-rolled again."""

    def test_snapshot_sub_command_writes_the_snapshot(self):
        self.write_graph({"1": ["a", "b"]})
        self.assertEqual(summaries.main(["snapshot"]), 0)
        self.assertTrue(config.SUMMARIES_SNAPSHOT_PATH.exists())

    def test_remap_sub_command_carries_summaries_across(self):
        self.write_snapshot({"7": ["a", "b", "c"]} | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph({"42": ["a", "b", "c"]} | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"7": "carried by the cli"} | self.many(30))
        self.assertEqual(summaries.main(["remap"]), 0)
        self.assertEqual(self.read_summaries().get("42"), "carried by the cli")

    def test_remap_bar_is_settable_from_the_command_line(self):
        old = {"7": [f"n{i}" for i in range(10)]}
        new = {"42": [f"n{i}" for i in range(7)], "43": [f"n{i}" for i in range(7, 10)]}
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"7": "seven of ten"} | self.many(30))
        self.assertEqual(summaries.main(["remap", "--bar", "0.8"]), 0)
        self.assertNotIn("42", self.read_summaries(), "--bar 0.8 must reject 0.7 overlap")

    def test_an_unknown_sub_command_fails_rather_than_silently_doing_nothing(self):
        self.assertEqual(summaries.main(["remapp"]), 1)


if __name__ == "__main__":
    unittest.main()
