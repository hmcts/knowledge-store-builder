"""The dangling-endpoint measurement: what it counts, and what it refuses to count.

The stage exists because three estates measured the same question and got rates
three orders of magnitude apart, so the answer cannot be decided centrally and a
store has to compute its own. That makes the *instrument* the thing worth
testing: a rate that is quietly wrong is worse than no rate, because it will be
quoted once and acted on.

Every test below names the production change that should make it fail. The two
that carry the most weight are the ambiguity split - which is what keeps the
recovered total from promising recoveries a repair would have to refuse - and
the two anti-vacuity cases, where a walk that read nothing must not report a
clean rate.
"""

from __future__ import annotations

import contextlib
import io as stdio
import json
import gzip
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated

from knowledgestore import cli, config, measure_dangling_endpoints as stage


def write_graph(path: Path, nodes: list[dict], links: list[dict]) -> Path:
    """A per-repository graph in graphify's node-link JSON, gzipped by suffix."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"nodes": nodes, "links": links})
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def clone_graph(
    root: Path, repository: str, nodes: list[dict], links: list[dict], name="graph.json"
):
    return write_graph(root / "repositories" / repository / "graphify-out" / name, nodes, links)


class EntityNaming(unittest.TestCase):
    def test_the_final_scope_segment_is_the_entity_name(self):
        """Break: matching on the whole id.

        graphify's node ids are path-and-scope qualified while its dangling
        endpoints arrive as bare names, so a whole-id predicate can never match
        and returns a clean 0.0% - correct code answering a neighbouring
        question, which is the failure this repository has shipped most often.
        """
        self.assertEqual(stage.entity_name("src/main/java/Cart.java::AddItem"), "additem")
        self.assertEqual(stage.entity_name("AddItem"), "additem")
        self.assertEqual(stage.entity_name(None), "")

    def test_a_node_answering_to_one_name_twice_counts_once(self):
        """Break: `node_keys` returning a list rather than a set.

        A node whose `local_id` already equals its id's final segment is the
        common case, so counting it twice would make almost every endpoint
        ambiguous - an ambiguous count inflated by the instrument, which reads
        as caution while hiding the real number.
        """
        self.assertEqual(stage.node_keys({"id": "pkg::Cart", "local_id": "Cart"}), {"cart"})


class Classification(SettingsIsolated):
    """The three buckets, on one graph holding all three kinds at once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config.configure(root=self.root)
        self.graph = clone_graph(
            self.root,
            "alpha",
            nodes=[
                {"id": "src/Cart.java::AddItem", "local_id": "AddItem", "label": "AddItem"},
                {"id": "src/Cart.java::Checkout", "local_id": "Checkout", "label": "Checkout"},
                {"id": "src/Basket.java::Checkout", "local_id": "Checkout", "label": "Checkout"},
                {"id": "src/Cart.java", "local_id": "Cart.java", "label": "Cart.java"},
            ],
            links=[
                # recoverable: names one node, under a different id
                {"source": "src/Cart.java", "target": "additem", "relation": "declares"},
                # ambiguous: two nodes answer to this name
                {"source": "src/Cart.java", "target": "checkout", "relation": "calls"},
                # absent: a standard-library symbol the extractor drops
                {"source": "src/Cart.java", "target": "arraylist", "relation": "uses"},
            ],
        )
        self.measured = stage.measure_graph(self.graph)

    def test_an_endpoint_naming_exactly_one_node_is_recoverable(self):
        """Break: classify never returning RECOVERABLE.

        The rate would read 0.0% on every estate, and the issue would be closed
        on a false negative produced by the instrument.
        """
        self.assertEqual(self.measured.classified[stage.RECOVERABLE], ["additem"])

    def test_an_endpoint_naming_two_nodes_is_ambiguous_and_not_recovered(self):
        """Break: folding `> 1` into the recovered total.

        This is the constraint the whole measurement rests on. A repair built on
        this number resolves only where the name is unambiguous, so counting the
        ambiguous ones as recovered promises recoveries the repair must refuse
        to make - and the promise is invisible, because the totals still add up.
        """
        self.assertEqual(self.measured.classified[stage.AMBIGUOUS], ["checkout"])
        self.assertNotIn("checkout", self.measured.classified[stage.RECOVERABLE])

    def test_an_endpoint_naming_no_node_is_absent(self):
        """Break: treating an unmatched name as recoverable.

        Most dangling endpoints on most estates are external and
        standard-library symbols the extractor drops deliberately; materialising
        them would give `ArrayList` a labelled node nobody asked for.
        """
        self.assertEqual(self.measured.classified[stage.ABSENT], ["arraylist"])

    def test_the_three_buckets_partition_the_dangling_endpoints(self):
        """Break: an endpoint counted in two buckets, or in none.

        A bucket tally that sums to more than the population it describes is a
        wrong measurement this codebase has shipped before, and it reconciles
        internally while being wrong.
        """
        counts = [
            self.measured.count(k) for k in (stage.RECOVERABLE, stage.AMBIGUOUS, stage.ABSENT)
        ]
        self.assertEqual(sum(counts), self.measured.dangling)
        self.assertEqual(self.measured.dangling, 3)

    def test_the_rate_is_recovered_over_dangling_and_excludes_ambiguity(self):
        """Break: a rate computed over recoverable + ambiguous.

        Derived by hand: one recoverable of three dangling endpoints is 33.3%.
        Counting the ambiguous one as recovered would print 66.7%, which is the
        number that would justify building the repair.
        """
        text = "\n".join(stage.report([self.measured]))
        self.assertIn("Recovery rate: 33.3% (1 of 3 dangling endpoints)", text)

    def test_the_edge_count_is_edges_and_not_endpoints(self):
        """Break: incrementing the edge tally once per dangling endpoint.

        Three edges each carry one dangling endpoint here. An edge-shaped count
        that actually counts endpoints inflates 'how much of the graph is
        affected', which is the figure that sizes the problem.
        """
        self.assertEqual(self.measured.edges, 3)
        self.assertEqual(self.measured.dangling_edges, 3)
        self.assertEqual(self.measured.endpoints, 4)


class LocalIdIsWhatMakesRecoveryPossible(SettingsIsolated):
    def test_a_node_is_found_by_local_id_when_its_id_shares_nothing(self):
        """Break: indexing node ids only.

        The issue's whole claim is that `local_id` carries the entity name. A
        node whose id is namespaced differently from the endpoint is found only
        through it, so an index built from ids alone reports the estate with the
        highest rate as having none.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            graph = clone_graph(
                Path(tmp),
                "alpha",
                nodes=[
                    {"id": "chunk-7::node-1", "local_id": "PaymentGateway", "label": "Gateway"},
                    {"id": "chunk-3::node-9", "local_id": "Ledger", "label": "Ledger"},
                ],
                links=[{"source": "chunk-3::node-9", "target": "paymentgateway"}],
            )
            measured = stage.measure_graph(graph)
        self.assertEqual(measured.classified[stage.RECOVERABLE], ["paymentgateway"])


class Determinism(SettingsIsolated):
    def test_named_endpoints_come_out_sorted_whatever_order_they_were_read_in(self):
        """Break: printing a set, or the order the edges happened to be read in.

        Stage output is diffed between builds, so an unsorted list makes two
        runs on identical inputs differ - and hash randomisation makes that
        invisible until someone diffs two builds in different processes.

        Twenty endpoints, fed in descending order, deliberately. With three, a
        set's arbitrary order can coincide with the sorted one often enough that
        the mutation gate would flake; with twenty the coincidence is one in
        twenty factorial, so this either holds or it does not.
        """
        expected = [f"entity{index:02d}" for index in range(20)]
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            graph = clone_graph(
                Path(tmp),
                "alpha",
                nodes=[{"id": "f", "local_id": "f"}]
                + [{"id": f"f::{name}", "local_id": name} for name in expected],
                links=[{"source": "f", "target": name} for name in reversed(expected)],
            )
            measured = stage.measure_graph(graph)
        self.assertEqual(measured.classified[stage.RECOVERABLE], expected)
        self.assertIn(
            "  recoverable, first 5 by id: entity00, entity01, entity02, entity03, entity04 "
            "(+15 more)",
            "\n".join(stage.report([measured])),
        )


class AntiVacuity(SettingsIsolated):
    """A measurement of nothing must never read like a clean result."""

    def _run(self, *argv: str) -> tuple[int, str]:
        out = stdio.StringIO()
        with contextlib.redirect_stdout(out):
            code = stage.main(list(argv))
        return code, out.getvalue()

    def test_a_walk_that_finds_no_graphs_fails_rather_than_reporting_a_rate(self):
        """Break: returning 0 with an empty total.

        Every stage in this library that shipped doing nothing did so with a
        passing suite. An empty walk printing '0 dangling endpoints' is
        indistinguishable from an estate with none.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            code, text = self._run()
        self.assertEqual(code, 1)
        self.assertIn("No per-repository graph found", text)
        self.assertNotIn("Recovery rate", text)

    def test_the_merged_estate_graph_is_named_as_not_a_substitute(self):
        """Break: dropping the sentence about the post-merge graph.

        One estate's first measurement came back at zero and was a tautology: it
        read the file its own merge had already cleaned. The store root almost
        always holds that file, so it is the one an operator reaches for next -
        and reading it makes this rate zero by construction.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            write_graph(Path(tmp) / "graphify-out" / "graph.json", [], [])
            code, text = self._run()
        self.assertEqual(code, 1)
        self.assertIn("is not a substitute", text)
        self.assertIn("post-merge", text)

    def test_a_graph_with_no_edges_is_not_measured(self):
        """Break: treating an edgeless graph as a measured zero.

        No edges means no endpoints, so every count is zero and the rate is
        undefined - reported as a result it says the store is clean.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            clone_graph(Path(tmp), "alpha", nodes=[{"id": "a", "local_id": "a"}], links=[])
            code, text = self._run()
        self.assertEqual(code, 1)
        self.assertIn("None of those graphs could be measured", text)
        self.assertNotIn("Recovery rate", text)

    def test_a_named_graph_that_does_not_exist_fails(self):
        """Break: silently skipping a missing --graph.

        Naming an input that was never read, and reporting on the rest, is how
        a total comes to describe fewer artefacts than the operator listed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            code, text = self._run("--graph", str(Path(tmp) / "absent.json"))
        self.assertEqual(code, 1)
        self.assertIn("No such graph file", text)

    def test_no_endpoint_dangling_is_reported_as_measured_not_as_a_rate(self):
        """Break: printing '0.0%' when nothing dangled.

        A genuine zero is a legitimate answer and one estate has it - but it is
        a different statement from '0.0% of them were recoverable', and only one
        of the two survives being quoted.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            clone_graph(
                Path(tmp),
                "alpha",
                nodes=[{"id": "a", "local_id": "a"}, {"id": "b", "local_id": "b"}],
                links=[{"source": "a", "target": "b"}],
            )
            code, text = self._run()
        self.assertEqual(code, 0)
        self.assertIn("Recovery rate: not defined", text)


class SaysWhatItRead(SettingsIsolated):
    def _run(self, *argv: str) -> tuple[int, str]:
        out = stdio.StringIO()
        with contextlib.redirect_stdout(out):
            code = stage.main(list(argv))
        return code, out.getvalue()

    def test_every_graph_read_is_named_in_the_output(self):
        """Break: printing totals without the files behind them.

        A tool's own count is not verification. Naming each input is what lets
        an operator reconcile the total against the clones they expected, and
        what makes 'which artefact did this describe' answerable at all.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            for repository in ("beta", "alpha"):
                clone_graph(
                    Path(tmp),
                    repository,
                    nodes=[{"id": "a", "local_id": "a"}],
                    links=[{"source": "a", "target": "missing"}],
                )
            code, text = self._run()
        self.assertEqual(code, 0)
        self.assertIn("repositories/alpha/graphify-out/graph.json", text)
        self.assertIn("repositories/beta/graphify-out/graph.json", text)
        self.assertIn("Read 2 per-repository graph(s)", text)

    def test_one_clone_holding_both_graph_forms_is_measured_once_and_says_so(self):
        """Break: measuring both files in a clone.

        Both counts would double while the rate stayed put - an error that
        reconciles internally, which is the kind nobody finds. The unread file
        is named because the two can disagree, and this repository has already
        paid for a stage that described a leftover graph without saying so.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            nodes = [{"id": "a", "local_id": "a"}]
            links = [{"source": "a", "target": "missing"}]
            clone_graph(Path(tmp), "alpha", nodes, links, name="graph.json")
            clone_graph(Path(tmp), "alpha", nodes, links, name="graph.json.gz")
            code, text = self._run()
        self.assertEqual(code, 0)
        self.assertIn("Read 1 per-repository graph(s)", text)
        self.assertIn("was NOT read: repositories/alpha/graphify-out/graph.json.gz", text)
        self.assertIn("0 of 1 dangling endpoints", text)

    def test_a_gzipped_per_repository_graph_is_read(self):
        """Break: dispatching on the suffix being lost.

        A store that compresses its per-repository graphs would otherwise get
        'no graphs found' - or worse, an unreadable-file line - for graphs that
        are present and fine.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            clone_graph(
                Path(tmp),
                "alpha",
                nodes=[{"id": "f::Cart", "local_id": "Cart"}, {"id": "f", "local_id": "f"}],
                links=[{"source": "f", "target": "cart"}],
                name="graph.json.gz",
            )
            code, text = self._run()
        self.assertEqual(code, 0)
        self.assertIn("Recovery rate: 100.0% (1 of 1 dangling endpoints)", text)


class TheStageIsWiredAndWritesNothing(SettingsIsolated):
    def test_the_cli_drives_the_walk_and_the_report(self):
        """Break: the helpers work and nothing drives `main()`.

        The most repeated escape in this repository: behaviour tested through a
        function while the stage that ships never calls it. Driven through
        `cli.main` rather than `stage.main` so the stage table, the module name
        and the argument handling are all exercised.
        """
        with tempfile.TemporaryDirectory() as tmp:
            clone_graph(
                Path(tmp),
                "alpha",
                nodes=[{"id": "f::Cart", "local_id": "Cart"}, {"id": "f", "local_id": "f"}],
                links=[{"source": "f", "target": "cart"}, {"source": "f", "target": "arraylist"}],
            )
            out = stdio.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--root", tmp, "dangling-endpoints"])
        self.assertEqual(code, 0)
        self.assertIn("Recovery rate: 50.0% (1 of 2 dangling endpoints)", out.getvalue())

    def test_the_stage_leaves_the_graph_byte_identical(self):
        """Break: a measurement that repairs.

        The issue asks for a number and explicitly not a fix, because the fix
        differs by estate. A stage that rewrote the graph would change a
        consumer's committed data on the strength of a measurement nobody has
        agreed to act on yet.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            graph = clone_graph(
                Path(tmp),
                "alpha",
                nodes=[{"id": "f::Cart", "local_id": "Cart"}, {"id": "f", "local_id": "f"}],
                links=[{"source": "f", "target": "cart"}],
            )
            before = graph.read_bytes()
            with contextlib.redirect_stdout(stdio.StringIO()):
                code = stage.main([])
            after = graph.read_bytes()
        self.assertEqual(code, 0)
        self.assertEqual(after, before)

    def test_the_json_output_reconciles_with_the_printed_report(self):
        """Break: two code paths computing the same total differently.

        The JSON is what a store will script against, so a total that disagrees
        with the printed one hands two different answers to the same question.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            clone_graph(
                Path(tmp),
                "alpha",
                nodes=[
                    {"id": "f::Cart", "local_id": "Cart"},
                    {"id": "g::Cart", "local_id": "Cart"},
                    {"id": "f", "local_id": "f"},
                ],
                links=[{"source": "f", "target": "cart"}, {"source": "f", "target": "arraylist"}],
            )
            target = Path(tmp) / "out" / "dangling.json"
            with contextlib.redirect_stdout(stdio.StringIO()):
                code = stage.main(["--json", str(target)])
            written = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        totals = written["totals"]
        self.assertEqual(totals["dangling"], 2)
        self.assertEqual(totals[stage.AMBIGUOUS], 1)
        self.assertEqual(totals[stage.ABSENT], 1)
        self.assertEqual(totals[stage.RECOVERABLE], 0)
        self.assertEqual(
            totals[stage.RECOVERABLE] + totals[stage.AMBIGUOUS] + totals[stage.ABSENT],
            totals["dangling"],
        )
        self.assertEqual(written["graphs"][0]["path"], "repositories/alpha/graphify-out/graph.json")

    def test_an_unreadable_graph_is_named_rather_than_taking_the_stage_down(self):
        """Break: letting a truncated file raise out of the run.

        One bad clone among many would otherwise lose the measurement for the
        whole estate, and the operator would have no way to tell which file did
        it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            broken = Path(tmp) / "repositories" / "alpha" / "graphify-out" / "graph.json"
            broken.parent.mkdir(parents=True)
            broken.write_text('{"nodes": [], "links": [{"source": "a"', encoding="utf-8")
            clone_graph(
                Path(tmp),
                "beta",
                nodes=[{"id": "f::Cart", "local_id": "Cart"}, {"id": "f", "local_id": "f"}],
                links=[{"source": "f", "target": "cart"}],
            )
            out = stdio.StringIO()
            with contextlib.redirect_stdout(out):
                code = stage.main([])
        self.assertEqual(code, 0)
        self.assertIn("UNREADABLE", out.getvalue())
        self.assertIn("Recovery rate: 100.0% (1 of 1 dangling endpoints)", out.getvalue())


class TheStageIsRegistered(unittest.TestCase):
    def test_it_is_a_stage_that_parses_its_own_arguments(self):
        """Break: a self-parsing stage left out of SELF_PARSING.

        `--help` would then fall through to the stage and run it. Harmless here
        and not harmless in general, which is why the list is checked rather
        than trusted.
        """
        self.assertIn("dangling-endpoints", cli.STAGES)
        self.assertIn("dangling-endpoints", cli.SELF_PARSING)


if __name__ == "__main__":
    unittest.main()
