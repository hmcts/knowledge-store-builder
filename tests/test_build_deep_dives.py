"""Deep-dive evidence bundles and dossier merging."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import build_deep_dives as dives


def node(nid, repo, label, community, source_file=None, kind=None):
    return {
        "id": nid,
        "repo": repo,
        "label": label,
        "community": community,
        "source_file": source_file,
        "metadata": {"kind": kind} if kind else {},
    }


GRAPH = {
    "nodes": [
        node("t::a", "target", "CaseAggregate", 1, "src/CaseAggregate.java"),
        node("t::b", "target", "HearingAggregate", 1, "src/HearingAggregate.java"),
        node("t::c", "target", "progression.case.json", 2, "raml/progression.case.json"),
        node("o::c", "other", "progression.case.json", 7, "schema/progression.case.json"),
        node("o::x", "other", "Unrelated", 8, "src/Unrelated.java"),
        node(
            "e::f", "e2e", "Progress a case", 9, "features/progress.feature", kind="gherkin_feature"
        ),
    ],
    "links": [
        {"source": "t::a", "target": "t::b"},
        {"source": "t::a", "target": "t::c"},
    ],
}
GRAPH["nodes"][5]["metadata"]["tickets"] = ["DD-1"]

LABELS = {"1": "Case handling", "2": "Case schema"}
SUMMARIES = {"1": "The case handling cluster."}
INTENT_FILES = {
    "src/CaseAggregate.java": {
        "tickets": {"DD-1": 3, "DD-2": 1, "DD-3": 1},
        "first": "2020-01-01",
        "last": "2026-07-01",
    },
    "src/HearingAggregate.java": {
        "tickets": {"DD-1": 1, "DD-2": 2},
        "first": "2021-01-01",
        "last": "2026-06-01",
    },
    "pom.xml": {"tickets": {"DD-3": 1}, "first": "2020-01-01", "last": "2020-02-01"},
}
DESCRIPTIONS = {
    "DD-1": {"d": ["Fix defect in case progression"], "first": "2020-03-04"},
    "DD-2": {"d": ["Revert hearing change"], "first": "2021-05-06"},
    "DD-3": {"d": ["Add feature toggles"], "first": "2020-07-08"},
}


class ScaleTest(SettingsIsolated):
    def test_counts_nodes_communities_and_summarised_top(self):
        got = dives.scale_section(GRAPH, "target", LABELS, SUMMARIES)
        self.assertEqual(got["nodes"], 3)
        self.assertAlmostEqual(got["share"], 3 / 6)
        self.assertEqual(got["communities"], 2)
        top = got["top_communities"][0]
        self.assertEqual((top["id"], top["label"], top["size"]), (1, "Case handling", 2))
        self.assertEqual(top["summary"], "The case handling cluster.")
        self.assertIsNone(got["top_communities"][1]["summary"])


class ChurnTest(SettingsIsolated):
    def test_orders_files_by_distinct_tickets(self):
        got = dives.churn_section(INTENT_FILES)
        self.assertEqual(got["files_with_history"], 3)
        self.assertEqual(got["top_files"][0]["path"], "src/CaseAggregate.java")
        self.assertEqual(got["top_files"][0]["tickets"], 3)


class InstabilityTest(SettingsIsolated):
    def test_measures_revert_and_fix_shares_with_samples(self):
        tickets = {"DD-1", "DD-2", "DD-3"}
        got = dives.instability_section(tickets, DESCRIPTIONS)
        self.assertEqual(got["tickets"], 3)
        self.assertAlmostEqual(got["revert_share"], 1 / 3)
        self.assertAlmostEqual(got["fix_share"], 1 / 3)
        self.assertEqual(got["sample_reverts"], ["DD-2: Revert hearing change"])

    def test_timeline_buckets_by_first_seen_year(self):
        got = dives.timeline_section({"DD-1", "DD-2", "DD-3"}, DESCRIPTIONS)
        self.assertEqual(got, {"2020": 2, "2021": 1})


class RepoTicketsTest(SettingsIsolated):
    def test_union_of_file_tickets(self):
        self.assertEqual(dives.repo_tickets(INTENT_FILES), {"DD-1", "DD-2", "DD-3"})


class CochangeTest(SettingsIsolated):
    def _files(self, pairs_count):
        # DD-n tickets each touching both files -> co-change support
        tickets = {f"DD-{i}": 1 for i in range(pairs_count)}
        return {
            "src/A.java": {"tickets": dict(tickets)},
            "src/B.java": {"tickets": dict(tickets)},
            "src/ATest.java": {"tickets": dict(tickets)},
        }

    def test_pairs_meet_threshold_and_test_pairs_are_excluded(self):
        self.addCleanup(setattr, dives, "MIN_COCHANGE", config.DIVE_MIN_COCHANGE)
        config.configure(DIVE_MIN_COCHANGE=10)
        got = dives.cochange_section(self._files(12))
        self.assertIn({"a": "src/A.java", "b": "src/B.java", "n": 12}, got)
        pairs = {(p["a"], p["b"]) for p in got}
        self.assertNotIn(("src/A.java", "src/ATest.java"), pairs)
        self.assertIn(("src/A.java", "src/B.java"), pairs)

    def test_sweeping_tickets_are_ignored(self):
        files = {f"f{i}.java": {"tickets": {"BIG-1": 1}} for i in range(60)}
        self.assertEqual(dives.cochange_section(files), [])


class CouplingSurfaceTest(SettingsIsolated):
    def test_shared_schema_labels_name_the_other_repos(self):
        got = dives.coupling_surface(GRAPH, "target")
        self.assertEqual(got, [{"label": "progression.case.json", "other_repos": ["other"]}])


class FeatureSectionTest(SettingsIsolated):
    def test_features_sharing_tickets_are_linked(self):
        got = dives.feature_section(GRAPH, {"DD-1", "DD-9"})
        self.assertEqual(got, [{"label": "Progress a case", "tickets": ["DD-1"]}])


class HotspotTest(SettingsIsolated):
    def test_high_churn_high_degree_files_flagged(self):
        got = dives.hotspot_section(INTENT_FILES, GRAPH, "target")
        paths = [h["path"] for h in got]
        self.assertIn("src/CaseAggregate.java", paths)  # churn 3, degree 2
        self.assertNotIn("pom.xml", paths)  # churn 1, no nodes


class ExtractTest(SettingsIsolated):
    def test_extract_writes_a_complete_bundle(self):
        import gzip
        import json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config.configure(
                GRAPH_PATH=root / "graph.json",
                LABELS_PATH=root / "labels.json",
                INTENT_INDEX_PATH=root / "intent.json.gz",
                TICKET_DESCRIPTIONS_PATH=root / "desc.json.gz",
                SUMMARIES_PATH=root / "summaries.json",
                DEEPDIVES_INPUT_DIR=root / "deep-dives",
                PROVENANCE_PATH=root / "provenance.json",
            )
            (root / "graph.json").write_text(json.dumps(GRAPH))
            (root / "labels.json").write_text(json.dumps(LABELS))
            (root / "summaries.json").write_text(json.dumps(SUMMARIES))
            with gzip.open(root / "intent.json.gz", "wt") as f:
                json.dump({"target": INTENT_FILES}, f)
            with gzip.open(root / "desc.json.gz", "wt") as f:
                json.dump(DESCRIPTIONS, f)
            self.assertEqual(dives.extract("target"), 0)
            bundle = json.loads((root / "deep-dives" / "target-input.json").read_text())
        for key in (
            "repo",
            "provenance",
            "scale",
            "churn",
            "instability",
            "timeline",
            "cochange",
            "hotspots",
            "coupling_surface",
            "features",
            "summary_coverage",
        ):
            self.assertIn(key, bundle)
        self.assertEqual(bundle["repo"], "target")

    def test_extract_unknown_repo_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            import json

            root = Path(tmp)
            self.addCleanup(setattr, dives, "GRAPH_PATH", config.GRAPH_PATH)
            config.configure(GRAPH_PATH=root / "graph.json")
            (root / "graph.json").write_text(json.dumps({"nodes": [], "links": []}))
            self.assertEqual(dives.extract("nope"), 1)


def _wire_deep_dive_paths(root: Path) -> None:
    """Point the deep-dive IO at a scratch tree - the merge()/main() seam under
    test. SettingsIsolated puts the settings back afterwards."""
    config.configure(
        DEEPDIVES_INPUT_DIR=root / "in",
        DEEPDIVES_DOCS_DIR=root / "docs",
        DEEPDIVES_PATH=root / "in" / "dives.json",
    )
    (root / "in").mkdir()
    (root / "docs").mkdir()


def _write_mixed_dive_batch(root: Path) -> None:
    """One valid, stamped dossier ('good'); one repo with a bundle but no
    dossier at all ('ghost'); one dossier that is too short ('short'). The
    three cases merge() must tell apart in a single pass, without letting
    the bad ones block the good one."""
    import json

    sha = "a" * 40
    (root / "in" / "good-input.json").write_text(
        json.dumps({"repo": "good", "provenance": {"sha": sha}})
    )
    (root / "docs" / "good.md").write_text(
        f"# Deep dive: good\n\nMeasured at `{sha[:8]}`.\n\n" + "Evidence paragraph. " * 60,
        encoding="utf-8",
    )
    (root / "in" / "ghost-input.json").write_text(
        json.dumps({"repo": "ghost", "provenance": {"sha": "b" * 40}})
    )
    # deliberately no docs/ghost.md - the missing-dossier case
    (root / "in" / "short-input.json").write_text(
        json.dumps({"repo": "short", "provenance": {"sha": "c" * 40}})
    )
    (root / "docs" / "short.md").write_text("Too short.", encoding="utf-8")


class MergeTest(SettingsIsolated):
    def test_merge_rejects_a_dossier_that_does_not_state_its_build(self):
        """Design promise: a dossier that cannot say which build it measured
        is rejected - churn/instability figures go stale every commit, so an
        unstamped claim is misleading rather than useful."""
        import json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _wire_deep_dive_paths(root)
            (root / "in" / "good-input.json").write_text(
                json.dumps({"repo": "good", "provenance": {"sha": "abcd1234" + "0" * 32}})
            )
            (root / "in" / "bad-input.json").write_text(
                json.dumps({"repo": "bad", "provenance": {"sha": "feed5678" + "0" * 32}})
            )
            (root / "docs" / "good.md").write_text(
                "# Deep dive: good\n\nMeasured at `abcd1234`.\n\n" + "Evidence paragraph. " * 60,
                encoding="utf-8",
            )
            (root / "docs" / "bad.md").write_text(
                "# Deep dive: bad\n\n" + "No stamp here. " * 60,
                encoding="utf-8",
            )
            code = dives.merge()
            written = json.loads((root / "in" / "dives.json").read_text())
        self.assertEqual(code, 1)  # bad was rejected
        self.assertEqual(list(written), ["good"])
        self.assertEqual(written["good"]["sha"], "abcd1234")
        self.assertIn("<h2>", written["good"]["html"])

    def test_one_bad_dossier_does_not_block_a_good_one(self):
        """Design promise: an invalid dossier can never enter the store, and
        one bad dossier does not silently block good ones in the same run."""
        import io as std_io
        import json
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _wire_deep_dive_paths(root)
            _write_mixed_dive_batch(root)
            captured = std_io.StringIO()
            with redirect_stdout(captured):
                code = dives.merge()
            written = json.loads((root / "in" / "dives.json").read_text())
        self.assertEqual(code, 1)
        self.assertEqual(list(written), ["good"])  # exactly the valid one entered the store
        self.assertEqual(written["good"]["sha"], "aaaaaaaa")
        output = captured.getvalue()
        self.assertIn("ghost: missing", output)  # reported, not silent
        self.assertIn(f"short: dossier shorter than {dives.MIN_DIVE_LENGTH}", output)

    def test_merge_with_no_bundles_tells_operator_to_extract_first(self):
        """Design promise: nothing to merge fails loudly (exit 1) rather than
        silently writing an empty store, and says what to do about it."""
        from contextlib import redirect_stderr
        from io import StringIO

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _wire_deep_dive_paths(root)
            captured = StringIO()
            with redirect_stderr(captured):
                code = dives.merge()
        self.assertEqual(code, 1)
        self.assertIn("No bundles", captured.getvalue())


class MainDispatchTest(SettingsIsolated):
    def setUp(self):
        self.addCleanup(setattr, sys, "argv", sys.argv)

    def test_extract_dispatch_rejects_unknown_repo_through_real_extract(self):
        """Design promise: `deepdive extract <repo>` for a repo not in the
        graph fails clearly (exit 1) - exercised through the real CLI
        dispatch and the real extract(), not a stand-in."""
        import json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.addCleanup(setattr, dives, "GRAPH_PATH", config.GRAPH_PATH)
            config.configure(GRAPH_PATH=root / "graph.json")
            (root / "graph.json").write_text(
                json.dumps({"nodes": [{"repo": "other-repo"}], "links": []})
            )
            sys.argv = ["prog", "extract", "no-such-repo"]
            self.assertEqual(dives.main(), 1)

    def test_merge_dispatch_produces_the_same_artefact_as_a_direct_call(self):
        """Design promise: `deepdive merge` through the CLI is the same
        contract as calling merge() directly - dispatch adds no behaviour of
        its own, exercised on the mixed valid/invalid batch."""
        import json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _wire_deep_dive_paths(root)
            _write_mixed_dive_batch(root)
            sys.argv = ["prog", "merge"]
            code = dives.main()
            written = json.loads((root / "in" / "dives.json").read_text())
        self.assertEqual(code, 1)
        self.assertEqual(list(written), ["good"])

    def test_no_arguments_and_bogus_argument_fail_the_usage_contract(self):
        """Design promise: with no sub-command or an unrecognised one, the
        stage fails (exit 1) instead of guessing what the caller meant."""
        sys.argv = ["prog"]
        self.assertEqual(dives.main(), 1)
        sys.argv = ["prog", "bogus"]
        self.assertEqual(dives.main(), 1)


if __name__ == "__main__":
    unittest.main()
