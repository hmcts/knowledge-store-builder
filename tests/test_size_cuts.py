"""Sizing a candidate content cut by what survives it (#116).

The issue's own history is what these tests defend. A structural prune - keep
every node with a cross-file edge - was proposed, measured and withdrawn, because
cross-file connectivity turned out to mark ordinary imports rather than
relevance. What survived is a content cut declared by whoever owns the store, and
the one measurement that decides between candidates: **surviving edges**, an edge
surviving only when both of its endpoints do.

So the central test here is that an edge with one surviving endpoint is not
counted. Everything else follows from it: a cut keeping only file-level nodes kept
tens of thousands of them and low hundreds of edges on the estate that reported
this, and it is the most attractive candidate of its generation by node count
alone. A stage that counted an edge per surviving endpoint would report that cut
as keeping a graph, and nothing downstream would disagree until clustering.

The second theme is silence. A rule that matches nothing, a cut that keeps
nothing, a graph file that was not read: all three look exactly like a clean run,
and each has a named line in the report.
"""

from __future__ import annotations

import io as stdio
import contextlib
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import cli, config, size_cuts, telemetry  # noqa: E402


def _node(node_id: str, source_file: str = "", repo: str = "", kind: str = "") -> dict:
    """A graphify-shaped AST node: `source_file`, `repo` and a top-level `type`."""
    node: dict = {"id": node_id, "label": node_id}
    if source_file:
        node["source_file"] = source_file
    if repo:
        node["repo"] = repo
    if kind:
        node["type"] = kind
    return node


def _write_graph(path: Path, nodes: list[dict], links: list[tuple[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "nodes": nodes,
        "links": [{"source": source, "target": target} for source, target in links],
    }
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _cuts(text: str) -> list[size_cuts.Cut]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "content-cuts.txt"
        path.write_text(text, encoding="utf-8")
        return size_cuts.read_cuts(path)


class SurvivingEdges(SettingsIsolated):
    """The measurement the issue exists for: both endpoints, or the edge is gone."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_an_edge_with_one_surviving_endpoint_is_not_kept(self):
        """Breaks if the endpoint test becomes `or` - the wrong measurement entirely.

        Hand-derived: the cut keeps `main.tf` only. Both edges join it to a node
        the cut drops, so nothing joins two survivors and the surviving edge count
        is 0 against a layer of 2. Counting an edge per surviving endpoint would
        report 2, and a candidate that strands every node it keeps would then look
        like a graph.
        """
        graph = _write_graph(
            self.tmp / "graph.json",
            [
                _node("tf", "infrastructure/main.tf", "orchard-api"),
                _node("java1", "src/main/java/Handler.java", "orchard-api"),
                _node("java2", "src/main/java/Client.java", "orchard-api"),
            ],
            [("tf", "java1"), ("java2", "tf")],
        )
        cuts = _cuts("cut iac\nfile *.tf\n")

        sizing = size_cuts.size([graph], cuts)

        self.assertEqual(sizing.nodes, 3)
        self.assertEqual(sizing.edges, 2)
        self.assertEqual(sizing.kept_nodes["iac"], 1)
        self.assertEqual(sizing.kept_edges.get("iac", 0), 0)

    def test_an_edge_between_two_survivors_is_kept(self):
        """Breaks if the endpoint test is inverted or the mask never reaches the edges.

        The companion to the test above: with `and` mis-wired as "neither
        endpoint", or with the per-cut bit never set, both tests cannot pass at
        once. Hand-derived: of three edges only `tf -> tfvars` joins two nodes the
        cut keeps.
        """
        graph = _write_graph(
            self.tmp / "graph.json",
            [
                _node("tf", "infrastructure/main.tf", "orchard-api"),
                _node("vars", "infrastructure/prod.tfvars", "orchard-api"),
                _node("java", "src/main/java/Handler.java", "orchard-api"),
            ],
            [("tf", "vars"), ("tf", "java"), ("java", "vars")],
        )

        sizing = size_cuts.size([graph], _cuts("cut iac\nfile *.tf\nfile *.tfvars\n"))

        self.assertEqual(sizing.kept_nodes["iac"], 2)
        self.assertEqual(sizing.kept_edges["iac"], 1)

    def test_a_file_level_cut_keeps_mass_and_no_structure(self):
        """The issue's counter-intuitive result, in miniature.

        Breaks if node counting and edge counting are ever wired to the same
        predicate: this cut keeps every one of the three file nodes and none of
        the four edges, because graphify's AST edges join symbols rather than
        files. A stage reporting node counts alone cannot tell this apart from a
        cut that kept a working graph, which is why the surviving-edge count is
        the thing being computed.
        """
        nodes = [_node(f"file{i}", f"src/f{i}.java", "orchard-api", "file") for i in (1, 2, 3)]
        nodes += [_node(f"sym{i}", f"src/f{i}.java", "orchard-api", "method") for i in (1, 2, 3)]
        graph = _write_graph(
            self.tmp / "graph.json",
            nodes,
            [("sym1", "sym2"), ("sym2", "sym3"), ("sym1", "file1"), ("sym3", "file3")],
        )

        sizing = size_cuts.size([graph], _cuts("cut files-only\nkind file\n"))

        self.assertEqual(sizing.kept_nodes["files-only"], 3)
        self.assertEqual(sizing.kept_edges.get("files-only", 0), 0)

    def test_ids_are_not_shared_between_graph_files(self):
        """Breaks if one id table is reused across files, which fabricates survivors.

        Per-repository graphs reuse ids freely - a `README.md` node exists in every
        repository - and an edge's endpoints are always in the same file as the
        edge. Hand-derived: `orchard-api`'s edge joins a kept `.tf` node to a Java
        node the cut drops, and `tundra-platform` keeps a node under that same
        dropped id. Sharing one table lets the second file's node satisfy the first
        file's edge, so the cut reports 1 surviving edge where the correct answer
        is 0.
        """
        first = _write_graph(
            self.tmp / "orchard-api" / "graph.json",
            [
                _node("shared", "src/main/java/Handler.java", "orchard-api"),
                _node("tf", "infrastructure/main.tf", "orchard-api"),
            ],
            [("tf", "shared")],
        )
        second = _write_graph(
            self.tmp / "tundra-platform" / "graph.json",
            [_node("shared", "infrastructure/network.tf", "tundra-platform")],
            [],
        )

        sizing = size_cuts.size([first, second], _cuts("cut iac\nfile *.tf\n"))

        self.assertEqual(sizing.kept_nodes["iac"], 2)
        self.assertEqual(sizing.kept_edges.get("iac", 0), 0)


class RuleSemantics(SettingsIsolated):
    """Union within an axis, intersection across axes, and the `not-` forms."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.graph = _write_graph(
            Path(self._tmp.name) / "graph.json",
            [
                _node("a", "src/main/java/Handler.java", "orchard-api", "class"),
                _node("b", "src/main/java/Handler.java", "orchard-api", "method"),
                _node("c", "infrastructure/main.tf", "orchard-api", "resource"),
                _node("d", "infrastructure/main.tf", "tundra-infrastructure", "resource"),
            ],
            [],
        )

    def _kept(self, text: str) -> int:
        cuts = _cuts(text)
        return size_cuts.size([self.graph], cuts).kept_nodes.get(cuts[0].name, 0)

    def test_two_rules_on_one_axis_widen_the_cut(self):
        """Breaks if rules on one axis intersect - which would keep nothing at all.

        No node is both a `.tf` and a `.java` file, so an axis read as `and`
        reports 0 where the answer is 4.
        """
        self.assertEqual(self._kept("cut both\nfile *.java\nfile *.tf\n"), 4)

    def test_two_axes_narrow_the_cut(self):
        """Breaks if axes union - the case that makes a declaration-kind cut possible.

        `file *.java` alone keeps 2 and `kind class` alone keeps 1; unioned they
        would keep 2, and intersected they keep the one Java class. Without this
        there is no way to say "the declarations of this language, not its
        callables".
        """
        self.assertEqual(self._kept("cut java-classes\nfile *.java\nkind class\n"), 1)

    def test_a_not_rule_excludes_whatever_else_matched(self):
        """Breaks if a `not-` rule is read as a selector, inverting the cut.

        Hand-derived: two `.java` nodes, one of them a method, so dropping the
        callables leaves 1.
        """
        self.assertEqual(self._kept("cut declarations\nfile *.java\nnot-kind method\n"), 1)

    def test_an_axis_the_cut_does_not_name_is_unconstrained(self):
        """Breaks if a missing axis is treated as matching nothing.

        A cut naming only `repo` must keep both of that repository's `.tf` and
        `.java` nodes; requiring every axis would silently keep none, and adding a
        second axis to a working cut would empty it.
        """
        self.assertEqual(self._kept("cut one-repo\nrepo tundra-*\n"), 1)
        self.assertEqual(self._kept("cut other-repo\nrepo orchard-*\n"), 3)

    def test_a_repo_rule_works_on_the_layer_as_extracted(self):
        """Breaks if the `repo` axis needs an attribute the default input has not got.

        `merge-graphs` is what adds `repo`, so on the per-repository layer - the
        stage's own default input - no node carries one, and every `repo` rule
        would select nothing while the report called it a rule with nothing to do.
        The fallback is the graph file's `repositories/<name>/` segment, so this
        fixture's nodes carry no `repo` attribute at all.
        """
        graph = _write_graph(
            Path(self._tmp.name) / "repositories" / "tundra-platform" / "graph.json",
            [_node("a", "main.tf"), _node("b", "app.java")],
            [],
        )
        cuts = _cuts("cut by-repo\nrepo tundra-*\n")

        sizing = size_cuts.size([graph], cuts)

        self.assertEqual(sizing.kept_nodes["by-repo"], 2)

    def test_a_glob_crosses_directories_without_a_double_star(self):
        """Breaks if matching is anchored at a path segment.

        The rule an operator will write most often is an extension, and it has to
        reach a file at any depth. `*.tf` matching only a root-level file is the
        silent-narrowing failure this pipeline keeps paying for.
        """
        self.assertEqual(self._kept("cut deep\nfile *.tf\n"), 2)

    def test_a_node_with_no_source_file_falls_out_of_a_file_cut(self):
        """Breaks if an absent attribute is read as an empty match.

        Newer graphify emits package-hierarchy nodes with neither label nor
        `source_file`. Treating absence as a match would sweep every one of them
        into a cut that names a file type.
        """
        graph = _write_graph(
            Path(self._tmp.name) / "structural.json",
            [_node("pkg"), _node("tf", "main.tf")],
            [],
        )
        cuts = _cuts("cut iac\nfile *.tf\n")
        self.assertEqual(size_cuts.size([graph], cuts).kept_nodes["iac"], 1)

    def test_a_kind_is_read_from_every_shape_that_carries_one(self):
        """Breaks if only one of the three node generations is understood.

        The library writes `metadata.kind`, graphify writes a top-level `type`,
        and a store's own extraction has written a top-level `kind`. A `kind` rule
        reading one of them selects nothing on the graphs written by the other
        two, and reports that as a rule with nothing to do.
        """
        graph = _write_graph(
            Path(self._tmp.name) / "kinds.json",
            [
                {"id": "meta", "metadata": {"kind": "feature"}},
                {"id": "typed", "type": "feature"},
                {"id": "kinded", "kind": "feature"},
                {"id": "other", "type": "method"},
            ],
            [],
        )
        cuts = _cuts("cut features\nkind feature\n")
        self.assertEqual(size_cuts.size([graph], cuts).kept_nodes["features"], 3)


class CutsFileRefusals(SettingsIsolated):
    """Every refusal is a rule that would otherwise measure something else in silence."""

    def test_a_double_star_glob_is_refused(self):
        """Breaks if `**` is accepted and read as a single `*`.

        `**/*.tf` then requires a directory separator, so it silently excludes a
        `.tf` file at a repository's root while reading as though it included
        everything. A rule that matches less than it says is this repository's
        most expensive defect class.
        """
        with self.assertRaises(size_cuts.CutError) as refused:
            _cuts("cut iac\nfile **/*.tf\n")
        self.assertIn("**", str(refused.exception))
        self.assertIn("line 2", str(refused.exception))

    def test_a_name_that_cannot_be_a_metric_segment_is_refused(self):
        """Breaks if a name reaches the telemetry record unvalidated.

        The record rejects a metric that is not `stage.measurement` in lower case,
        so an upper-case or underscored cut name would fail at the end of a full
        sizing run rather than when the file was read. Underscores are refused
        rather than translated, because `iac_anywhere` and `iac-anywhere` would
        then be one metric.
        """
        for name in ("IaC", "iac_anywhere", "2nd-try"):
            with self.subTest(name=name), self.assertRaises(size_cuts.CutError):
                _cuts(f"cut {name}\nfile *.tf\n")

    def test_a_duplicate_cut_name_is_refused(self):
        """Breaks if two candidates can share a name - the second overwrites the first.

        One name is one report row and one pair of metrics, so the second
        declaration's counts would replace the first's with nothing said.
        """
        with self.assertRaises(size_cuts.CutError):
            _cuts("cut iac\nfile *.tf\ncut iac\nfile *.hcl\n")

    def test_a_cut_with_no_rules_is_refused(self):
        """Breaks if a truncated file is measured as a cut that keeps everything.

        A candidate with no rules keeps every node, so it would report the layer's
        own totals as a cut's - the report already carries those, and the
        duplicate would read as a candidate somebody chose.
        """
        with self.assertRaises(size_cuts.CutError):
            _cuts("cut iac\n")

    def test_a_rule_before_any_cut_is_refused(self):
        """Breaks if a stray rule is attached to whatever candidate follows it.

        The likely cause is a deleted `cut` line, and silently attaching the
        orphaned rules to the next candidate changes what that candidate measures.
        """
        with self.assertRaises(size_cuts.CutError):
            _cuts("file *.tf\ncut iac\nfile *.hcl\n")

    def test_an_unknown_rule_word_is_refused(self):
        """Breaks if an unrecognised line is skipped, which drops a rule silently.

        `exclude *.java` is the plausible mistake - the repository-filters file
        spells its negation that way - and skipping it would size a cut that keeps
        the very content the operator wrote a line to remove.
        """
        with self.assertRaises(size_cuts.CutError) as refused:
            _cuts("cut iac\nexclude *.java\n")
        self.assertIn("not-", str(refused.exception))

    def test_comments_and_blank_lines_are_ignored(self):
        """Breaks if the file cannot be annotated, which is where the intent lives.

        A cut is a statement of what the store is for; the reason belongs beside
        the rule, as it does in the repository-filters file.
        """
        cuts = _cuts("# why this cut exists\n\ncut iac\n\n# the estate's IaC\nfile *.tf\n")
        self.assertEqual([cut.name for cut in cuts], ["iac"])
        self.assertEqual([rule.describe() for rule in cuts[0].rules], ["file *.tf"])

    def test_declaration_order_is_preserved(self):
        """Breaks if candidates are sorted, which reorders the operator's argument.

        The file is usually written widest-first so the rows can be read against
        each other; sorting by name would scatter that.
        """
        cuts = _cuts("cut wide\nfile *.tf\ncut narrow\nfile *.tfvars\n")
        self.assertEqual([cut.name for cut in cuts], ["wide", "narrow"])

    def test_a_cuts_path_that_climbs_out_of_the_store_is_refused(self):
        """Breaks if `--cuts` can open a file outside the store it names.

        The argument comes from the command line, so whatever built it - an
        operator, a script or an agent - chooses which file the process reads. The
        check is lexical rather than resolved, because `realpath` collapses `..`
        and would launder exactly what this rejects; the same reasoning is written
        down for the write side in `io.checked_write_target`.
        """
        with self.assertRaises(size_cuts.CutError) as refused:
            size_cuts.read_cuts(Path("config", "..", "..", "elsewhere", "content-cuts.txt"))
        self.assertIn("upward", str(refused.exception))

    def test_the_shipped_example_parses(self):
        """Breaks if the file people are told to copy stops being readable.

        `examples/content-cuts.txt` is documented as `cp`-and-edit, so a renamed
        rule word or an illustrative `**` in it would refuse on the operator's
        first run of the stage. The example is the only copy of this format that
        nothing else exercises.
        """
        example = Path(__file__).resolve().parent.parent / "examples" / "content-cuts.txt"

        cuts = size_cuts.read_cuts(example)

        self.assertTrue(cuts, f"{example} declares no candidate to check")
        for cut in cuts:
            with self.subTest(cut=cut.name):
                self.assertTrue(cut.rules)
                for rule in cut.rules:
                    self.assertIn(rule.axis, size_cuts.AXES)

    def test_a_missing_file_declares_no_candidates(self):
        """Breaks if a store with no cuts file cannot measure its own layer.

        Sizing the layer is worth doing before anybody has proposed a cut, and
        that is the state every store starts in.
        """
        self.assertEqual(size_cuts.read_cuts(Path("no", "such", "file.txt")), [])


class WhatTheReportSays(SettingsIsolated):
    """Each line here replaces something an operator would otherwise not look for."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _report(self, graphs: list[Path], text: str) -> tuple[str, str]:
        cuts = _cuts(text)
        sizing = size_cuts.size(graphs, cuts)
        out, err = stdio.StringIO(), stdio.StringIO()
        size_cuts.report(sizing, cuts, Path("config/content-cuts.txt"), out=out, err=err)
        return out.getvalue(), err.getvalue()

    def test_every_input_file_is_named_with_its_own_counts(self):
        """Breaks if only the total is printed, which is the tool agreeing with itself.

        A glob that read 61 of the 81 per-repository graphs somebody believed it
        read produces a total that reconciles perfectly against itself. The
        reconciliation an operator can actually perform is against the files they
        named, so each one is printed with its own two counts.
        """
        first = _write_graph(self.tmp / "a" / "graph.json", [_node("x", "main.tf")], [])
        second = _write_graph(
            self.tmp / "b" / "graph.json",
            [_node("y", "main.tf"), _node("z", "other.tf")],
            [("y", "z")],
        )

        out, _ = self._report([first, second], "cut iac\nfile *.tf\n")

        self.assertIn(str(first), out)
        self.assertIn(str(second), out)
        self.assertIn("3 nodes", out.replace(",", ""))
        self.assertIn("total", out)

    def test_a_rule_that_matched_nothing_is_named_with_its_line(self):
        """Breaks if an unmatched rule is silent, which reads exactly like a clean run.

        A glob written for a path form the graph does not use selects nothing and
        looks identical to a glob with nothing to select. The line number is what
        makes it fixable without re-reading the whole file.
        """
        graph = _write_graph(self.tmp / "graph.json", [_node("x", "main.tf")], [])

        _, err = self._report([graph], "cut iac\nfile *.tf\nfile *.bicep\n")

        self.assertIn("line 3", err)
        self.assertIn("*.bicep", err)
        self.assertNotIn("*.tf`", err)

    def test_a_cut_that_keeps_nodes_and_no_edges_is_named(self):
        """Breaks if the collapse is left for the reader to spot in the table.

        This is the file-level cut's shape, and the whole reason the issue asks
        for edges rather than nodes. Zero rather than a threshold: no constant
        this library ships could say how few edges is too few on an estate it has
        never seen, and none is unambiguous.
        """
        graph = _write_graph(
            self.tmp / "graph.json",
            [_node("f1", "a.java", kind="file"), _node("s1", "a.java", kind="method")],
            [("s1", "f1")],
        )

        _, err = self._report([graph], "cut files-only\nkind file\n")

        self.assertIn("files-only", err)
        self.assertIn("no edge", err)

    def test_a_cut_that_keeps_nothing_is_named(self):
        """Breaks if an empty cut is reported only as a row of zeros.

        Its usual cause is two axes that each match plenty and intersect to
        nothing, so the unmatched-rule warning stays silent and the row is the
        only evidence.
        """
        graph = _write_graph(self.tmp / "graph.json", [_node("x", "main.tf", kind="resource")], [])

        _, err = self._report([graph], "cut impossible\nfile *.tf\nkind method\n")

        self.assertIn("impossible", err)
        self.assertIn("no node", err)

    def test_the_layer_is_reported_when_no_candidate_is_declared(self):
        """Breaks if the stage needs a cuts file to say anything.

        The layer measured on its own terms is the useful half before any cut is
        proposed - and a post-cut measurement cannot characterise the raw layer,
        because a cut removes content for reasons unrelated to it being noise.
        """
        graph = _write_graph(self.tmp / "graph.json", [_node("x", "main.tf")], [])

        out, _ = self._report([graph], "")

        self.assertIn("1 nodes", out.replace(",", ""))
        self.assertIn("content-cuts.txt", out)

    def test_two_runs_over_the_same_inputs_print_the_same_bytes(self):
        """Breaks if anything iterates a set or a dict keyed by unordered data.

        Stage output here is read into decisions and diffed between refreshes, so
        a report whose rows or warnings reorder per process is unusable for the
        comparison it exists to support.
        """
        graph = _write_graph(
            self.tmp / "graph.json",
            [_node("a", "main.tf"), _node("b", "app.java"), _node("c", "prod.tfvars")],
            [("a", "c"), ("a", "b")],
        )
        text = "cut iac\nfile *.tf\nfile *.tfvars\ncut java\nfile *.java\nfile *.kt\n"

        first = self._report([graph], text)
        second = self._report([graph], text)

        self.assertEqual(first, second)
        self.assertIn("iac", first[0], "the fixture must produce a report to compare")


class TheStageAsRun(SettingsIsolated):
    """The wiring: a real store root, the real telemetry record, real exit codes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        config.configure(root=self.root)

    def _repo_graph(self, name: str, nodes: list[dict], links: list[tuple[str, str]]) -> Path:
        return _write_graph(
            self.root / "repositories" / name / "graphify-out" / "graph.json", nodes, links
        )

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = stdio.StringIO(), stdio.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = size_cuts.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _declare(self, text: str) -> None:
        config.CONTENT_CUTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.CONTENT_CUTS_PATH.write_text(text, encoding="utf-8")

    def test_it_reads_the_layer_as_extracted_by_default(self):
        """Breaks if the default input is the merged graph rather than the per-repo layer.

        The layer a store *holds* and the layer it *publishes* answer different
        questions, and conflating them is how a cut's surviving-node count came to
        be read as a statement about the raw layer's contamination. The default is
        the same glob the documented merge command takes.
        """
        self._repo_graph(
            "orchard-api", [_node("a", "main.tf"), _node("b", "app.java")], [("a", "b")]
        )
        self._repo_graph("tundra-platform", [_node("c", "network.tf")], [])
        self._declare("cut iac\nfile *.tf\n")

        code, out, _ = self._run()

        self.assertEqual(code, 0)
        self.assertIn("2 graph file(s)", out)
        recorded = telemetry.read()
        self.assertEqual(recorded["size_cuts.layer_nodes"], 3)
        self.assertEqual(recorded["size_cuts.layer_edges"], 1)
        self.assertEqual(recorded["size_cuts.layer_graphs"], 2)

    def test_a_graph_no_manifest_declares_is_still_read_and_named(self):
        """Breaks if the inputs are taken from the manifest instead of the glob.

        Extraction is manifest-driven and `merge-graphs` is directory-driven, and
        nothing reconciles the two: one store held 164 per-repository graphs
        against 163 declared repositories, because a repository was cloned and
        extracted and the manifest naming it was discarded when that refresh
        aborted. The graph stayed and the merge reads it, so a stage walking the
        manifest is blind to the one input whose commit the store cannot name -
        and reports a clean result while doing it.
        """
        config.REPOSITORIES_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        config.REPOSITORIES_CONFIG.write_text("orchard-api|main\n", encoding="utf-8")
        self._repo_graph("orchard-api", [_node("a", "main.tf")], [])
        self._repo_graph("undeclared-service", [_node("b", "network.tf")], [])

        code, out, _ = self._run("--no-record")

        self.assertEqual(code, 0)
        self.assertIn("undeclared-service", out)
        self.assertIn("2 graph file(s)", out)

    def test_the_counts_reach_the_telemetry_record_as_integers(self):
        """Breaks if a cut is recorded as a ratio, or not recorded at all.

        A stored rate cannot be re-derived, so a later refresh could not tell a
        shrinking numerator from a growing denominator - and comparison against
        the store's own previous refresh is the only comparison available, because
        two estates measured layer ratios a factor of a hundred apart.
        """
        self._repo_graph(
            "orchard-api",
            [_node("a", "main.tf"), _node("b", "prod.tfvars"), _node("c", "app.java")],
            [("a", "b"), ("a", "c")],
        )
        self._declare("cut iac-anywhere\nfile *.tf\nfile *.tfvars\n")

        code, _, _ = self._run()

        self.assertEqual(code, 0)
        self.assertEqual(
            telemetry.read(),
            {
                "size_cuts.iac_anywhere.edges": 1,
                "size_cuts.iac_anywhere.nodes": 2,
                "size_cuts.layer_edges": 2,
                "size_cuts.layer_graphs": 1,
                "size_cuts.layer_nodes": 3,
            },
        )

    def test_no_record_leaves_the_previous_refresh_alone(self):
        """Breaks if trying a candidate out destroys the baseline it is compared against.

        Sizing is exploratory - the point is to run it once per candidate - and
        each recording replaces the last refresh's counts. Without this flag the
        third attempt of an afternoon compares against the second attempt of the
        same afternoon and reports nothing moved.
        """
        self._repo_graph("orchard-api", [_node("a", "main.tf")], [])
        telemetry.record(
            {"size_cuts.layer_nodes": 41, "size_cuts.layer_edges": 7},
            out=stdio.StringIO(),
            err=stdio.StringIO(),
        )

        code, _, _ = self._run("--no-record")

        self.assertEqual(code, 0)
        self.assertEqual(telemetry.read()["size_cuts.layer_nodes"], 41)

    def test_reading_no_node_refuses_and_records_nothing(self):
        """Breaks if zeros are written over a real refresh's counts.

        The likely cause is a path that holds no `nodes` array - a store's own
        `graph.json` naming, an unextracted corpus - and recording the zero
        destroys the only baseline there is while reporting a successful run.
        """
        empty = self.root / "empty.json"
        empty.write_text(json.dumps({"unrelated": []}), encoding="utf-8")
        telemetry.record({"size_cuts.layer_nodes": 41}, out=stdio.StringIO(), err=stdio.StringIO())

        code, _, err = self._run(str(empty))

        self.assertEqual(code, 1)
        self.assertIn("nothing was recorded", err)
        self.assertEqual(telemetry.read()["size_cuts.layer_nodes"], 41)

    def test_a_cuts_file_outside_the_store_is_refused_and_one_inside_is_read(self):
        """Breaks if `--cuts` can open any file on the machine, and if it opens none.

        Both halves in one test on purpose: a confinement that refused everything
        would pass the refusal half and be useless, which is how a guard goes
        vacuous. The boundary is enforced here rather than in `read_cuts` because
        this is the only place the store's root is known - the same argument
        `io.checked_write_target` makes for the write side.
        """
        self._repo_graph("orchard-api", [_node("a", "main.tf")], [])
        elsewhere = Path(self._tmp.name).parent / "outside-the-store.txt"
        elsewhere.write_text("cut iac\nfile *.tf\n", encoding="utf-8")
        self.addCleanup(elsewhere.unlink)
        inside = self.root / "config" / "alternative-cuts.txt"
        inside.parent.mkdir(parents=True, exist_ok=True)
        inside.write_text("cut iac\nfile *.tf\n", encoding="utf-8")

        refused, _, err = self._run("--cuts", str(elsewhere), "--no-record")
        accepted, out, _ = self._run("--cuts", str(inside), "--no-record")

        self.assertEqual(refused, 1)
        self.assertIn("outside the store", err)
        self.assertEqual(accepted, 0)
        self.assertIn("iac", out, "the cuts file inside the store was not read")

    def test_a_named_file_that_does_not_exist_refuses(self):
        """Breaks if a mistyped argument is measured as a graph with no nodes.

        `iter_array` yields nothing for a path that is not there, so the report
        would be a plausible-looking zero for a file nobody ever read.
        """
        code, _, err = self._run(str(self.root / "typo.json"))

        self.assertEqual(code, 1)
        self.assertIn("typo.json", err)

    def test_no_graph_at_all_refuses_rather_than_reporting_nothing(self):
        """Breaks if an unextracted corpus reports a clean, empty measurement.

        Zero graph files is a state every store passes through before its first
        extraction, and the failure mode is reading the empty report as "this
        estate has no AST layer".
        """
        code, _, err = self._run()

        self.assertEqual(code, 1)
        self.assertIn("No graph file", err)

    def test_a_compressed_per_repository_graph_is_read(self):
        """Breaks if only the uncompressed form is found.

        Stores commit the archive and gitignore the plain file, so on a fresh
        checkout the `.gz` is all there is - and the stage that could not read one
        shipped in this library before (`record-clustering --graph graph.json.gz`).
        """
        _write_graph(
            self.root / "repositories" / "orchard-api" / "graphify-out" / "graph.json.gz",
            [_node("a", "main.tf"), _node("b", "prod.tfvars")],
            [("a", "b")],
        )
        self._declare("cut iac\nfile *.tf\nfile *.tfvars\n")

        code, out, _ = self._run()

        self.assertEqual(code, 0)
        self.assertIn("graph.json.gz", out)
        self.assertEqual(telemetry.read()["size_cuts.iac.edges"], 1)

    def test_the_uncompressed_graph_wins_and_the_pair_is_reported(self):
        """Breaks if a stale counterpart is read without saying which file was read.

        Two graph files that disagree cost an operator a hand-made diff once
        already, on a store where every count in the report reconciled. The stage
        does not adjudicate - the operator knows which file is real - but it must
        say which one it read.
        """
        directory = self.root / "repositories" / "orchard-api" / "graphify-out"
        _write_graph(directory / "graph.json", [_node("a", "main.tf")], [])
        _write_graph(directory / "graph.json.gz", [_node("a", "main.tf"), _node("b", "b.tf")], [])

        code, _, err = self._run()

        self.assertEqual(code, 0)
        self.assertIn("both forms", err)
        self.assertEqual(telemetry.read()["size_cuts.layer_nodes"], 1)

    def test_an_unreadable_cuts_file_is_named_before_any_graph_is_read(self):
        """Breaks if a bad cuts file surfaces as a traceback after a long scan.

        Sizing reads every per-repository graph, so a refusal that arrives after
        that work has been done is a refusal an operator waits for.
        """
        self._repo_graph("orchard-api", [_node("a", "main.tf")], [])
        self._declare("cut iac\nfile **/*.tf\n")

        code, _, err = self._run()

        self.assertEqual(code, 1)
        self.assertIn("content-cuts.txt", err)
        self.assertIn("**", err)

    def test_the_stage_is_reachable_through_the_cli(self):
        """Breaks if the module is written and never wired into the stage table.

        A mechanism nothing calls is this repository's most repeated escape: three
        mutation-gate entries are behaviour that was tested through its own
        function while nothing drove it.
        """
        self._repo_graph("orchard-api", [_node("a", "main.tf")], [])
        out = stdio.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["size-cuts", "--no-record"])

        self.assertEqual(code, 0)
        self.assertIn("Layer as extracted", out.getvalue())
        self.assertIn("size-cuts", cli.usage())


if __name__ == "__main__":
    unittest.main()
