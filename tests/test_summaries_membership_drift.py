"""Whether the membership snapshot still describes the committed graph.

The store writes the snapshot, requires it, and reports summary counts derived
from it — and nothing compared the two. Community ids are positional, so a
summary is bound to a *number*; only the snapshot binds it to a member set.
Rebuild or re-cluster without refreshing the snapshot and every summary stays
attached to a community it no longer describes, with no outward sign: every
community still has a summary and every summary still has a community, so the
coverage line reads the same either way.

`_remap_refusal` cannot see it. It refuses when the snapshot and the graph share
*no* node ids, and a snapshot taken from a stale graph shares *every* id with
that same stale file — consistently wrong is the one case that guard is blind to.

Each test names the production change that should make it fail.
"""

from __future__ import annotations

import contextlib
import gzip
import io as stdio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import status  # noqa: E402


def _nodes(members: dict[str, list[str]], unclustered: list[str] | None = None) -> list[dict]:
    """Graph nodes where `members` maps community id -> node ids."""
    nodes = [
        {"id": node, "label": node, "community": int(cid)}
        for cid, ids in members.items()
        for node in ids
    ]
    nodes += [{"id": node, "label": node} for node in unclustered or []]
    return nodes


class MembershipDrift(unittest.TestCase):
    """The comparison itself, driven directly so the arithmetic is hand-checkable."""

    def test_a_community_that_kept_its_members_is_attached(self):
        """Catches a check that reports drift on a graph nothing moved in.

        A guard that fires on the healthy case is suppressed within a week, and a
        suppressed check is worse than an absent one.
        """
        snapshot = {"1": ["a", "b", "c", "d"]}
        result = summaries.membership_drift(
            {"1": "prose"}, snapshot, _nodes({"1": ["a", "b", "c", "d"]})
        )
        self.assertIsNone(result["cause"])
        self.assertEqual([entry["id"] for entry in result["attached"]], ["1"])
        self.assertEqual(result["adrift"], [])
        self.assertEqual(result["unsnapshotted"], [])

    def test_a_community_whose_members_scattered_is_adrift(self):
        """Catches the defect the issue reports: nothing compares the two at all.

        Community 1 held four nodes when the prose was written; the graph now
        files one of them there and the other three elsewhere. 1 of 4 is 25%,
        under the 60% carry bar, so the prose is keyed to a set that moved.
        Remove the comparison and this is the test that notices.
        """
        snapshot = {"1": ["a", "b", "c", "d"]}
        graph = _nodes({"1": ["a"], "2": ["b", "c", "d"]})
        result = summaries.membership_drift({"1": "prose", "2": "other"}, snapshot, graph)
        self.assertEqual([entry["id"] for entry in result["adrift"]], ["1"])
        self.assertEqual(result["adrift"][0]["share"], 0.25)
        self.assertEqual(result["adrift"][0]["was"], 4)
        self.assertEqual(result["adrift"][0]["size"], 1)

    def test_the_bar_is_the_remap_carry_bar_not_strict_equality(self):
        """Catches a guard stricter than the tool it guards.

        Three of four members still filed under the id is 75%: over `remap`'s 60%
        carry bar, so `remap` would carry this prose unchanged. A check using set
        equality would call it adrift and send someone re-authoring prose that was
        never broken. Replace the `< bar` comparison with `!=` on the sets and this
        fails.
        """
        snapshot = {"1": ["a", "b", "c", "d"]}
        graph = _nodes({"1": ["a", "b", "c"], "2": ["d"]})
        result = summaries.membership_drift({"1": "prose", "2": "other"}, snapshot, graph)
        self.assertEqual(result["adrift"], [])
        self.assertEqual([entry["id"] for entry in result["attached"]], ["1"])
        self.assertEqual(result["attached"][0]["share"], 0.75)

    def test_a_community_that_grew_around_its_members_is_narrowed_not_attached(self):
        """Catches a recall-only verdict, which is how prose ends up describing a corner.

        Both snapshot members are still filed under community 1, so recall is
        100% — and the community now holds twenty nodes, so the prose describes
        10% of what a reader sees, under the 20% floor. Reported as its own
        population rather than conflated with adrift, because the responses
        differ: this prose is not mis-keyed, it is incomplete.
        """
        snapshot = {"1": ["a", "b"]}
        graph = _nodes({"1": ["a", "b"] + [f"n{i}" for i in range(18)]})
        result = summaries.membership_drift({"1": "prose"}, snapshot, graph, precision=0.2)
        self.assertEqual(result["adrift"], [])
        self.assertEqual(result["attached"], [])
        self.assertEqual([entry["id"] for entry in result["narrowed"]], ["1"])
        self.assertEqual(result["narrowed"][0]["precision"], 0.1)

    def test_a_summary_with_no_snapshot_entry_is_reported_not_skipped(self):
        """Catches `if cid in snapshot` narrowing the population in silence.

        A summary with no snapshot entry can be neither checked nor re-keyed — a
        remap cannot even withdraw it — so filtering it out narrows what was
        examined and then reports a count as though it had covered everything.
        The populations must reconcile against the committed summaries.
        """
        snapshot = {"1": ["a", "b"]}
        graph = _nodes({"1": ["a", "b"], "7": ["z"]})
        result = summaries.membership_drift({"1": "prose", "7": "orphan"}, snapshot, graph)
        self.assertEqual(result["unsnapshotted"], ["7"])
        checked = len(result["attached"]) + len(result["adrift"]) + len(result["narrowed"])
        self.assertEqual(checked + len(result["unsnapshotted"]), 2)

    def test_an_empty_snapshot_entry_counts_as_unsnapshotted_not_as_total_loss(self):
        """Catches an absent measurement reading as a measured 0%.

        A community recorded with an empty member list was never measured. Scoring
        it 0.0 and filing it under adrift reports a clean verdict over nothing,
        which is the shape of this repository's most expensive mistakes.
        """
        result = summaries.membership_drift({"1": "prose"}, {"1": []}, _nodes({"1": ["a", "b"]}))
        self.assertEqual(result["adrift"], [])
        self.assertEqual(result["unsnapshotted"], ["1"])

    def test_no_membership_read_is_named_as_its_own_cause(self):
        """Catches "every summary is adrift" being reported for a one-line read failure.

        graphify carries the assignment in `community`. Rename that key, or let a
        clustering step print success without persisting its result, and every
        comparison fails — which reads as catastrophic drift and would send
        someone re-authoring a whole store. It needs the opposite response to
        moved membership, so it is a separate cause and the message says not to
        act on it as drift.
        """
        graph = _nodes({}, unclustered=[f"n{i}" for i in range(10)])
        result = summaries.membership_drift({"1": "prose"}, {"1": ["a", "b"]}, graph)
        self.assertEqual(result["cause"], "no-membership")
        self.assertEqual(result["adrift"], [])
        self.assertIn("no membership having been read", result["message"])
        self.assertIn("Do not re-author prose", result["message"])

    def test_a_snapshot_sharing_no_ids_with_the_graph_is_the_wrong_snapshot(self):
        """Catches a wrong snapshot reported as total drift.

        The same condition `_remap_refusal` refuses on. Every summary compares as
        adrift, and the fix is to re-take the snapshot, not to re-author prose.
        """
        result = summaries.membership_drift(
            {"1": "prose"}, {"1": ["a", "b"]}, _nodes({"1": ["x", "y"]})
        )
        self.assertEqual(result["cause"], "wrong-snapshot")
        self.assertEqual(result["adrift"], [])
        self.assertIn("wrong snapshot", result["message"])

    def test_an_empty_graph_is_named_rather_than_reported_as_clean(self):
        """Catches a check over nothing exiting as though it had checked something."""
        result = summaries.membership_drift({"1": "prose"}, {"1": ["a"]}, [])
        self.assertEqual(result["cause"], "no-graph")
        self.assertEqual(result["attached"], [])

    def test_the_snapshot_taken_from_a_stale_graph_is_caught(self):
        """Catches the exact blind spot `_remap_refusal` has.

        The snapshot shares every node id with the graph, so the shared-ids guard
        passes; the clustering has moved, so the prose is mis-keyed. That
        combination is what nothing in the library could detect.
        """
        stale = {"1": ["a", "b", "c", "d"], "2": ["e", "f", "g", "h"]}
        # Same nodes, re-clustered: 1 and 2 swapped halves.
        graph = _nodes({"1": ["a", "e", "f", "g"], "2": ["b", "c", "d", "h"]})
        result = summaries.membership_drift({"1": "one", "2": "two"}, stale, graph)
        self.assertIsNone(result["cause"], "the shared-ids guard must not fire here")
        self.assertEqual([entry["id"] for entry in result["adrift"]], ["1", "2"])

    def _partly_namespaced(self):
        """One community namespaced `<repo>::<id>`, one bare, snapshot all bare.

        The partial case, which is the one that looks legitimate: enough ids still
        match for `wrong-snapshot` not to fire.
        """
        snapshot = {
            "1": [f"a{i}" for i in range(6)],
            "2": [f"b{i}" for i in range(4)],
        }
        graph = _nodes(
            {
                "1": [f"repo::a{i}" for i in range(6)],
                "2": [f"b{i}" for i in range(4)],
            }
        )
        return {"1": "one", "2": "two"}, snapshot, graph

    def test_ids_are_compared_exactly_as_the_remap_compares_them(self):
        """Catches a check loosened to strip `<repo>::`, which `remap` never does.

        `_claim_targets` looks each snapshot member up in a dict keyed by raw
        `node["id"]`, so a namespaced graph id does not match a bare snapshot id
        there either. A check that stripped the prefix would report this prose as
        sound while `remap` drops it — a guard looser than the tool it guards. In
        a merged estate two repositories can also hold the same local id, so
        stripping the prefix merges distinct nodes into one member.
        """
        result = summaries.membership_drift(*self._partly_namespaced())
        self.assertIsNone(result["cause"], "enough ids match that this is not a wrong snapshot")
        self.assertEqual([entry["id"] for entry in result["adrift"]], ["1"])
        self.assertEqual(result["adrift"][0]["share"], 0.0)
        self.assertEqual([entry["id"] for entry in result["attached"]], ["2"])

    def test_an_id_space_mismatch_is_named_rather_than_silently_corrected(self):
        """Catches drift reported without saying it may be an id-space mismatch.

        A first implementation of this check elsewhere reported communities adrift
        that were not, because the snapshot's ids were bare and the graph's were
        namespaced. Naming it is what stops the same reading here — and what stops
        the fix being a looser comparison.
        """
        result = summaries.membership_drift(*self._partly_namespaced())
        note = summaries._namespace_note(result)
        self.assertIn("different id", note)
        self.assertIn("prefix stripped is not the fix", note)

    def test_matching_id_spaces_produce_no_mismatch_note(self):
        """Catches a note that fires on every run, which is a note nobody reads."""
        graph = _nodes({"1": ["a", "b"], "2": ["c"]})
        result = summaries.membership_drift({"1": "prose"}, {"1": ["a", "b"]}, graph)
        self.assertEqual(summaries._namespace_note(result), "")

    def test_communities_are_reported_in_a_stable_order(self):
        """Catches a report whose line order changes between runs on one input.

        The result is read by people and diffed by machines; iterating a dict
        keyed by unordered data makes two runs on the same graph disagree.
        """
        snapshot = {str(cid): [f"n{cid}"] for cid in (2, 10, 9, 100, 1)}
        graph = _nodes({str(cid): [f"other{cid}"] for cid in (2, 10, 9, 100, 1)})
        graph += [{"id": f"n{cid}", "label": "x", "community": 999} for cid in (2, 10, 9, 100, 1)]
        result = summaries.membership_drift(
            {str(cid): "prose" for cid in (2, 10, 9, 100, 1)}, snapshot, graph
        )
        self.assertEqual([entry["id"] for entry in result["adrift"]], ["1", "2", "9", "10", "100"])


class AdriftStage(SettingsIsolated):
    """The stage end to end: real files, the real streamed graph read, real exit codes."""

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

    def write_graph(self, members, unclustered=None, path=None):
        graph = {
            "directed": False,
            "multigraph": False,
            "graph": {},
            "nodes": _nodes(members, unclustered),
            "links": [],
        }
        (path or config.GRAPH_PATH).write_text(json.dumps(graph), encoding="utf-8")

    def write_gzipped_graph(self, members):
        graph = {"nodes": _nodes(members), "links": []}
        with gzip.open(str(config.GRAPH_PATH) + ".gz", "wt", encoding="utf-8") as handle:
            json.dump(graph, handle)

    def write_snapshot(self, members):
        with gzip.open(config.SUMMARIES_SNAPSHOT_PATH, "wt", encoding="utf-8") as handle:
            json.dump(members, handle)

    def write_summaries(self, mapping):
        config.SUMMARIES_PATH.write_text(json.dumps(mapping), encoding="utf-8")

    def run_adrift(self, **kwargs):
        out, err = stdio.StringIO(), stdio.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = summaries.adrift(**kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_drift_exits_non_zero_so_a_refresh_can_gate_on_it(self):
        """Catches a check that reports drift and exits 0.

        A stage nobody can gate on is a stage nobody runs twice. Exit 1 is drift;
        exit 2 is reserved for the check not having run.
        """
        self.write_summaries({"1": "prose", "2": "other"})
        self.write_snapshot({"1": ["a", "b", "c", "d"], "2": ["e", "f", "g", "h"]})
        self.write_graph({"1": ["a", "e", "f", "g"], "2": ["b", "c", "d", "h"]})
        code, out, _ = self.run_adrift()
        self.assertEqual(code, 1)
        self.assertIn("community 1", out)
        self.assertIn("Adrift", out)

    def test_a_healthy_store_exits_zero(self):
        """Catches a stage that can only fail, which is a stage that gets switched off."""
        self.write_summaries({"1": "prose"})
        self.write_snapshot({"1": ["a", "b", "c", "d"]})
        self.write_graph({"1": ["a", "b", "c", "d"]})
        code, out, _ = self.run_adrift()
        self.assertEqual(code, 0, out)
        self.assertIn("1 of 1 checked summaries", out)

    def test_the_report_reconciles_its_populations_against_the_committed_summaries(self):
        """Catches a count that describes a narrower population than it claims.

        Checked plus with-no-snapshot-entry must equal the committed summaries;
        a tool's own count is not verification.
        """
        self.write_summaries({"1": "prose", "2": "other", "3": "third"})
        self.write_snapshot({"1": ["a", "b"], "2": ["c", "d"]})
        self.write_graph({"1": ["a", "b"], "2": ["c", "d"], "3": ["e"]})
        code, out, _ = self.run_adrift()
        self.assertEqual(code, 1)
        self.assertIn("Reconciles: 2 checked + 1 with no snapshot entry = 3", out)
        self.assertIn("neither checked nor re-keyed", out)

    def test_the_check_names_the_graph_file_it_read(self):
        """Catches a count whose reader has to guess which of two graphs it describes.

        A store gitignores `graph.json` and commits `graph.json.gz`, so on any
        checkout the two can disagree. A silence about which one was read is what
        made an operator diff them by hand.
        """
        self.write_summaries({"1": "prose"})
        self.write_snapshot({"1": ["a", "b"]})
        self.write_graph({"1": ["a", "b"]})
        _, out, _ = self.run_adrift()
        self.assertIn("graph.json;", out)

    def test_the_committed_archive_is_read_when_the_plain_graph_is_absent(self):
        """Catches the check being unrunnable on a fresh checkout.

        `graph.json` is gitignored, so after a clone only the `.gz` exists.
        Stopping there would make this check unavailable exactly where an operator
        wants it, and streaming the archive costs the same.
        """
        self.write_summaries({"1": "prose"})
        self.write_snapshot({"1": ["a", "b", "c", "d"]})
        self.write_gzipped_graph({"1": ["a", "b", "c", "d"]})
        code, out, err = self.run_adrift()
        self.assertEqual(code, 0, err)
        self.assertIn("graph.json.gz", out)

    def test_a_refusal_message_names_the_file_that_was_actually_read(self):
        """Catches a message blaming `graph.json` for something read from the `.gz`.

        A store has two graph files. On a checkout only the committed archive
        exists, so a message hard-coded to `config.GRAPH_PATH` sends the operator
        to a file that is not there — the same class of silence that made someone
        diff two graphs by hand.
        """
        self.write_summaries({"1": "prose"})
        self.write_snapshot({"1": ["a", "b"]})
        self.write_gzipped_graph({"9": ["x", "y"]})
        code, _, err = self.run_adrift()
        self.assertEqual(code, 2)
        self.assertIn("the snapshot and graph.json.gz share no node ids", err)

    def test_a_missing_snapshot_exits_two_rather_than_reporting_a_clean_store(self):
        """Catches "no snapshot" being reported as no drift.

        Nothing to compare against is not a clean result, and it needs a different
        response from drift — hence the separate exit code.
        """
        self.write_summaries({"1": "prose"})
        self.write_graph({"1": ["a", "b"]})
        code, _, err = self.run_adrift()
        self.assertEqual(code, 2)
        self.assertIn("No membership snapshot", err)

    def test_no_membership_in_the_graph_exits_two_and_names_the_cause(self):
        """Catches an unreadable membership answered by rewriting an estate's prose."""
        self.write_summaries({"1": "prose"})
        self.write_snapshot({"1": ["a", "b"]})
        self.write_graph({}, unclustered=[f"n{i}" for i in range(10)])
        code, out, err = self.run_adrift()
        self.assertEqual(code, 2)
        self.assertIn("Cannot check membership", err)
        self.assertNotIn("Adrift", out)

    def test_a_stale_counterpart_graph_is_named(self):
        """Catches this check reporting on communities the store does not ship.

        The uncompressed graph is a leftover from a discarded run; the committed
        `.gz` holds a different clustering. Every count reconciles and describes
        the wrong artefact.
        """
        self.write_summaries({"1": "prose"})
        self.write_snapshot({"1": ["a", "b", "c", "d"]})
        self.write_graph({"1": ["a", "b", "c", "d"]})
        self.write_gzipped_graph({"1": ["a", "b"], "2": ["c", "d"]})
        _, out, _ = self.run_adrift()
        self.assertIn("MISMATCH", out)

    def test_the_subcommand_is_reachable_and_takes_its_flags(self):
        """Catches a check that exists and is wired to nothing.

        `summaries adrift` is how an operator reaches it; a helper nobody can call
        detects nothing.
        """
        self.write_summaries({"1": "prose"})
        self.write_snapshot({"1": ["a", "b", "c", "d"]})
        self.write_graph({"1": ["a", "b", "c"], "2": ["d"]})
        out = stdio.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(stdio.StringIO()):
            lenient = summaries.main(["adrift"])
            strict = summaries.main(["adrift", "--bar", "0.9"])
        self.assertEqual(lenient, 0, out.getvalue())
        self.assertEqual(strict, 1, "--bar did not reach the comparison")


class StatusNamesItsBlindSpot(SettingsIsolated):
    """`status` counts summaries and cannot check them; it must say which.

    The count is identical whether the prose still describes its community or
    not, and an operator read exactly that line as healthy. `status` must not read
    the graph, so the honest thing it can do is name the gap and point at the
    check rather than leave a green line to be read as a verdict.
    """

    def _run(self, snapshot: bool) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "knowledge" / "summaries").mkdir(parents=True)
        summaries_path = root / "knowledge" / "summaries" / "communities.json"
        summaries_path.write_text(json.dumps({"1": "prose"}), encoding="utf-8")
        snapshot_path = root / "knowledge" / "summaries" / "membership-snapshot.json.gz"
        if snapshot:
            with gzip.open(snapshot_path, "wt", encoding="utf-8") as handle:
                json.dump({"1": ["a"]}, handle)
        provenance_path = root / "provenance.json"
        provenance_path.write_text(json.dumps({"repositories": {}}), encoding="utf-8")
        config.configure(
            ROOT=root,
            PROVENANCE_PATH=provenance_path,
            SUMMARIES_PATH=summaries_path,
            SUMMARIES_SNAPSHOT_PATH=snapshot_path,
            SUMMARIES_INPUT_PATH=root / "digests.json",
            INTENT_INDEX_PATH=root / "file-tickets.json.gz",
            TOPICS_BRIEFS_PATH=root / "briefs.json",
            TOPICS_CONFIG_PATH=root / "topics.txt",
        )
        self.addCleanup(setattr, status, "run_git", status.run_git)
        status.run_git = lambda arguments: ""
        out = stdio.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(stdio.StringIO()):
            self.assertEqual(status.main([]), 0, "status must never return non-zero")
        return out.getvalue()

    def test_status_says_the_summary_count_does_not_mean_the_prose_still_fits(self):
        """Catches the coverage line being left to read as a verdict.

        Remove the pointer and `status` is back to reporting the same count for a
        store whose prose has been silently re-pointed and one whose has not.
        """
        text = self._run(snapshot=True)
        self.assertIn("Summary membership: not checked here", text)
        self.assertIn("summaries adrift", text)

    def test_a_missing_snapshot_is_named_where_the_count_is_reported(self):
        """Catches "no snapshot" going unreported next to a coverage number.

        With no snapshot, nothing anywhere can bind that prose to a member set —
        so the coverage figure cannot be checked even in principle.
        """
        text = self._run(snapshot=False)
        self.assertIn("no snapshot at", text)
        self.assertIn("summaries snapshot", text)

    def test_no_pointer_when_the_store_has_no_summaries(self):
        """Catches nagging a store that has no prose to be wrong about."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        config.configure(
            ROOT=root,
            SUMMARIES_PATH=root / "communities.json",
            SUMMARIES_SNAPSHOT_PATH=root / "membership-snapshot.json.gz",
        )
        self.assertEqual(status.snapshot_pointer(), "")


if __name__ == "__main__":
    unittest.main()
