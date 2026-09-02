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

import contextlib
import gzip
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import io as store_io  # noqa: E402


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

    def read_withdrawn(self):
        return json.loads(config.SUMMARIES_WITHDRAWN_PATH.read_text(encoding="utf-8"))

    def read_report(self):
        return json.loads(config.REMAP_REPORT_PATH.read_text(encoding="utf-8"))

    def write_swallowed(self):
        """An old cluster of five absorbed whole into a new one of twenty.

        Recall 1.00 — every old member is still together — and precision 0.25,
        which clears the shipped precision floor. The one shape the old criterion
        could not see.
        """
        old = {"7": [f"n{i}" for i in range(5)]}
        new = {"42": [f"n{i}" for i in range(5)] + [f"u{i}" for i in range(15)]}
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"7": "the five-node cluster"} | self.many(30))

    def write_identical(self):
        """An old cluster carried onto a new id with its membership unchanged."""
        self.write_snapshot({"7": ["a", "b", "c"]} | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph({"42": ["a", "b", "c"]} | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"7": "the same three nodes"} | self.many(30))

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

    # --- the carry criterion -------------------------------------------------

    def test_a_community_that_swallowed_an_old_one_does_not_carry_its_prose(self):
        """#296. Recall alone cannot see this: a new community that absorbs an
        old one whole scores 1.00 however much unrelated material it also holds,
        so prose written about a small coherent community was silently
        re-attached to a large incoherent one — and every summary still had a
        community and every community still had prose, so the store looked
        healthy."""
        self.write_swallowed()
        self.assertEqual(summaries.remap(), 0)
        result = self.read_summaries()
        self.assertNotIn(
            "42", result, "recall is 1.00 here, so only set equality can withhold the prose"
        )
        self.assertNotIn("7", result)

    def test_an_identical_member_set_still_carries(self):
        """The sensitivity half, and the one that matters: a criterion that
        refuses everything passes the test above."""
        self.write_identical()
        self.assertEqual(summaries.remap(), 0)
        self.assertEqual(self.read_summaries().get("42"), "the same three nodes")

    def test_the_swallowed_communitys_prose_is_withdrawn_rather_than_dropped(self):
        """Withdrawing is only legible if the writing survives it, so the prose
        lands in a file shaped like the one it left and can be revised and
        merged back."""
        self.write_swallowed()
        self.assertEqual(summaries.remap(), 0)
        self.assertEqual(self.read_withdrawn().get("7"), "the five-node cluster")
        displaced = self.read_report()["displaced"]["7"]
        self.assertEqual(displaced["reason"], "not-identical")
        self.assertEqual(displaced["best_target"], "42", "the near miss is the backfill's target")
        self.assertEqual(displaced["share"], 1.0, "recall stays perfect - which is the whole bug")

    def test_a_carried_summary_is_not_also_withdrawn(self):
        self.write_identical()
        self.assertEqual(summaries.remap(), 0)
        self.assertEqual(self.read_withdrawn(), {}, "nothing was withheld, so nothing is withdrawn")
        self.assertIs(self.read_report()["carried"]["42"]["exact"], True)

    def test_carried_and_withdrawn_reconcile_against_the_summaries_read(self):
        """Every summary is carried or withdrawn, never neither: a criterion
        this strict is only trustworthy if nothing falls out of the count."""
        old = {"1": ["a", "b"], "2": ["c", "d"], "3": ["gone1"]}
        new = {"10": ["a", "b"], "11": ["c", "d", "e", "f"]}
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"1": "one", "2": "two", "3": "three"} | self.many(30))
        self.assertEqual(summaries.remap(), 0)
        carried = self.read_report()["carried"]
        withdrawn = self.read_withdrawn()
        self.assertEqual(sorted(withdrawn, key=summaries._by_id), ["2", "3"])
        self.assertEqual(len(carried) + len(withdrawn), 33, "33 summaries went in")
        self.assertEqual(len(withdrawn), len(self.read_report()["displaced"]))

    def test_the_withdrawal_count_is_reported_beside_the_retention_figure(self):
        """The retention figure reads as reassurance, and on a real rebuild it
        was the opposite. It now carries its own contradiction."""
        old = {"1": ["a", "b"], "2": ["c", "d"], "3": ["gone1"]}
        new = {"10": ["a", "b"], "11": ["c", "d", "e", "f"]}
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"1": "one", "2": "two", "3": "three"} | self.many(30))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            summaries.remap()
        printed = buffer.getvalue()
        self.assertIn("31 of 33", printed)
        self.assertIn("withdrew 2", printed, "the retention figure alone reads as reassurance")
        self.assertIn("1 not identical", printed)
        self.assertIn("1 whose members are gone", printed)

    def test_a_run_that_withdraws_nothing_replaces_an_earlier_runs_file(self):
        """A withdrawn file left behind by the previous remap reads as this
        run's finding — the stale-artefact shape, on an artefact that names
        prose somebody is meant to go and re-author."""
        config.SUMMARIES_WITHDRAWN_PATH.write_text(
            json.dumps({"99": "withdrawn by an earlier remap"}), encoding="utf-8"
        )
        self.write_identical()
        self.assertEqual(summaries.remap(), 0)
        self.assertNotIn("99", self.read_withdrawn())

    # --- the tolerance, kept but no longer the default -----------------------

    def test_the_overlap_criterion_carries_the_swallowing_case_when_asked_for(self):
        """The previous behaviour, reachable and now an explicit choice."""
        self.write_swallowed()
        self.assertEqual(summaries.remap(carry="overlap"), 0)
        self.assertEqual(self.read_summaries().get("42"), "the five-node cluster")

    def test_prose_carried_below_set_equality_is_marked_in_the_report(self):
        """Suggestion 2 of #296: a tolerance is only defensible if a downstream
        check can tell which summaries it stretched to place."""
        self.write_swallowed()
        self.assertEqual(summaries.remap(carry="overlap"), 0)
        self.assertIs(self.read_report()["carried"]["42"]["exact"], False)

    def test_prose_carried_onto_its_own_set_is_marked_exact_under_the_tolerance(self):
        """Without this the mark could be a constant `false` and say nothing."""
        self.write_identical()
        self.assertEqual(summaries.remap(carry="overlap"), 0)
        self.assertIs(self.read_report()["carried"]["42"]["exact"], True)

    def test_the_count_carried_below_equality_is_reported(self):
        """The mark is machine-readable; the count is what an operator sees
        without opening the report."""
        self.write_swallowed()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            summaries.remap(carry="overlap")
        self.assertIn("1 of 31", buffer.getvalue())

    # --- the overlap bar, under --carry overlap ------------------------------

    def test_summary_is_carried_when_the_dominant_cluster_holds_enough_members(self):
        old = {"7": [f"n{i}" for i in range(10)]}
        # seven of ten land in new cluster 42, three scatter
        new = {"42": [f"n{i}" for i in range(7)], "43": [f"n{i}" for i in range(7, 10)]}
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"7": "the seven-of-ten cluster"} | self.many(30))
        self.assertEqual(summaries.remap(bar=0.6, carry="overlap"), 0)
        result = self.read_summaries()
        self.assertEqual(result.get("42"), "the seven-of-ten cluster")
        self.assertNotIn("7", result)

    def test_the_default_precision_floor_drops_a_ballooned_cluster(self):
        """Drives `remap()` with no precision argument, so the shipped default
        is what is under test.

        Every other precision test passes the floor explicitly, so all of them
        pass with the default set to zero - the value that actually ships,
        protecting nothing. Same gap as an unwired check: the behaviour was
        covered and the configuration was not.
        """
        old = {"154": [f"n{i}" for i in range(37)]}
        new = {"9": [f"n{i}" for i in range(37)] + [f"grew{i}" for i in range(421)]}
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"154": "describes 37 members"} | self.many(30))
        self.assertEqual(summaries.remap(carry="overlap"), 0)
        self.assertNotIn(
            "9",
            self.read_summaries(),
            "recall is 1.00 here, so only a precision floor can drop it",
        )

    def test_the_same_summary_is_dropped_at_a_higher_bar(self):
        # the issue's defining case: 7/10 carries at 0.6 and does not at 0.8
        old = {"7": [f"n{i}" for i in range(10)]}
        new = {"42": [f"n{i}" for i in range(7)], "43": [f"n{i}" for i in range(7, 10)]}
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"7": "the seven-of-ten cluster"} | self.many(30))
        self.assertEqual(summaries.remap(bar=0.8, carry="overlap"), 0)
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
        self.assertEqual(summaries.remap(carry="overlap"), 0)
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
        self.assertEqual(summaries.remap(carry="overlap"), 0)
        result = self.read_summaries()
        self.assertEqual(result.get("99"), "from one", "lowest old id wins, deterministically")

    def test_collision_winner_is_the_largest_share_not_the_lowest_id(self):
        """Behaviour change, measured before making it: on a real refresh the
        share rule chose a better-fitting summary for 36 of 86 contested
        clusters (median +16.7 points of overlap) with identical retention.
        The old rule kept the lowest old id regardless of fit."""
        # old "1": 3 of 5 members land in 99 (share 0.6); old "2": 3 of 3 (1.0)
        old = {"1": ["a", "b", "c", "d", "e"], "2": ["f", "g", "h"]}
        new = {"99": ["a", "b", "c", "f", "g", "h"]}
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries({"1": "from one", "2": "from two"} | self.many(30))
        self.assertEqual(summaries.remap(carry="overlap"), 0)
        self.assertEqual(
            self.read_summaries().get("99"),
            "from two",
            "the summary describing more of the merged cluster wins",
        )

    def test_remap_report_keeps_displaced_prose_and_carried_provenance(self):
        """Dropped prose used to be recoverable only by git archaeology. The
        report is the spool the backfill revises from, and the carried map is
        what lets verify split its flag rate by provenance."""
        old = {
            "1": ["a", "b", "c"],  # carried cleanly
            "2": ["a2", "b2", "c2"],  # 2 of 3 land in 88 (0.67) - loses to 3
            "3": ["d2", "e2", "f2", "g2"],  # 4 of 4 land in 88 (1.0) - wins
            "4": ["p", "q", "r", "s", "t"],  # below bar: 2 of 5 land anywhere
            "5": ["gone1", "gone2"],  # members gone
        }
        new = {
            "77": ["a", "b", "c"],
            "88": ["a2", "b2", "d2", "e2", "f2", "g2"],
            "66": ["p", "q"],
        }
        self.write_snapshot(old | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_graph(new | {str(i): [f"x{i}"] for i in range(1000, 1030)})
        self.write_summaries(
            {"1": "one", "2": "two", "3": "three", "4": "four", "5": "five"} | self.many(30)
        )
        self.assertEqual(summaries.remap(carry="overlap"), 0)
        report = json.loads(config.REMAP_REPORT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            report["carried"]["77"],
            {"from": "1", "share": 1.0, "precision": 1.0, "exact": True},
            "the carried record now says how much of its new cluster the prose describes",
        )
        self.assertEqual(report["carried"]["88"]["from"], "3")
        displaced = report["displaced"]
        self.assertEqual(displaced["2"]["reason"], "collision")
        self.assertEqual(displaced["2"]["best_target"], "88")
        self.assertEqual(displaced["2"]["prose"], "two", "the prose itself is the point")
        self.assertEqual(displaced["4"]["reason"], "below-bar")
        self.assertEqual(displaced["5"]["reason"], "members-gone")

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
        # The prose alone: the write also carries the reserved metadata block the
        # skip in `RemapWriteGateTest` is gated on (#313).
        self.assertEqual(store_io.read_summaries(config.SUMMARIES_PATH), {"1": "the only one"})

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
        self.assertEqual(summaries.main(["remap", "--carry", "overlap", "--bar", "0.8"]), 0)
        self.assertNotIn("42", self.read_summaries(), "--bar 0.8 must reject 0.7 overlap")

    def test_the_carry_criterion_is_settable_from_the_command_line(self):
        """The tolerance is only an opt-in if the flag reaches `remap`."""
        self.write_swallowed()
        self.assertEqual(summaries.main(["remap", "--carry", "overlap"]), 0)
        self.assertEqual(
            self.read_summaries().get("42"),
            "the five-node cluster",
            "--carry overlap must reach the stage; the default withholds this",
        )

    def test_an_unknown_carry_criterion_is_rejected_rather_than_ignored(self):
        """argparse exits 2 on an unknown choice. A silently ignored value would
        run the default while the operator believed they had changed it."""
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            summaries.main(["remap", "--carry", "jaccard"])

    def test_an_unknown_sub_command_fails_rather_than_silently_doing_nothing(self):
        self.assertEqual(summaries.main(["remapp"]), 1)


class RemapRefusalTest(unittest.TestCase):
    """The refusals, tested apart from how they are reported.

    Each of these produces a plausible-looking 0% retention rather than an
    error, which is why they are refusals. Extracting them from `remap` is only
    worth anything if the reason is checkable without a store on disk, a graph
    file, or captured stdout - so these call the function directly and assert on
    the returned message.
    """

    OK = dict(
        summaries={"1": "prose"},
        nodes=[{"id": "a", "community": 1}],
        new_community={"a": "1"},
        old_members={"1": ["a"]},
        floor=1,
        coverage=0.5,
    )

    def _refusal(self, **overrides):
        return summaries._remap_refusal(**{**self.OK, **overrides})

    def test_a_healthy_remap_is_not_refused(self):
        self.assertIsNone(self._refusal())

    def test_it_returns_the_reason_rather_than_printing_it(self):
        """The point of the extraction: reason and reporting are separable."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            message = self._refusal(floor=99)
        self.assertEqual(out.getvalue(), "", "the caller decides where a refusal is printed")
        self.assertIn("99", message)

    def test_too_few_summaries_names_the_count_and_the_floor(self):
        message = self._refusal(floor=10)
        self.assertIn("only 1 summaries", message)
        self.assertIn("floor 10", message)

    def test_an_unclustered_graph_is_refused_with_its_coverage(self):
        message = self._refusal(
            nodes=[{"id": "a", "community": 1}, {"id": "b"}, {"id": "c"}, {"id": "d"}],
            coverage=0.5,
        )
        self.assertIn("1 of 4", message)
        self.assertIn("25.0%", message)

    def test_coverage_counts_nodes_not_distinct_ids(self):
        """A merged graph repeats ids; collapsing them understates coverage
        enough to refuse a healthy graph."""
        repeated = [{"id": "a", "community": 1} for _ in range(4)]
        self.assertIsNone(self._refusal(nodes=repeated, coverage=0.9))

    def test_an_empty_graph_does_not_divide_by_zero(self):
        """Isolated from the snapshot guard, which an empty graph trips first
        whenever the snapshot is not also empty."""
        self.assertIsNone(self._refusal(nodes=[], new_community={}, old_members={}, coverage=0.9))

    def test_an_empty_graph_against_a_real_snapshot_is_the_wrong_snapshot(self):
        """Not a coverage failure - there is nothing to have coverage of. The
        useful thing to say is that these two files do not belong together."""
        self.assertIn("wrong snapshot", self._refusal(nodes=[], new_community={}, coverage=0.9))

    def test_a_snapshot_sharing_no_node_ids_is_refused_as_the_wrong_snapshot(self):
        message = self._refusal(old_members={"1": ["nowhere"]})
        self.assertIn("wrong snapshot", message)

    def test_an_empty_snapshot_is_not_treated_as_the_wrong_one(self):
        """Nothing to intersect is not evidence of a mismatch."""
        self.assertIsNone(self._refusal(old_members={}))


class ClaimTargetsTest(unittest.TestCase):
    """Pass 1 under `--carry overlap`: the best new cluster, and the ways one is lost.

    The tolerance rather than the shipped criterion, deliberately: `bar` and
    `precision` only decide anything under `overlap`, and the default is held by
    the tests that drive `remap()` itself and by `ExactClaimTargetsTest` below.
    """

    def _claim(self, summaries_in, old_members, new_community, bar=0.6, precision=0.0):
        """Returns (claims, displaced)."""
        return summaries._claim_targets(
            summaries_in, old_members, new_community, bar, precision, "overlap"
        )

    def test_a_dominant_target_is_claimed_with_its_share(self):
        claims, displaced = self._claim(
            {"1": "prose"}, {"1": ["a", "b", "c"]}, {"a": "9", "b": "9", "c": "8"}
        )
        self.assertEqual(claims, {"1": ("9", 2 / 3, 1.0, False)})
        self.assertEqual(displaced, {})

    def test_a_share_below_the_bar_is_displaced_with_the_target_it_missed(self):
        claims, displaced = self._claim(
            {"1": "prose"}, {"1": ["a", "b", "c"]}, {"a": "9", "b": "8", "c": "7"}
        )
        self.assertEqual(claims, {})
        self.assertEqual(displaced["1"]["reason"], "below-bar")
        self.assertEqual(
            displaced["1"]["best_target"],
            "9",
            "the near miss is the raw material for a backfill, so it must be recorded",
        )

    def test_a_summary_with_no_snapshot_entry_is_members_gone(self):
        _, displaced = self._claim({"1": "prose"}, {}, {"a": "9"})
        self.assertEqual(displaced["1"]["reason"], "members-gone")
        self.assertIsNone(displaced["1"]["best_target"])

    def test_members_that_no_longer_carry_a_community_are_members_gone(self):
        """A distinct branch from an absent snapshot entry: the members are
        known, but none of them landed anywhere in the new graph."""
        _, displaced = self._claim({"1": "prose"}, {"1": ["a", "b"]}, {"z": "9"})
        self.assertEqual(displaced["1"]["reason"], "members-gone")

    def test_the_share_is_measured_against_the_old_cluster_only(self):
        """This is #127 in one assertion. The share divides by the old
        membership, so it reports how much of the OLD cluster stayed together
        and says nothing about how much of the NEW cluster it now describes -
        here, one member of a cluster of ten. Pinning it so a fix has to change
        the test deliberately rather than by accident.
        """
        claims, _ = self._claim(
            {"1": "prose"},
            {"1": ["a"]},
            {"a": "9", **{f"other{i}": "9" for i in range(9)}},
        )
        self.assertEqual(
            claims["1"][:2], ("9", 1.0), "recall 1.0 despite describing a tenth of the cluster"
        )
        self.assertAlmostEqual(claims["1"][2], 0.1, msg="precision is the half that sees it")

    def test_prose_describing_a_corner_of_its_new_cluster_is_dropped(self):
        """Community 154 from a real refresh, reproduced.

        37 members grew to 458 with every old member retained: recall 1.00,
        clearing a 60% bar comfortably, precision 0.08. Not stale and not
        unsupported - confidently describing a small corner of something much
        larger, which a reader cannot detect.
        """
        old = {"154": [f"m{i}" for i in range(37)]}
        new = {
            **{f"m{i}": "9" for i in range(37)},
            **{f"other{i}": "9" for i in range(421)},
        }
        claims, displaced = self._claim({"154": "prose"}, old, new, bar=0.6, precision=0.2)
        self.assertEqual(claims, {})
        self.assertEqual(displaced["154"]["reason"], "below-precision")
        self.assertAlmostEqual(displaced["154"]["precision"], 0.081, places=3)
        self.assertEqual(
            displaced["154"]["share"],
            1.0,
            "recall stays perfect - which is exactly why the recall bar cannot see this",
        )

    def test_the_same_summary_is_carried_when_no_precision_floor_is_asked_for(self):
        """The previous behaviour, pinned: without a floor this is carried."""
        old = {"154": [f"m{i}" for i in range(37)]}
        new = {
            **{f"m{i}": "9" for i in range(37)},
            **{f"other{i}": "9" for i in range(421)},
        }
        claims, _ = self._claim({"154": "prose"}, old, new, bar=0.6, precision=0.0)
        self.assertIn("154", claims)

    def test_prose_describing_most_of_its_cluster_survives_the_floor(self):
        """The floor is deliberately low: 93.5% of one estate's carried
        summaries already sit at 80% precision or better, and re-authoring
        costs real money, so judgement calls are carried rather than dropped."""
        old = {"1": ["a", "b", "c"]}
        new = {"a": "9", "b": "9", "c": "9", "d": "9"}
        claims, displaced = self._claim({"1": "prose"}, old, new, bar=0.6, precision=0.2)
        self.assertIn("1", claims)
        self.assertEqual(displaced, {})

    def test_ordering_is_deterministic_for_a_stable_tiebreak(self):
        claims, _ = self._claim(
            {"10": "a", "9": "b", "2": "c"},
            {"10": ["x"], "9": ["y"], "2": ["z"]},
            {"x": "1", "y": "2", "z": "3"},
        )
        self.assertEqual(list(claims), ["2", "9", "10"])


class ExactClaimTargetsTest(unittest.TestCase):
    """Pass 1 under the shipped criterion: a claim about a set, tested as one."""

    def _claim(self, summaries_in, old_members, new_community, bar=0.6, precision=0.2):
        """Returns (claims, displaced). The bar and floor are passed deliberately:
        neither may decide anything under `exact`."""
        return summaries._claim_targets(
            summaries_in, old_members, new_community, bar, precision, "exact"
        )

    def test_a_community_holding_the_same_set_is_claimed_and_marked_identical(self):
        claims, displaced = self._claim(
            {"1": "prose"}, {"1": ["a", "b", "c"]}, {"a": "9", "b": "9", "c": "9"}
        )
        self.assertEqual(claims, {"1": ("9", 1.0, 1.0, True)})
        self.assertEqual(displaced, {})

    def test_a_community_holding_the_old_set_and_one_more_node_is_withdrawn(self):
        """One node is enough. A community that gained a node is a different set,
        therefore a different claim, therefore not the thing the prose describes -
        and a tolerance only moves the question of how much it may absorb
        somewhere the reader cannot see it."""
        claims, displaced = self._claim(
            {"1": "prose"}, {"1": ["a", "b", "c"]}, {"a": "9", "b": "9", "c": "9", "d": "9"}
        )
        self.assertEqual(claims, {})
        self.assertEqual(displaced["1"]["reason"], "not-identical")
        self.assertEqual(displaced["1"]["share"], 1.0, "recall cannot see this")
        self.assertEqual(displaced["1"]["precision"], 0.75, "and a 0.2 floor does not either")

    def test_a_member_that_left_the_graph_is_named_as_a_changed_set_not_a_near_miss(self):
        """The survivors are all together, so an overlap criterion reads this as
        a good carry at 0.67. The set the prose described no longer exists."""
        claims, displaced = self._claim(
            {"1": "prose"}, {"1": ["a", "b", "c"]}, {"a": "9", "b": "9"}
        )
        self.assertEqual(claims, {})
        self.assertEqual(displaced["1"]["reason"], "not-identical")

    def test_repeated_node_ids_do_not_read_as_an_identical_set(self):
        """Set equality, not recall 1.0 and precision 1.0.

        A merged graph can repeat a node id, so the snapshot's member list can
        too - and here both ratios reach 1.0 over a community holding a node the
        prose never described. The ratios are a neighbour of the quantity being
        claimed, and this is the case that tells them apart.
        """
        claims, displaced = self._claim({"1": "prose"}, {"1": ["a", "a"]}, {"a": "9", "b": "9"})
        self.assertEqual(claims, {}, "recall 1.0 and precision 1.0, and the sets still differ")
        self.assertEqual(displaced["1"]["reason"], "not-identical")

    def test_members_gone_is_still_reported_as_its_own_cause(self):
        _, displaced = self._claim({"1": "prose"}, {"1": ["a", "b"]}, {"z": "9"})
        self.assertEqual(displaced["1"]["reason"], "members-gone")

    def test_the_recall_bar_cannot_withhold_an_identical_set(self):
        """`exact` is the criterion, not an extra one stacked on the tolerance."""
        claims, _ = self._claim({"1": "prose"}, {"1": ["a"]}, {"a": "9"}, bar=1.0, precision=1.0)
        self.assertIn("1", claims)


class PrecisionDistributionTest(unittest.TestCase):
    """The reported distribution must describe the population it came from.

    Shipped in v0.11.4 with overlapping bands: `next()` over a descending list
    returns the first bound in list order, so the lower three bands all ended at
    0.8 and every summary below 80% was counted three times. On a real estate it
    reported 487 summaries from a population of 483, and claimed 2 sat under 20%
    while the drop count on the line above correctly said none did.

    The assertion here is the sum rather than the boundaries, deliberately: it
    catches this whole class without encoding where the bands happen to fall,
    and it would have failed on the shipped version without anyone knowing what
    the bug was.
    """

    def _reported(self, precisions: list) -> str:
        carried = {str(i): {"precision": p} for i, p in enumerate(precisions)}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            summaries._report_precision(carried)
        return out.getvalue()

    def _counts(self, text: str) -> list:
        return [int(n) for n in re.findall(r"(\d+) at ", text)]

    def test_the_bands_sum_to_the_population(self):
        precisions = [0.95] * 481 + [0.6, 0.65]
        counts = self._counts(self._reported(precisions))
        self.assertEqual(
            sum(counts),
            len(precisions),
            f"bands must partition the population, not overlap it: {counts}",
        )

    def test_a_value_lands_in_exactly_one_band(self):
        for value, expected in ((0.95, 0), (0.6, 1), (0.3, 2), (0.05, 3)):
            counts = self._counts(self._reported([value]))
            self.assertEqual(sum(counts), 1, f"{value} counted {sum(counts)} times")
            self.assertEqual(counts[expected], 1, f"{value} landed in the wrong band: {counts}")

    def test_a_boundary_value_is_not_double_counted(self):
        counts = self._counts(self._reported([0.8, 0.5, 0.2]))
        self.assertEqual(sum(counts), 3, f"boundaries double-counted: {counts}")

    def test_an_unmeasured_report_is_not_reported_as_perfect(self):
        """Entries written before precision was recorded have no such field.
        Defaulting them to 1.0 printed "5,405 at 80%+" on a real estate for a
        report that had measured nothing - a clean verdict over an absent
        measurement."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            summaries._report_precision({str(i): {"from": "0", "share": 0.9} for i in range(5405)})
        text = out.getvalue()
        self.assertIn("not recorded", text)
        self.assertNotIn("at 80%+", text)

    def test_a_partly_measured_report_says_how_much_it_measured(self):
        carried = {"1": {"precision": 0.9}, "2": {"from": "0"}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            summaries._report_precision(carried)
        self.assertIn("1 of 2", out.getvalue())

    def test_nothing_carried_reports_nothing(self):
        self.assertEqual(self._reported([]), "")


class RemapWriteGateTest(SettingsIsolated):
    """A remap that changed no prose writes nothing, and one that changed prose writes.

    `merge` grew this gate in #299 and `remap` did not, so the two stages
    disagreed about whether a no-op should touch a committed file (#313). An
    identity remap - unchanged clustering, every summary carried onto the id it
    already held, nothing withdrawn - rewrote every line of the artefact, and a
    whole-file diff that means nothing is what teaches a reviewer to skim the one
    that means something.

    Deliberately not a subclass of `RemapTest`: inheriting its fixture would
    inherit its tests too, and every inherited copy becomes another name the
    mutation table has to carry for the entries this module already observes.
    """

    # A coverage block, reconciled by hand: shown + unshown == total per field.
    # It survives an identity remap only because the write is skipped, which is
    # the point - remap cannot know a carried summary's evidence base.
    COVERAGE = {
        "top_nodes": {"shown": 2, "unshown": 3, "total": 5},
        "business_features": {"shown": 0, "unshown": 0, "total": 0},
        "tickets": {"shown": 1, "unshown": 0, "total": 1},
    }
    # A fixed point in the past, so "was this file rewritten?" is answered by the
    # filesystem rather than by comparing bytes a rewrite would reproduce.
    PINNED = 1_000_000_000
    # The community whose prose is watched across the remap. The rest are filler,
    # so the plausibility floor does not refuse the run.
    WATCHED = "7"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        (self.root / "graphify-out").mkdir(parents=True)
        self._old_root = config.ROOT
        self.addCleanup(config.configure, root=str(self._old_root))
        config.configure(root=str(self.root))

    # --- fixture -------------------------------------------------------------

    @classmethod
    def prose(cls, cid: str, note: str = "") -> str:
        """Prose long enough to clear `merge`'s length bound, keyed to its id."""
        return (
            f"Community {cid} groups the lookup and persistence paths that one "
            f"invented service reads at build time.{note}"
        )

    def members(self, watched: str = WATCHED) -> dict[str, list[str]]:
        """A clustering with more communities than the plausibility floor asks for."""
        return {watched: ["a", "b", "c"]} | {str(i): [f"x{i}"] for i in range(1000, 1030)}

    def write_graph(self, members: dict[str, list[str]]) -> None:
        config.GRAPH_PATH.write_text(json.dumps(_graph(members)), encoding="utf-8")

    def identity_store(self, note: str = "") -> dict[str, str]:
        """Snapshot and graph agreeing exactly, and the prose that belongs to them."""
        members = self.members()
        with gzip.open(config.SUMMARIES_SNAPSHOT_PATH, "wt", encoding="utf-8") as handle:
            json.dump(members, handle)
        self.write_graph(members)
        return {cid: self.prose(cid, note if cid == self.WATCHED else "") for cid in members}

    def recluster(self) -> None:
        """The re-cluster: the watched community keeps its nodes under a new id."""
        self.write_graph(self.members(watched="42"))

    def commit_through_merge(self, prose: dict[str, str]) -> None:
        """Commit `prose` through the real merge stage.

        The file under test is then the artefact `merge` writes, digest and all,
        rather than a hand-made stand-in for it - the disagreement pinned here is
        between two real stages.
        """
        digests = [
            {"id": cid} | ({"coverage": self.COVERAGE} if cid == self.WATCHED else {})
            for cid in prose
        ]
        config.SUMMARIES_INPUT_PATH.write_text(json.dumps(digests), encoding="utf-8")
        self.assertEqual(self.run_merge(prose, name="first.json")[0], 0)

    def run_remap(self, **kwargs) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = summaries.remap(**kwargs)
        return code, out.getvalue()

    def run_merge(self, prose: dict[str, str], name: str = "again.json") -> tuple[int, str]:
        batch = self.root / name
        batch.write_text(json.dumps(prose), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = summaries.merge([str(batch)])
        return code, out.getvalue()

    def pin(self) -> None:
        self.assertTrue(config.SUMMARIES_PATH.is_file(), "nothing was committed to remap")
        os.utime(config.SUMMARIES_PATH, (self.PINNED, self.PINNED))

    def rewritten(self) -> bool:
        return int(config.SUMMARIES_PATH.stat().st_mtime) != self.PINNED

    def body(self) -> dict:
        return store_io.read_summaries(config.SUMMARIES_PATH)

    # --- the gate ------------------------------------------------------------

    def test_an_identity_remap_does_not_rewrite_the_committed_file(self):
        """The break: a remap that carries every summary onto the id it already
        holds rewriting the file anyway, so an operator reviewing a re-cluster
        cannot tell an identity remap from a real one at a glance."""
        prose = self.identity_store()
        self.commit_through_merge(prose)
        before = config.SUMMARIES_PATH.read_bytes()
        self.pin()

        code, output = self.run_remap()

        self.assertEqual(code, 0)
        self.assertFalse(self.rewritten(), "no prose moved, so nothing may be written")
        self.assertEqual(config.SUMMARIES_PATH.read_bytes(), before)
        self.assertIn("not rewritten", output)
        # Not a refusal wearing a skip: the same run carried all 31 and withdrew none.
        self.assertIn(f"Retained {len(prose)} of {len(prose)} summaries (100%), withdrew 0", output)

    def test_the_skipped_write_leaves_the_coverage_merge_recorded(self):
        """The break: losing the evidence base on a no-op. The metadata block is
        what a reader subtracts from to see when they are reading a sample, and
        an identity remap has no grounds to disturb it."""
        prose = self.identity_store()
        self.commit_through_merge(prose)
        self.pin()

        self.assertEqual(self.run_remap()[0], 0)

        document = json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            document[store_io.SUMMARIES_METADATA_KEY]["coverage"][self.WATCHED], self.COVERAGE
        )

    def test_a_remap_that_moves_an_id_still_rewrites_the_file(self):
        """The over-correction this exists to catch: "skip the rewrite" becoming
        "never rewrite" looks exactly like success from the test above, and would
        strand every committed summary on ids the graph no longer uses."""
        prose = self.identity_store()
        self.commit_through_merge(prose)
        self.recluster()
        self.pin()

        code, output = self.run_remap()

        self.assertEqual(code, 0)
        self.assertTrue(self.rewritten(), "an id moved, so the file must be written")
        self.assertEqual(self.body()["42"], prose[self.WATCHED])
        self.assertNotIn(self.WATCHED, self.body())
        self.assertNotIn("not rewritten", output)

    def test_a_second_remap_of_the_same_clustering_writes_nothing(self):
        """The break: remap recording no digest of what it wrote, so its own
        output is unrecognisable to it and every later run rewrites the file. A
        store whose file predates the digest normalises it once, not once a run."""
        prose = self.identity_store()
        # Two-space indentation and no metadata block: a file no `merge` of this
        # library wrote, which is the shape the reporting store had.
        config.SUMMARIES_PATH.write_text(json.dumps(prose, indent=2), encoding="utf-8")

        self.assertEqual(self.run_remap()[0], 0)
        first = config.SUMMARIES_PATH.read_bytes()
        self.pin()

        code, output = self.run_remap()

        self.assertEqual(code, 0)
        self.assertFalse(self.rewritten(), "remap must recognise the file it wrote itself")
        self.assertEqual(config.SUMMARIES_PATH.read_bytes(), first)
        self.assertIn("not rewritten", output)

    def test_a_merge_after_a_remap_recognises_the_digest_the_remap_recorded(self):
        """The break: the two stages computing or recording the digest
        differently, so a merge that adds no prose rewrites the whole file
        directly after a remap - the churn moved rather than removed."""
        prose = self.identity_store()
        config.SUMMARIES_PATH.write_text(json.dumps(prose), encoding="utf-8")
        config.SUMMARIES_INPUT_PATH.write_text(
            json.dumps([{"id": cid} for cid in prose]), encoding="utf-8"
        )
        self.assertEqual(self.run_remap()[0], 0)
        after_remap = config.SUMMARIES_PATH.read_bytes()
        self.pin()

        code, output = self.run_merge(prose)

        self.assertEqual(code, 0, output)
        self.assertFalse(self.rewritten(), "the prose is the prose already committed")
        self.assertEqual(config.SUMMARIES_PATH.read_bytes(), after_remap)
        self.assertIn("not rewritten", output)

    def test_non_ascii_prose_survives_a_remap_as_merge_wrote_it(self):
        """The second defect underneath #313: `remap` wrote with the default
        `ensure_ascii` and `merge` with `ensure_ascii=False`, so a remap escaped
        every non-ASCII character a merge had written literally and the next
        merge unescaped it again. Alternating the two stages churned the file on
        its own, over prose neither had changed."""
        prose = self.identity_store(note=" Uses the naming café — an invented one.")
        self.commit_through_merge(prose)
        self.assertIn("café", config.SUMMARIES_PATH.read_text(encoding="utf-8"))
        self.recluster()

        self.assertEqual(self.run_remap()[0], 0)

        text = config.SUMMARIES_PATH.read_text(encoding="utf-8")
        self.assertIn("café — an invented one", text, "remap must not escape what merge did not")
        self.assertNotIn("\\u", text)


if __name__ == "__main__":
    unittest.main()
