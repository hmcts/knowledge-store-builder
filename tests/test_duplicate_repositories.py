"""A near-copy of a repository has to be visible to a person (#246).

Two repositories on one estate were near-identical to two others by their
(label, path) pairs, and no count the pipeline reported made it visible. They
were found because subagents authoring community summaries independently noticed
they were writing the same description twice.

Node ids are why nothing saw it: `merge-graphs` namespaces every id as
`<repo>::<id>`, so two copies of one file produce ids that cannot collide by
construction. Labels and paths are not namespaced.

These tests keep it a measurement. There is deliberately no assertion that
anything is classified a duplicate, and no threshold above which one is declared,
because a near-copy can be a vendored fork or a migration part-way through and
only the person reading the two names can say. What is asserted is that the
numbers a person needs to make that judgement are all on the line, that neither
directional reading is hidden, that an estate with nothing to report says nothing,
and that the pruning which makes the report affordable cannot change its result.
"""

from __future__ import annotations

import contextlib
import io as io_module
import json
import os
import subprocess
import sys
import tempfile
import unittest
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import duplicate_repositories as duplicates  # noqa: E402
from knowledgestore import graph_stream  # noqa: E402
from knowledgestore import status  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src"


def signatures(estate: dict[str, list[tuple[str, str]]]) -> dict[str, set[tuple[str, str]]]:
    return {repo: set(pairs) for repo, pairs in estate.items()}


def own_pairs(repo: str, count: int) -> list[tuple[str, str]]:
    """`count` (label, path) pairs that no other repository built this way holds."""
    return [(f"{repo}-symbol-{i}", f"src/{repo}/{i}.txt") for i in range(count)]


def graph(estate: dict[str, list[tuple[str, str]]]) -> dict:
    """A node-link graph carrying one node per (label, path) pair, as merge-graphs
    would leave it: ids namespaced per repository, so no id collides across repos."""
    nodes = []
    for repo, pairs in estate.items():
        for index, (label, source) in enumerate(pairs):
            nodes.append(
                {
                    "id": f"{repo}::node-{index}",
                    "label": label,
                    "source_file": source,
                    "repo": repo,
                }
            )
    return {"nodes": nodes, "links": []}


def unpruned(by_repo: dict[str, set[tuple[str, str]]], top: int) -> list[tuple[str, str]]:
    """The same ranking, every pair intersected, written independently of the module.

    Deliberately the naive form: it exists to be compared against the pruned one,
    so it must not share the code it is checking.
    """
    scored = []
    for left, right in combinations(sorted(by_repo), 2):
        shared = len(by_repo[left] & by_repo[right])
        if shared:
            larger = max(len(by_repo[left]), len(by_repo[right]))
            scored.append((-shared / larger, left, right))
    return [(left, right) for _score, left, right in sorted(scored)[:top]]


class GraphFixture(unittest.TestCase):
    """A real graph file on disk, read by the real streaming scan."""

    def _graph_file(self, payload: dict) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "graph.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


class OverlapMeasurementTest(GraphFixture):
    def test_identical_pair_sets_are_reported_at_one_hundred_percent(self):
        """Breaks if the overlap is computed over anything a copy does not preserve.

        Ids are namespaced per repository by `merge-graphs`, so an implementation
        keyed on them reports 0% for two byte-identical repositories - which is
        exactly the silence this report exists to end. Read from a graph file, not
        from hand-built sets, so the signature the scan actually derives is what is
        measured.
        """
        shared = [("Widget", "src/widget.java"), ("Gadget", "src/gadget.java")]
        path = self._graph_file(graph({"alpha": shared, "alpha-fork": list(shared)}))

        report = duplicates.near_duplicates(path)

        self.assertEqual(len(report.overlaps), 1)
        overlap = report.overlaps[0]
        self.assertEqual((overlap.left, overlap.right), ("alpha", "alpha-fork"))
        self.assertEqual(overlap.shared, 2)
        self.assertEqual(overlap.fraction, 1.0)
        self.assertEqual((overlap.left_fraction, overlap.right_fraction), (1.0, 1.0))

    def test_a_partial_overlap_keeps_its_hand_derived_percentage(self):
        """Breaks if the percentage is bucketed, rounded to whole numbers, or
        computed against the wrong denominator.

        Seven of eight pairs shared between two eight-pair repositories is 87.5%
        exactly. A whole-number format prints 88% and a bucketed one prints
        something like "high", and both lose the distinction between a fork that
        has moved on and one that has not.
        """
        common = own_pairs("common", 7)
        estate = {
            "alpha": common + own_pairs("alpha", 1),
            "beta": common + own_pairs("beta", 1),
        }

        report = duplicates.rank(signatures(estate))
        overlap = report.overlaps[0]

        self.assertEqual((overlap.shared, overlap.left_size, overlap.right_size), (7, 8, 8))
        self.assertEqual(overlap.fraction, 0.875)
        self.assertIn("87.5%", duplicates.lines(report)[1])

    def test_containment_shows_both_readings_on_the_same_line(self):
        """Breaks if only one direction of the overlap reaches the report.

        A small repository wholly inside a large one is 100% of itself and 10% of
        the other, and both are true. A line carrying only the ranking figure reads
        as "10%, ignore it" and hides that every file the small repository holds is
        already somewhere else; a line carrying only the containment figure reads as
        a full duplicate of something 10 times its size.
        """
        small = own_pairs("shared", 2)
        estate = {"gamma": small, "omega": small + own_pairs("omega", 18)}

        report = duplicates.rank(signatures(estate))
        overlap = report.overlaps[0]
        line = duplicates.lines(report)[1]

        self.assertEqual(overlap.fraction, 0.1)
        self.assertEqual(overlap.left_fraction, 1.0)
        self.assertEqual(overlap.right_fraction, 0.1)
        self.assertIn("100.0% of gamma (2)", line)
        self.assertIn("10.0% of omega (20)", line)

    def test_an_estate_sharing_no_pair_prints_nothing(self):
        """Breaks if the report finds something on an estate that has nothing.

        The sensitivity control. A report that always prints a top ten is a report
        nobody reads, and the ranking would then be indistinguishable from noise on
        the estates where it matters.
        """
        estate = {name: own_pairs(name, 4) for name in ("alpha", "beta", "gamma")}

        report = duplicates.rank(signatures(estate))

        self.assertEqual(report.overlaps, ())
        self.assertEqual(duplicates.lines(report), [])

    def test_the_cost_is_stated_in_the_report(self):
        """Breaks if the report stops saying what it cost.

        A quadratic report nobody can size is a report an operator declines to run.
        The two figures are the claim that the pruning works, so they have to be on
        screen rather than in a docstring.
        """
        common = own_pairs("common", 4)
        estate = {"alpha": list(common), "beta": list(common), "gamma": own_pairs("gamma", 4)}

        report = duplicates.rank(signatures(estate))

        self.assertEqual((report.considered, report.intersected), (3, 3))
        self.assertIn("3 pair(s) bounded, 3 intersected", duplicates.lines(report)[0])


class PruningTest(unittest.TestCase):
    """The bound is exact. These tests are what says so."""

    def estate(self) -> dict[str, list[tuple[str, str]]]:
        """Two near-copy pairs among twelve repositories, sizes chosen so that the
        bound of every remaining pair is below the second-best actual overlap."""
        alpha = own_pairs("alpha", 20)
        beta = own_pairs("beta", 18)
        estate = {
            "alpha": list(alpha),
            "alpha-fork": list(alpha),
            "beta": beta + own_pairs("beta-only", 2),
            "beta-copy": beta + own_pairs("beta-copy-only", 2),
        }
        for size in (1, 2, 3, 5, 8, 40, 70, 80):
            estate[f"other-{size:02d}"] = own_pairs(f"other-{size:02d}", size)
        return estate

    def test_the_bound_never_excludes_a_pair_that_would_have_ranked(self):
        """Breaks if the pruning becomes a heuristic - a similarity threshold, a
        sampled signature, a bound that is not an upper bound.

        `|A n B| <= min(|A|, |B|)`, so `shared / max <= min / max`. Any inexact
        bound skips real near-copies in silence, which is the failure this report
        was written to end, and the silence looks identical to a clean estate.
        """
        by_repo = signatures(self.estate())

        report = duplicates.rank(by_repo, top=2)

        self.assertEqual(
            [(o.left, o.right) for o in report.overlaps],
            unpruned(by_repo, top=2),
            "the pruned ranking differs from intersecting every pair",
        )
        self.assertEqual(
            [(o.left, o.right) for o in report.overlaps],
            [("alpha", "alpha-fork"), ("beta", "beta-copy")],
        )
        self.assertEqual(report.considered, 66, "12 repositories is 66 pairs")
        self.assertLess(
            report.intersected,
            report.considered,
            "no pair was pruned, so this fixture proves nothing about the pruning",
        )
        self.assertEqual(report.intersected, 6)

    def test_an_underestimating_bound_would_stop_before_a_pair_that_ranks(self):
        """Breaks if the bound stops being an exact upper bound on the overlap.

        The saving is only sound because `min / max` can never be less than
        `shared / max`. Anything that shrinks it - a sampled signature, a bound
        scaled "to be safe", a similarity threshold - stops the walk early and the
        pair it stopped before is never reported. Here `eta` is wholly inside
        `theta` at 50% and is the second-best pair on the estate, and it sits behind
        candidates bounded at 0.6; a bound that under-estimated by squaring itself
        would fall below the 30% pair already held and never reach it.
        """
        contained = own_pairs("inside-theta", 6)
        together = own_pairs("shared-pair", 3)
        estate = {
            "alpha": own_pairs("alpha", 20),
            "alpha-fork": own_pairs("alpha", 20),
            "epsilon": together + own_pairs("epsilon", 7),
            "zeta": together + own_pairs("zeta", 7),
            "eta": list(contained),
            "theta": contained + own_pairs("theta", 6),
        }
        by_repo = signatures(estate)

        report = duplicates.rank(by_repo, top=2)

        self.assertEqual(
            [(o.left, o.right) for o in report.overlaps],
            [("alpha", "alpha-fork"), ("eta", "theta")],
        )
        self.assertEqual([(o.left, o.right) for o in report.overlaps], unpruned(by_repo, top=2))
        self.assertEqual([o.fraction for o in report.overlaps], [1.0, 0.5])

    def test_a_bound_equal_to_the_worst_kept_overlap_is_still_evaluated(self):
        """Breaks if the pruning test is written `<=` rather than `<`.

        A pair whose bound equals the worst overlap currently kept can still tie it
        and take its place on the name tiebreak, so stopping at equality reports a
        different pair. The off-by-one is invisible unless a fixture ties.
        """
        common = own_pairs("common", 2)
        estate = {
            "alpha": list(common),
            "bravo": common + own_pairs("bravo", 2),
            "charlie": common + own_pairs("charlie", 2),
            "echo": common + own_pairs("echo", 2),
        }
        by_repo = signatures(estate)

        report = duplicates.rank(by_repo, top=1)

        self.assertEqual(
            [(o.left, o.right) for o in report.overlaps],
            unpruned(by_repo, top=1),
        )
        self.assertEqual([(o.left, o.right) for o in report.overlaps], [("alpha", "bravo")])
        self.assertEqual(report.intersected, report.considered)


class DeterminismTest(unittest.TestCase):
    def estate(self) -> dict[str, list[tuple[str, str]]]:
        """Two pairs at an identical overlap, reached in the opposite order to the
        one they must print in.

        `alpha` is wholly inside `xray`, which is 50% of `xray`; `yankee` and `zulu`
        share half of each other, also 50%. The candidate order is by *bound*, and
        two equal-sized repositories bound at 1.0 while a small one against a large
        one bounds at 0.5 - so `yankee`/`zulu` is evaluated first and `alpha`/`xray`
        second. Only an explicit tiebreak puts them back in name order, and a
        fixture whose pairs arrive already sorted cannot tell.
        """
        into_xray = own_pairs("shared-with-xray", 2)
        across = own_pairs("shared-across", 2)
        return {
            "alpha": list(into_xray),
            "xray": into_xray + own_pairs("xray", 2),
            "yankee": across + own_pairs("yankee", 2),
            "zulu": across + own_pairs("zulu", 2),
        }

    def test_equal_overlaps_are_ordered_by_name(self):
        """Breaks if two equally overlapping pairs can swap.

        Both pairs are 50%, so nothing but the name tiebreak separates them. A sort
        on the fraction alone is stable, which means it keeps the order the pairs
        happened to be evaluated in - and that order comes from the set sizes.
        """
        report = duplicates.rank(signatures(self.estate()))

        self.assertEqual(
            [o.fraction for o in report.overlaps], [0.5, 0.5], "the two must be tied to tie"
        )
        self.assertEqual(
            [(o.left, o.right) for o in report.overlaps],
            [("alpha", "xray"), ("yankee", "zulu")],
        )

    def test_two_processes_with_different_hash_seeds_print_the_same_bytes(self):
        """Breaks if any ordering in the report comes from a hashed collection.

        In-process reruns cannot catch this: string hashing is randomised per
        process, so a set- or dict-ordered ranking is stable within one run and
        differs between two. That has broken this repository before and was
        invisible until someone diffed two builds.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "graph.json"
            path.write_text(json.dumps(graph(self.estate())), encoding="utf-8")
            outputs = [self._in_subprocess(path, seed) for seed in ("0", "1", "524287")]

        self.assertIn("alpha", outputs[0], "the subprocess produced no report to compare")
        self.assertEqual(len(set(outputs)), 1, f"the three runs disagree: {outputs}")

    def _in_subprocess(self, path: Path, seed: str) -> str:
        script = (
            "import sys;"
            "from pathlib import Path;"
            "from knowledgestore import duplicate_repositories as d;"
            "print('\\n'.join(d.lines(d.near_duplicates(Path(sys.argv[1])))))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(SRC)},
        )
        return completed.stdout


class ScanTest(GraphFixture):
    def test_a_node_with_neither_label_nor_path_contributes_no_signature(self):
        """Breaks if structural nodes are given a signature.

        Newer graphify emits Java package-hierarchy nodes with neither `label` nor
        `source_file`. `("", "")` is a pair every repository would then hold exactly
        once, so two repositories with nothing whatsoever in common would be
        reported as overlapping - a finding manufactured by the instrument.
        """
        path = self._graph_file(
            {
                "nodes": [
                    {"id": "alpha::pkg", "repo": "alpha"},
                    {"id": "beta::pkg", "repo": "beta"},
                ],
                "links": [],
            }
        )

        scanned = duplicates.scan(path)

        self.assertEqual(scanned.by_repo, {})
        self.assertEqual(scanned.nodes, 0)
        self.assertEqual(duplicates.rank(scanned.by_repo).overlaps, ())

    def test_a_path_with_no_label_is_still_a_signature(self):
        """Breaks if a node has to carry both fields to count.

        A shared path with no label is still the same file in two repositories, and
        requiring both would drop it from the measurement.
        """
        path = self._graph_file(
            {
                "nodes": [
                    {"id": "alpha::a", "source_file": "src/a.txt", "repo": "alpha"},
                    {"id": "beta::a", "source_file": "src/a.txt", "repo": "beta"},
                ],
                "links": [],
            }
        )

        report = duplicates.near_duplicates(path)

        self.assertEqual(report.overlaps[0].shared, 1)
        self.assertEqual(report.overlaps[0].fraction, 1.0)

    def test_a_repository_carrying_no_signature_is_named_as_not_compared(self):
        """Breaks if the header claims a repository was compared when it was not.

        A repository whose nodes are all structural carries no (label, path) pair, so
        no pair can be formed from it and the report says nothing about it either
        way. Counting it among the compared would license a claim about an artefact
        the check never read - and "N of N repositories compared" is the same number
        printed twice, which is how a count stops being a finding.
        """
        shared = [("Widget", "src/widget.java"), ("Gadget", "src/gadget.java")]
        payload = graph({"alpha": shared, "alpha-fork": list(shared)})
        payload["nodes"].append({"id": "structural::pkg", "repo": "structural"})
        path = self._graph_file(payload)

        report = duplicates.near_duplicates(path)

        self.assertEqual((report.repositories, report.compared), (3, 2))
        self.assertIn(
            "2 repositories compared (1 carried no (label, path) pair)",
            duplicates.lines(report)[0],
        )

    def test_a_graph_with_no_repo_attribute_says_nothing_was_compared(self):
        """Breaks if a graph built without `repo` reports silence.

        Every per-repository feature keys on `n["repo"]`, and a graph lacking it
        yields no signatures at all. Printing nothing would read as an estate with
        no near-copies - the #104 class, where a broken precondition presents as a
        clean result and sends an operator to check a manifest that is correct.
        """
        path = self._graph_file(
            {
                "nodes": [
                    {"id": "a", "label": "Widget", "source_file": "src/widget.java"},
                    {"id": "b", "label": "Widget", "source_file": "src/widget.java"},
                ],
                "links": [],
            }
        )

        printed = duplicates.lines(duplicates.near_duplicates(path))

        self.assertEqual(len(printed), 1)
        self.assertIn("`repo` attribute", printed[0])
        self.assertIn("Not a clean result", printed[0])


class StatusDuplicatesTest(SettingsIsolated):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "graphify-out").mkdir(parents=True)
        self._old_root = config.ROOT
        config.configure(root=str(self.root))

    def tearDown(self):
        config.configure(root=str(self._old_root))
        self._tmp.cleanup()
        super().tearDown()

    def _write_graph(self, text: str) -> None:
        config.GRAPH_PATH.write_text(text, encoding="utf-8")

    def _run(self, argv):
        out = io_module.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            with contextlib.suppress(Exception):
                status.main(argv)
        return out.getvalue()

    def test_the_flag_reports_the_ranking(self):
        """Breaks if the report is written and never reaches `main`.

        Reporting through the function while nothing drives the CLI is the most
        repeated escape in this repository's mutation gate.
        """
        shared = [("Widget", "src/widget.java"), ("Gadget", "src/gadget.java")]
        self._write_graph(json.dumps(graph({"alpha": shared, "alpha-fork": list(shared)})))

        output = self._run(["--duplicates"])

        self.assertIn("Repository (label, path) overlap", output)
        self.assertIn("alpha / alpha-fork", output)
        self.assertIn("100.0%", output)

    def test_without_the_flag_it_stays_silent(self):
        """Breaks if the streamed pass runs on every `status`.

        `status` is the cheap stage an operator runs constantly and its docstring
        promises it never reads the graph. A pass over the whole graph on every
        invocation would get the flag turned off rather than kept.
        """
        shared = [("Widget", "src/widget.java")]
        self._write_graph(json.dumps(graph({"alpha": shared, "alpha-fork": list(shared)})))

        output = self._run([])

        self.assertNotIn("Repository (label, path) overlap", output)

    def test_the_graph_is_only_read_when_the_flag_is_given(self):
        """Breaks if `status` reads the graph without being asked.

        Observed at the IO boundary rather than by counting calls: a graph whose
        node array never closes raises `TruncatedJson` when it is streamed and
        cannot raise anything when it is not. A default run must not touch it.
        """
        self._write_graph('{"nodes": [{"id": "a", "label": "A", "repo": "alpha"}')

        quiet = io_module.StringIO()
        with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            try:
                status.main([])
            except graph_stream.TruncatedJson:
                self.fail("`status` streamed the graph without being asked for --duplicates")
            except Exception:
                # A temporary root is not a git checkout, so an unrelated failure
                # later in `main` is not this test's business. The point is which
                # exception did not come out of it.
                pass

            with self.assertRaises(graph_stream.TruncatedJson):
                status.main(["--duplicates"])

    def test_an_absent_graph_is_named_rather_than_reported_as_clean(self):
        """Breaks if a store with no graph prints nothing under the flag.

        Silence is this report's answer for "no near-copies", so it must not also
        be its answer for "no graph to look at".
        """
        output = self._run(["--duplicates"])

        self.assertIn("Repository overlap: no graph at", output)


if __name__ == "__main__":
    unittest.main()
