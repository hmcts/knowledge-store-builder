"""The remap's second route: `(repository, source_file)` where the node ids are gone.

A rebuild that re-runs semantic extraction renames essentially every semantic
node id, because those ids are built from labels an extraction authored. Only a
minority of the pre-rebuild ids survive and the survivors are almost exactly the
deterministic AST population, so prose about a semantically-extracted community
is dropped as `members-gone` even where the underlying files are unchanged.
Corpus paths do not have that property.

The route is a fallback rather than a replacement key, and most of these tests
exist to hold that shape rather than the matching itself. Each names the break
it catches; the ones worth stating twice are the over-correction guard (a
verdict the node ids reached is never reopened), the empty-key case (an empty
old key set matching an empty new one is a carry on no evidence that scores like
a perfect one) and the precision floor, which is measured in nodes on both sides
precisely so the file key cannot coarsen it.

Expected values here are derived by hand from the fixtures' own literals - the
counts are all single digits or the 37-to-458 shape recorded in the source - and
never by running the function being asserted on.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io as _io
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import config  # noqa: E402


def _nodes(communities: dict[str, list[tuple]]) -> list[dict]:
    """Graph nodes from `{community id: [(node id, repo, source_file), ...]}`.

    A `source_file` of None is a structural node - newer graphify emits Java
    package-hierarchy nodes with neither that nor a label - and is written with
    the key absent, the way such a node actually arrives.
    """
    out = []
    for community, members in communities.items():
        for node_id, repo, source in members:
            node = {"id": node_id, "community": int(community), "repo": repo}
            if source is not None:
                node["source_file"] = source
            out.append(node)
    return out


def _index(communities: dict[str, list[tuple]]):
    """The three new-graph indexes `_file_claims` needs, built by the real code."""
    nodes = _nodes(communities)
    by_file, files_of = summaries._file_index(nodes)
    sizes = Counter(str(node["community"]) for node in nodes)
    return by_file, files_of, sizes


GONE = {"reason": "members-gone", "best_target": None, "share": None, "prose": "prose"}


class FileClaimsTest(unittest.TestCase):
    """The fallback in isolation: what it reopens, what it refuses, what it measures."""

    def _claim(self, displaced, old_members, old_files, new, bar=0.6, precision=0.2, carry="exact"):
        by_file, files_of, sizes = _index(new)
        return summaries._file_claims(
            displaced,
            old_members,
            {name: {tuple(key) for key in keys} for name, keys in old_files.items()},
            by_file,
            files_of,
            sizes,
            bar,
            precision,
            carry,
        )

    def test_a_members_gone_community_whose_files_match_is_carried(self):
        """The break: a rebuild renames every semantic node id, and prose about a
        community whose corpus files are unchanged is withdrawn anyway. Without
        this the fallback does nothing and the whole route is decoration."""
        claims, outcomes = self._claim(
            {"7": dict(GONE)},
            {"7": ["sem::Old A", "sem::Old B", "sem::Old C"]},
            {"7": [("repo-a", "src/A.java"), ("repo-a", "src/B.java"), ("repo-a", "src/C.java")]},
            {
                "42": [
                    ("sem::New A", "repo-a", "src/A.java"),
                    ("sem::New B", "repo-a", "src/B.java"),
                    ("sem::New C", "repo-a", "src/C.java"),
                ]
            },
        )
        self.assertEqual(claims, {"7": ("42", 1.0, 1.0, False)})
        self.assertEqual(
            outcomes["7"],
            {
                "outcome": "carried",
                "files": 3,
                "matched_files": 3,
                "best_target": "42",
                "share": 1.0,
                "precision": 1.0,
                "nodes": 3,
                "target_nodes": 3,
                "attributed_nodes": 3,
                "files_exact": True,
            },
        )

    def test_a_fallback_carry_is_never_marked_as_set_equality(self):
        """The break: `"exact": true` on a fallback carry. That flag is #296's
        marker for "the target holds exactly the set the prose was written
        about", and by construction it does not - the members are gone. Marking
        a file-set match as set equality hands a downstream check a clean bill
        for the least exact carry the stage makes."""
        claims, outcomes = self._claim(
            {"7": dict(GONE)},
            {"7": ["old"]},
            {"7": [("repo-a", "src/A.java")]},
            {"42": [("new", "repo-a", "src/A.java")]},
        )
        self.assertEqual(claims["7"][3], False, "the node set is gone, so nothing is identical")
        self.assertTrue(outcomes["7"]["files_exact"], "the file verdict is reported on its own key")

    def test_a_verdict_the_node_route_reached_is_not_reopened(self):
        """The over-correction guard, and the reason this is a fallback.

        The break: the fallback becoming a second criterion stacked after the
        first, so prose the node ids rejected on evidence that still exists gets
        carried on a coarser key anyway. Every reason but `members-gone` was
        measured against members the graph still holds; only `members-gone`
        means there was nothing to measure.
        """
        perfect = {"1": [("repo-a", "src/A.java")]}
        new = {"42": [("a", "repo-a", "src/A.java")]}
        for reason in ("not-identical", "below-bar", "below-precision", "collision"):
            with self.subTest(reason=reason):
                claims, outcomes = self._claim(
                    {"1": {"reason": reason, "best_target": "42", "prose": "prose"}},
                    {"1": ["a-old"]},
                    perfect,
                    new,
                )
                self.assertEqual(claims, {}, f"{reason} was already decided on live evidence")
                self.assertEqual(outcomes, {}, "and it is not even reported as a fallback outcome")

    def test_a_community_with_no_file_key_is_not_carried_on_an_empty_match(self):
        """The dangerous case, and the reason it is refused by name.

        The break: an empty old key set matching an empty new key set. Every
        measure below would read 1.0 over no evidence at all, and a wholly
        structural community - graphify's Java package-hierarchy nodes carry no
        `source_file` - has exactly that key set. The target here is structural
        too, so both sides are empty and the match would look perfect.
        """
        claims, outcomes = self._claim(
            {"7": dict(GONE)},
            {"7": ["pkg::a", "pkg::b"]},
            {},
            {"42": [("pkg::x", "repo-a", None), ("pkg::y", "repo-a", None)]},
        )
        self.assertEqual(claims, {})
        self.assertEqual(outcomes["7"], {"outcome": "no-file-key", "files": 0})

    def test_a_structural_old_community_is_refused_even_where_the_target_has_files(self):
        """The same break from the other side: nothing to key on is not a match
        against everything. A structural-only old community keys on no file, so
        there is no file it can be said to share with a target that has them."""
        claims, outcomes = self._claim(
            {"7": dict(GONE)},
            {"7": ["pkg::a"]},
            {"7": []},
            {"42": [("n", "repo-a", "src/A.java")]},
        )
        self.assertEqual(claims, {})
        self.assertEqual(outcomes["7"]["outcome"], "no-file-key")

    def test_files_absent_from_the_new_graph_are_named_as_no_match(self):
        """Distinct from having no key: the community keyed on real files and
        those files have left the corpus. The break is collapsing the two, which
        would report a deleted repository as a structural community."""
        claims, outcomes = self._claim(
            {"7": dict(GONE)},
            {"7": ["old"]},
            {"7": [("repo-gone", "src/A.java")]},
            {"42": [("n", "repo-a", "src/B.java")]},
        )
        self.assertEqual(claims, {})
        self.assertEqual(outcomes["7"], {"outcome": "no-file-match", "files": 1})

    def test_the_precision_floor_rejects_a_carry_onto_a_much_larger_cluster(self):
        """Community 154 from a real refresh, reached through the fallback.

        The break: the file key coarsening the precision bar out of existence.
        37 members grew to 458 nodes over the *same three files*, so file recall
        is 1.00, the file sets are identical, and every node in the target is
        attributable to those files. A file-counted precision sees nothing wrong
        with that. Counted in nodes it is 37/458 = 0.081 - the same figure the
        node route computes for the same cluster - and it is refused.
        """
        old_nodes = [f"sem::old {i}" for i in range(37)]
        files = [("repo-a", f"src/{name}.java") for name in ("A", "B", "C")]
        new = {
            "9": [(f"sem::new {i}", "repo-a", "src/A.java") for i in range(150)]
            + [(f"doc::{i}", "repo-a", "src/B.java") for i in range(150)]
            + [(f"doc::b{i}", "repo-a", "src/C.java") for i in range(158)]
        }
        claims, outcomes = self._claim({"154": dict(GONE)}, {"154": old_nodes}, {"154": files}, new)
        self.assertEqual(claims, {}, "recall 1.00 and an identical file set, and still refused")
        self.assertEqual(outcomes["154"]["outcome"], "below-precision")
        self.assertEqual(outcomes["154"]["share"], 1.0, "file recall cannot see this")
        self.assertEqual(outcomes["154"]["target_nodes"], 458)
        self.assertEqual(outcomes["154"]["attributed_nodes"], 458, "every node is on those files")
        self.assertEqual(
            outcomes["154"]["precision"], 0.081, "37 of 458, the node route's own figure"
        )

    def test_a_target_that_absorbed_other_files_fails_the_floor_too(self):
        """The other precision term. The break: crediting the prose with a
        merged cluster's whole node count because the old files are all still in
        it somewhere. Three old files land in a target of 33 nodes, 30 of which
        come from a file the prose never described: 3/33 = 0.091."""
        files = [("repo-a", f"src/{name}.java") for name in ("A", "B", "C")]
        new = {
            "42": [
                ("n1", "repo-a", "src/A.java"),
                ("n2", "repo-a", "src/B.java"),
                ("n3", "repo-a", "src/C.java"),
            ]
            + [(f"other{i}", "repo-b", "src/Z.java") for i in range(30)]
        }
        claims, outcomes = self._claim(
            {"7": dict(GONE)},
            {"7": ["o1", "o2", "o3"]},
            {"7": files},
            new,
            carry="overlap",
        )
        self.assertEqual(claims, {})
        self.assertEqual(outcomes["7"]["outcome"], "below-precision")
        self.assertEqual(outcomes["7"]["share"], 1.0, "all three old files are present")
        self.assertEqual(outcomes["7"]["attributed_nodes"], 3)
        self.assertEqual(outcomes["7"]["precision"], 0.091, "3 of 33")

    def test_structural_nodes_in_the_target_are_never_credited_to_the_prose(self):
        """The break: dropping nodes with no `source_file` from the precision
        denominator, so a structural-heavy target reads as fully described by
        prose keyed on the two files it happens to contain. Two described nodes
        in a community of twenty is 0.1, and the eighteen package-hierarchy
        nodes are the reason."""
        new = {
            "42": [
                ("n1", "repo-a", "src/A.java"),
                ("n2", "repo-a", "src/B.java"),
            ]
            + [(f"pkg::{i}", "repo-a", None) for i in range(18)]
        }
        claims, outcomes = self._claim(
            {"7": dict(GONE)},
            {"7": ["o1", "o2"]},
            {"7": [("repo-a", "src/A.java"), ("repo-a", "src/B.java")]},
            new,
        )
        self.assertEqual(claims, {})
        self.assertEqual(outcomes["7"]["outcome"], "below-precision")
        self.assertTrue(outcomes["7"]["files_exact"], "the file sets match exactly, and it is not")
        self.assertEqual(outcomes["7"]["target_nodes"], 20, "the structural nodes are in the graph")
        self.assertEqual(outcomes["7"]["attributed_nodes"], 2, "and are attributable to no file")
        self.assertEqual(outcomes["7"]["precision"], 0.1)

    def test_a_file_set_short_of_the_recall_bar_is_refused_under_overlap(self):
        """The break: the fallback carrying on any match at all. One old file of
        three landing is 0.33, under a 0.6 bar."""
        new = {"42": [("n1", "repo-a", "src/A.java")]}
        claims, outcomes = self._claim(
            {"7": dict(GONE)},
            {"7": ["o1"]},
            {"7": [("repo-a", f"src/{n}.java") for n in ("A", "B", "C")]},
            new,
            carry="overlap",
        )
        self.assertEqual(claims, {})
        self.assertEqual(outcomes["7"]["outcome"], "below-bar")
        self.assertEqual(outcomes["7"]["share"], 0.333)

    def test_a_file_set_that_is_not_the_targets_is_refused_under_exact(self):
        """The break: `--carry exact` silently becoming a tolerance on the
        fallback route. The target holds a fourth file the prose never
        described, so it is not the file set either."""
        new = {
            "42": [
                ("n1", "repo-a", "src/A.java"),
                ("n2", "repo-a", "src/B.java"),
                ("n3", "repo-a", "src/D.java"),
            ]
        }
        claims, outcomes = self._claim(
            {"7": dict(GONE)},
            {"7": ["o1", "o2"]},
            {"7": [("repo-a", "src/A.java"), ("repo-a", "src/B.java")]},
            new,
        )
        self.assertEqual(claims, {})
        self.assertEqual(outcomes["7"]["outcome"], "not-identical")
        self.assertFalse(outcomes["7"]["files_exact"])

    def test_the_best_target_is_chosen_deterministically_when_two_tie(self):
        """The break: `Counter.most_common` breaking a tie on insertion order,
        which here follows a set iteration - so hash randomisation would move
        the chosen target between processes and two runs on one graph would
        disagree. Both targets hold one of the two old files; the lower id wins.
        """
        new = {
            "9": [("n9", "repo-a", "src/A.java")],
            "8": [("n8", "repo-a", "src/B.java")],
        }
        _, outcomes = self._claim(
            {"7": dict(GONE)},
            {"7": ["o1"]},
            {"7": [("repo-a", "src/A.java"), ("repo-a", "src/B.java")]},
            new,
            carry="overlap",
            bar=0.5,
        )
        self.assertEqual(outcomes["7"]["best_target"], "8")
        self.assertEqual(outcomes["7"]["share"], 0.5, "one of the two old files")

    def test_repository_is_part_of_the_key(self):
        """The break: keying on the bare path. Two repositories in a merged
        estate hold `src/main/java/App.java`, and collapsing them would match a
        community in one onto a community in the other."""
        claims, outcomes = self._claim(
            {"7": dict(GONE)},
            {"7": ["o1"]},
            {"7": [("repo-a", "src/App.java")]},
            {"42": [("n1", "repo-b", "src/App.java")]},
        )
        self.assertEqual(claims, {})
        self.assertEqual(outcomes["7"]["outcome"], "no-file-match")


class SnapshotFileKeysTest(SettingsIsolated):
    """`summaries snapshot` writes the second half of the baseline."""

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

    def _snapshot(self, communities) -> str:
        config.GRAPH_PATH.write_text(
            json.dumps({"nodes": _nodes(communities), "links": []}), encoding="utf-8"
        )
        err = _io.StringIO()
        with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(summaries.snapshot(), 0)
        return err.getvalue()

    def _read(self) -> dict:
        with gzip.open(config.SUMMARIES_FILE_SNAPSHOT_PATH, "rt", encoding="utf-8") as handle:
            return json.load(handle)

    def test_the_file_keys_are_written_beside_the_node_ids(self):
        """The break: no file snapshot, so the remap's fallback has no old side
        to key on - and after a rebuild nothing else can supply one, because the
        node ids that would have answered are the ones that have gone."""
        self._snapshot(
            {
                "1": [("a", "repo-a", "src/A.java"), ("b", "repo-a", "src/B.java")],
                "2": [("c", "repo-b", "src/C.java")],
            }
        )
        self.assertEqual(
            self._read()["communities"],
            {
                "1": [["repo-a", "src/A.java"], ["repo-a", "src/B.java"]],
                "2": [["repo-b", "src/C.java"]],
            },
        )

    def test_a_repeated_file_is_recorded_once_and_the_keys_are_sorted(self):
        """The break: two runs on one graph producing different bytes. These are
        committed artefacts, so a set iteration reaching the file would make the
        order depend on hash randomisation."""
        self._snapshot(
            {
                "1": [
                    ("a", "repo-a", "src/Z.java"),
                    ("b", "repo-a", "src/A.java"),
                    ("c", "repo-a", "src/Z.java"),
                ]
            }
        )
        self.assertEqual(
            self._read()["communities"]["1"],
            [["repo-a", "src/A.java"], ["repo-a", "src/Z.java"]],
        )

    def test_structural_nodes_contribute_no_key(self):
        """The break: keying a node with no `source_file` as `(repo, "")`. That
        one key is shared by every structural node in a repository, so any two
        structural-heavy communities would match on it wholesale."""
        self._snapshot(
            {
                "1": [("a", "repo-a", "src/A.java"), ("pkg::x", "repo-a", None)],
                "2": [("pkg::y", "repo-a", None)],
            }
        )
        recorded = self._read()["communities"]
        self.assertEqual(recorded["1"], [["repo-a", "src/A.java"]])
        self.assertNotIn(
            "2", recorded, "a community keyed on nothing is absent, not an empty match"
        )

    def test_a_graph_with_no_source_files_says_the_fallback_will_not_run(self):
        """The break: a later remap printing "0 carried by the fallback" against
        a graph where the route had nothing to work with, with nothing in either
        run saying so. A clean verdict over an absent measurement."""
        err = self._snapshot({"1": [("pkg::x", "repo-a", None), ("pkg::y", "repo-a", None)]})
        self.assertIn("no fallback to fall back to", err)

    def test_the_file_snapshot_is_fingerprinted_against_the_membership_snapshot(self):
        """The break: an older library's `snapshot` refreshing the membership
        file and leaving a previous rebuild's file keys beside it - the shape a
        pinned release produces - after which the fallback keys on a clustering
        nobody is remapping. The digest is over the membership mapping as
        written, computed here from this test's own literals."""
        self._snapshot({"1": [("a", "repo-a", "src/A.java"), ("b", "repo-a", "src/B.java")]})
        canonical = json.dumps({"1": ["a", "b"]}, sort_keys=True, separators=(",", ":"))
        self.assertEqual(
            self._read()["members_digest"],
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


class RemapFallbackTest(SettingsIsolated):
    """The route end to end: the merged artefact, the report and the two counts.

    Built through the real pipeline - `snapshot` against the pre-rebuild graph,
    then `remap` against the rebuilt one - because the two snapshot files must be
    taken together for the fallback to trust them, and hand-writing them would
    test a pairing the stage never produces.
    """

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

    # --- the fixture ---------------------------------------------------------
    #
    # 30 filler communities so the `--floor` guard does not fire, each one AST
    # node that survives the rebuild unchanged. Then two communities that matter:
    #
    #   "7" - three semantically extracted nodes on three files. The rebuild
    #         renames all three ids and keeps the files, which is the whole
    #         finding: node-id route drops it as `members-gone`, file route
    #         carries it.
    #   "8" - three AST nodes whose ids are deterministic and survive. Carried by
    #         the node ids, and it must stay that way.

    FILLER = {str(1000 + i): [(f"x{i}", "repo-f", f"src/F{i}.java")] for i in range(30)}
    OLD_SEMANTIC = [
        ("sem::Old A", "repo-a", "src/A.java"),
        ("sem::Old B", "repo-a", "src/B.java"),
        ("sem::Old C", "repo-a", "src/C.java"),
    ]
    NEW_SEMANTIC = [
        ("sem::New A", "repo-a", "src/A.java"),
        ("sem::New B", "repo-a", "src/B.java"),
        ("sem::New C", "repo-a", "src/C.java"),
    ]
    AST = [(f"ast{i}", "repo-a", "src/D.java") for i in range(3)]

    def write_graph(self, communities):
        config.GRAPH_PATH.write_text(
            json.dumps({"nodes": _nodes(communities), "links": []}), encoding="utf-8"
        )

    def take_snapshot(self):
        self.write_graph({**self.FILLER, "7": self.OLD_SEMANTIC, "8": self.AST})
        with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
            self.assertEqual(summaries.snapshot(), 0)

    def rebuild(self):
        self.write_graph({**self.FILLER, "42": self.NEW_SEMANTIC, "43": self.AST})

    def write_summaries(self):
        body = {"7": "the semantic community", "8": "the AST community"}
        body |= {str(1000 + i): f"filler {i}" for i in range(30)}
        config.SUMMARIES_PATH.write_text(json.dumps(body), encoding="utf-8")

    def remap(self):
        out, err = _io.StringIO(), _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = summaries.remap()
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue(), err.getvalue()

    def report(self):
        return json.loads(config.REMAP_REPORT_PATH.read_text(encoding="utf-8"))

    # --- the tests -----------------------------------------------------------

    def test_the_renamed_community_is_carried_onto_its_new_id(self):
        """The break this whole change exists for: a rebuild that renames every
        semantic node id withdraws prose whose corpus files never moved. The
        assertion is on the merged artefact, not on the retention line."""
        self.take_snapshot()
        self.write_summaries()
        self.rebuild()
        self.remap()
        merged = json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(merged["42"], "the semantic community")
        self.assertEqual(merged["43"], "the AST community")
        self.assertNotIn(
            "7",
            json.loads(config.SUMMARIES_WITHDRAWN_PATH.read_text(encoding="utf-8")),
            "carried prose must not also sit in the withdrawn file",
        )

    def test_the_report_names_the_route_that_carried_each_summary(self):
        """The break: the two routes reported as one number. The measurement
        that decides whether the fallback should stay is its false-carry rate,
        which is a question about the fallback-only carries - and nobody can
        sample a set the report does not distinguish."""
        self.take_snapshot()
        self.write_summaries()
        self.rebuild()
        stdout, _ = self.remap()
        report = self.report()
        self.assertEqual(
            report["carried"]["42"],
            {
                "from": "7",
                "share": 1.0,
                "precision": 1.0,
                "exact": False,
                "route": "source-files",
                "files": 3,
                "matched_files": 3,
                "files_exact": True,
                "nodes": 3,
                "target_nodes": 3,
                "attributed_nodes": 3,
            },
        )
        self.assertEqual(report["carried"]["43"]["route"], "node-ids")
        self.assertEqual(report["carried"]["43"]["exact"], True)
        self.assertEqual(
            report["fallback"],
            {"available": True, "considered": 1, "outcomes": {"carried": 1}},
        )
        self.assertIn(
            "Carried by route: 31 on node ids, 1 on (repository, source_file)",
            stdout,
            "30 filler plus the AST community on ids, the renamed one on files",
        )

    def test_a_store_with_no_file_snapshot_remaps_as_it_did_before(self):
        """The break: a store whose snapshot predates the file keys failing, or
        - worse - reporting `0 carried by the fallback` as though the route had
        run and found nothing. An absent measurement is not a zero."""
        self.take_snapshot()
        config.SUMMARIES_FILE_SNAPSHOT_PATH.unlink()
        self.write_summaries()
        self.rebuild()
        _, stderr = self.remap()
        merged = json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("42", merged, "nothing carries onto the renamed community")
        withdrawn = json.loads(config.SUMMARIES_WITHDRAWN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(withdrawn["7"], "the semantic community")
        fallback = self.report()["fallback"]
        self.assertEqual(fallback["available"], False)
        self.assertEqual(fallback["considered"], 1)
        self.assertEqual(fallback["outcomes"], {})
        self.assertIn("no file snapshot", fallback["reason"])
        self.assertIn("did not run for the 1 summaries whose members are gone", stderr)

    def test_a_file_snapshot_from_another_membership_is_not_trusted(self):
        """The break: keying the fallback on file sets recorded against a
        different clustering. An older library's `snapshot` refreshes the
        membership file and leaves the previous rebuild's file keys beside it,
        and the fallback then carries prose using evidence about other data."""
        self.take_snapshot()
        with gzip.open(config.SUMMARIES_SNAPSHOT_PATH, "wt", encoding="utf-8") as handle:
            json.dump({"7": ["sem::Old A"], "8": [node for node, _, _ in self.AST]}, handle)
        self.write_summaries()
        self.rebuild()
        _, stderr = self.remap()
        self.assertNotIn("42", json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8")))
        fallback = self.report()["fallback"]
        self.assertEqual(fallback["available"], False)
        self.assertIn("different membership snapshot", fallback["reason"])
        self.assertIn("different membership snapshot", stderr)

    def test_the_fallback_never_takes_a_target_from_the_node_id_route(self):
        """The over-correction guard where it can actually bite: both routes
        claim one target. The break is sorting a file share against a node share
        - they are shares of different things - which would let the fallback's
        coarser 1.00 displace a node-id claim measured on ids the graph still
        holds. Here the fallback looks strictly better on both numbers and must
        still lose.
        """
        # "8" keeps its AST ids and lands in "43" with one node added, so under
        # `overlap` it carries at recall 1.00 and precision 0.75. "7" is renamed
        # away, and its files are exactly "43"'s four files - so the fallback
        # would claim "43" at recall 1.00 and precision 1.00.
        four = self.AST + [("ast-new", "repo-a", "src/E.java")]
        self.write_graph({**self.FILLER, "7": self.OLD_SEMANTIC, "8": self.AST})
        with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
            self.assertEqual(summaries.snapshot(), 0)
        # The snapshot must record "7" as keyed on "43"'s files, which is what
        # makes it a contender for the same target.
        with gzip.open(config.SUMMARIES_FILE_SNAPSHOT_PATH, "rt", encoding="utf-8") as handle:
            document = json.load(handle)
        document["communities"]["7"] = [["repo-a", "src/D.java"], ["repo-a", "src/E.java"]]
        with gzip.open(config.SUMMARIES_FILE_SNAPSHOT_PATH, "wt", encoding="utf-8") as handle:
            json.dump(document, handle)
        self.write_summaries()
        self.write_graph({**self.FILLER, "43": four})
        out, err = _io.StringIO(), _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(summaries.remap(carry="overlap"), 0, err.getvalue())
        report = self.report()
        self.assertEqual(report["carried"]["43"]["from"], "8", "the node-id claim keeps the target")
        self.assertEqual(report["carried"]["43"]["route"], "node-ids")
        self.assertEqual(report["carried"]["43"]["precision"], 0.75, "and its own weaker figure")
        self.assertEqual(report["displaced"]["7"]["reason"], "collision")
        self.assertEqual(report["displaced"]["7"]["route"], "source-files")
        self.assertEqual(
            report["fallback"]["outcomes"],
            {"collision": 1},
            "a fallback claim pass 2 took away is not counted as a carry",
        )
        self.assertEqual(
            json.loads(config.SUMMARIES_WITHDRAWN_PATH.read_text(encoding="utf-8"))["7"],
            "the semantic community",
            "the loser's prose still reaches the backfill queue",
        )

    def test_a_fallback_refusal_leaves_the_node_routes_verdict_standing(self):
        """The break: a fallback that ran and refused eating the node route's
        withdrawal, so the paragraph somebody is meant to re-author is neither
        carried nor withdrawn. The target here holds the right files and twenty
        times the nodes, so the precision floor refuses it."""
        swollen = self.NEW_SEMANTIC + [(f"doc::{i}", "repo-a", "src/A.java") for i in range(60)]
        self.take_snapshot()
        self.write_summaries()
        self.write_graph({**self.FILLER, "42": swollen, "43": self.AST})
        self.remap()
        withdrawn = json.loads(config.SUMMARIES_WITHDRAWN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(withdrawn["7"], "the semantic community")
        entry = self.report()["displaced"]["7"]
        self.assertEqual(entry["reason"], "members-gone", "the node route's verdict is unchanged")
        self.assertEqual(entry["fallback"]["outcome"], "below-precision")
        self.assertEqual(entry["fallback"]["target_nodes"], 63)
        self.assertEqual(entry["fallback"]["precision"], 0.048, "3 of 63")
        self.assertEqual(
            self.report()["fallback"]["outcomes"], {"below-precision": 1}, "and it is counted"
        )


if __name__ == "__main__":
    unittest.main()
