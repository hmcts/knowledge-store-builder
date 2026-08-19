"""Which partitioner clustered the graph must be recorded, and reported honestly.

graphify clusters with graspologic's Leiden where that library imports and with
networkx's Louvain where it does not. Community ids key every authored summary,
so the same corpus clustered in two environments produces two partitions, and
`summaries remap` then reports a retention collapse whose cause is the machine
rather than the corpus. Nothing recorded the choice.

The failures these defend against, in order of how badly each one lies:

- an absent record printed as agreement - the shape of defect this library
  shipped twice in a week, where an unmeasured thing reads as a clean result;
- a detection that infers availability from something other than the import
  graphify actually decides by, and so can disagree with what ran;
- an environment where graspologic is present but broken reported as Louvain,
  which graphify would not fall back to there;
- a record written over an unclustered graph, naming a partitioner for a
  clustering that does not exist;
- `status` growing a graph load to enrich this check, breaking the one property
  its module docstring promises.
"""

from __future__ import annotations

import gzip
import io as io_module

import contextlib
import io as _io
import json
import sys
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import record_clustering  # noqa: E402
from knowledgestore import status  # noqa: E402


CLUSTERED = {
    "nodes": [
        {"id": "a", "community": 0},
        {"id": "b", "community": 0},
        {"id": "c", "community": 7},
        {"id": "d"},
    ],
    "links": [],
}


class Detection(unittest.TestCase):
    def test_an_absent_graspologic_reports_louvain(self):
        """graphify falls back on ImportError, so this must too - a probe that
        reported Leiden regardless would record a partitioner that never ran."""

        def absent() -> None:
            raise ImportError("No module named 'graspologic'")

        name, how = record_clustering.available_partitioner(probe=absent)
        self.assertEqual(name, record_clustering.LOUVAIN)
        self.assertIn("graspologic", how)

    def test_an_importable_graspologic_reports_leiden(self):
        name, _ = record_clustering.available_partitioner(probe=lambda: None)
        self.assertEqual(name, record_clustering.LEIDEN)

    def test_a_broken_graspologic_is_undeterminable_not_louvain(self):
        """A numpy ABI mismatch raises ValueError, which graphify does NOT catch:
        it would fail rather than fall back. Calling that Louvain would be a guess
        printed as a measurement, and it would read as reproducible."""

        def broken() -> None:
            raise ValueError("numpy.dtype size changed")

        name, how = record_clustering.available_partitioner(probe=broken)
        self.assertIsNone(name)
        self.assertIn("ValueError", how)

    def test_the_real_probe_matches_whether_graspologic_is_installed(self):
        """The default probe, in this environment, against an independent measure
        of the same fact. Catches a probe wired to one answer, or one testing a
        module name graphify does not use - the availability would then be
        invented rather than detected, whichever way round it happened to be."""
        name, _ = record_clustering.available_partitioner()
        expected = (
            record_clustering.LEIDEN
            if find_spec("graspologic") is not None
            else record_clustering.LOUVAIN
        )
        self.assertEqual(name, expected)


class Recording(SettingsIsolated):
    def _store(self, graph: dict) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "graphify-out").mkdir(parents=True)
        config.configure(root=str(root))
        config.GRAPH_PATH.write_text(json.dumps(graph), encoding="utf-8")
        return root

    def _record(self) -> dict:
        return json.loads(config.CLUSTERING_RECORD_PATH.read_text(encoding="utf-8"))

    def test_the_record_names_the_partitioner_and_what_it_describes(self):
        """Counts by hand from CLUSTERED: two communities (0 and 7) over three of
        its four nodes. A record that named a partitioner without saying which
        clustering it describes leaves a reader nothing to check it against."""
        self._store(CLUSTERED)
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(record_clustering.main([]), 0)
        written = self._record()
        self.assertIn(written["partitioner"], record_clustering.PARTITIONER_NAMES)
        self.assertEqual(written["communities"], 2)
        self.assertEqual(written["clustered_nodes"], 3)
        self.assertIn("2 communities over 3 nodes", out.getvalue())

    def test_the_recorded_partitioner_is_the_one_this_environment_offers(self):
        self._store(CLUSTERED)
        with contextlib.redirect_stdout(_io.StringIO()):
            record_clustering.main([])
        detected, _ = record_clustering.available_partitioner()
        self.assertEqual(self._record()["partitioner"], detected)

    def test_an_unclustered_graph_is_refused_and_writes_nothing(self):
        """`graphify cluster-only` reports success without persisting a
        clustering. Recording a partitioner over that names the algorithm behind
        communities that do not exist, and the next reader believes it."""
        self._store({"nodes": [{"id": "a"}, {"id": "b"}], "links": []})
        err = _io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(record_clustering.main([]), 1)
        self.assertFalse(config.CLUSTERING_RECORD_PATH.exists())
        self.assertIn("Cluster the graph before recording", err.getvalue())

    def test_an_unrecognised_recorded_value_reads_as_unknown(self):
        """A hand-edited or future-format record must not be half-believed: a
        partitioner this library cannot name is unknown, and unknown must never
        reach the agreement branch."""
        self._store(CLUSTERED)
        config.CLUSTERING_RECORD_PATH.write_text('{"partitioner": "spectral"}', encoding="utf-8")
        self.assertIsNone(record_clustering.recorded_partitioner())

    def test_no_record_at_all_reads_as_unknown(self):
        self._store(CLUSTERED)
        self.assertIsNone(record_clustering.recorded_partitioner())


class DescribesTheGraphThatShips(SettingsIsolated):
    """A record must not describe a graph nobody will have.

    Shipped in v0.11.6: the recorder read the uncompressed `graph.json`, which is
    gitignored in every store because the compressed `.gz` is the artefact. On a
    real estate a discarded verification run had left `graph.json` in the tree, so
    the stage recorded 42,572 communities over 785,610 nodes while the committed
    `.gz` held 42,627 over 785,493 - and exited 0 with real-looking counts.

    That is worse than not having the stage: it converts an honest "unknown, not
    agreed" into a false "agreed", and both files are called graph-something so
    nothing about the run looks wrong.
    """

    def _store(self, compressed: bool) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "graphify-out").mkdir(parents=True)
        config.configure(root=str(root))
        config.GRAPH_PATH.write_text(
            json.dumps({"nodes": [{"id": "a", "community": 1}, {"id": "b", "community": 2}]}),
            encoding="utf-8",
        )
        if compressed:
            # Two bytes of gzip header and nothing else - deliberately truncated.
            #
            # This used to say "existence alone is the signal, because comparing
            # 1.4 GB of graph is what this refuses to do". The counts ARE compared
            # now: measured at 4.7s and 3.8 GB peak for 785,493 nodes, and the thing
            # being refused was a node-by-node CONTENT comparison, which is a
            # different and far more expensive question than two counts.
            #
            # So the stub now earns its keep twice over: it proves an unreadable
            # counterpart cannot take the stage down. It raised EOFError - neither
            # OSError nor ValueError - and escaped the handler, over a file the
            # stage was not asked to describe.
            config.GRAPH_PATH.with_suffix(".json.gz").write_bytes(b"\x1f\x8b")
        return root

    def test_it_records_when_both_graph_files_exist_and_names_the_other(self):
        """An earlier fix REFUSED here, and that was wrong: the committed `.gz` is
        tracked, so it is present from checkout on every refresh after the first -
        both files exist at the exact moment this stage is meant to run, and the
        refusal made the stage unreachable on any store that commits a compressed
        graph, which is the documented practice."""
        self._store(compressed=True)
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()):
            code = record_clustering.main([])
        self.assertEqual(code, 0, "refusing here breaks the normal refresh")
        self.assertTrue(config.CLUSTERING_RECORD_PATH.is_file())
        self.assertIn("also exists", out.getvalue())
        self.assertIn("--graph", out.getvalue())

    def test_an_explicit_graph_overrides_the_default(self):
        """The operator knows which of their two files is real; this stage does not."""
        root = self._store(compressed=True)
        other = root / "graphify-out" / "elsewhere.json"
        other.write_text(json.dumps({"nodes": [{"id": "z", "community": 9}]}), encoding="utf-8")
        with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
            record_clustering.main(["--graph", str(other)])
        written = json.loads(config.CLUSTERING_RECORD_PATH.read_text(encoding="utf-8"))
        self.assertEqual(written["described"], "elsewhere.json")
        self.assertEqual(written["communities"], 1)

    def test_the_record_captures_whether_hashes_were_randomised(self):
        """Without it, a store that clustered unseeded is indistinguishable from one
        that did not - and the community count cannot substitute, because a
        re-cluster can return an identical count with different membership."""
        self._store(compressed=False)
        with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
            record_clustering.main([])
        written = json.loads(config.CLUSTERING_RECORD_PATH.read_text(encoding="utf-8"))
        self.assertIn("hash_randomised", written)
        self.assertEqual(written["hash_randomised"], bool(sys.flags.hash_randomization))

    def test_an_unpinned_run_says_so_at_record_time(self):
        """Recording the field is not enough: the operator is standing there when
        this runs, and it is the cheapest moment to tell them the communities they
        just built are not reproducible."""
        if not sys.flags.hash_randomization:
            self.skipTest("this interpreter pins hashes; the unpinned path cannot be observed")
        self._store(compressed=False)
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()):
            record_clustering.main([])
        self.assertIn("randomisation was ON", out.getvalue())
        self.assertIn("PYTHONHASHSEED=0", out.getvalue())

    def test_the_seed_is_read_from_the_interpreter_not_the_environment(self):
        """`PYTHONHASHSEED=random` is legal, reads like an instruction, and leaves
        randomisation on. The environment variable would record it as set."""
        self.assertEqual(record_clustering.hash_randomisation(), bool(sys.flags.hash_randomization))

    def test_it_records_when_only_the_uncompressed_graph_exists(self):
        """The intended build-time case: recorded beside the clustering that just
        wrote it, before anything is compressed."""
        self._store(compressed=False)
        with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
            code = record_clustering.main([])
        self.assertEqual(code, 0)
        self.assertTrue(config.CLUSTERING_RECORD_PATH.is_file())

    def test_the_record_says_which_file_it_described(self):
        """So a reader never has to guess which of a store's two graph files a
        record refers to."""
        self._store(compressed=False)
        with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
            record_clustering.main([])
        written = json.loads(config.CLUSTERING_RECORD_PATH.read_text(encoding="utf-8"))
        self.assertEqual(written["described"], config.GRAPH_PATH.name)


class Verdict(unittest.TestCase):
    def test_an_unrecorded_partitioner_is_never_reported_as_agreement(self):
        """The failure this whole family is about: nothing measured, printed as a
        clean result. It must say unknown, and must not claim a match."""
        said = status.partitioner_verdict(None, record_clustering.LOUVAIN, "probed")
        self.assertIn("not recorded", said)
        self.assertIn("unknown, not agreed", said)
        self.assertNotIn("the same partitioner", said)

    def test_a_mismatch_says_a_re_cluster_here_will_not_reproduce_it(self):
        """The one-line diagnosis the issue asks for. Without the negation this
        reads as two facts side by side and nobody draws the conclusion."""
        said = status.partitioner_verdict(
            record_clustering.LEIDEN, record_clustering.LOUVAIN, "probed"
        )
        self.assertIn("records Leiden (graspologic)", said)
        self.assertIn("this environment has Louvain (networkx)", said)
        self.assertIn("will NOT reproduce", said)
        self.assertIn("summaries remap", said)

    def test_a_mismatch_the_other_way_round_is_reported_too(self):
        """Leiden arriving in an environment whose store was built with Louvain
        moves the ids just as far; a check that only looked for missing
        graspologic would call that agreement."""
        said = status.partitioner_verdict(
            record_clustering.LOUVAIN, record_clustering.LEIDEN, "probed"
        )
        self.assertIn("will NOT reproduce", said)

    def test_a_match_says_so_without_promising_reproducibility(self):
        """The partitioner matching is necessary and not sufficient - the hash
        seed still has to be pinned - so this must not read as a guarantee."""
        said = status.partitioner_verdict(
            record_clustering.LOUVAIN, record_clustering.LOUVAIN, "probed"
        )
        self.assertIn("the same partitioner", said)
        self.assertIn("PYTHONHASHSEED=0", said)

    def test_an_undeterminable_environment_concludes_nothing(self):
        said = status.partitioner_verdict(record_clustering.LEIDEN, None, "raised ValueError")
        self.assertIn("undeterminable", said)
        self.assertIn("raised ValueError", said)
        self.assertNotIn("the same partitioner", said)

    def test_every_verdict_names_the_file_it_read(self):
        """Each silent failure here came from a check that could not say what it
        consulted, so the artefact is named in all four states."""
        for recorded in (None, record_clustering.LEIDEN, record_clustering.LOUVAIN):
            for here in (None, record_clustering.LEIDEN, record_clustering.LOUVAIN):
                with self.subTest(recorded=recorded, here=here):
                    said = status.partitioner_verdict(recorded, here, "probed")
                    self.assertIn("graphify-out/clustering-inputs.json", said)


class Reported(SettingsIsolated):
    def _store(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "graphify-out").mkdir(parents=True)
        config.configure(root=str(root))
        return root

    def _reported(self) -> str:
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            status._report_clustering()
        return out.getvalue()

    def _other_partitioner(self) -> str:
        here, _ = record_clustering.available_partitioner()
        return (
            record_clustering.LEIDEN
            if here == record_clustering.LOUVAIN
            else record_clustering.LOUVAIN
        )

    def test_a_store_with_no_record_hears_about_it(self):
        self._store()
        self.assertIn("not recorded", self._reported())

    def test_a_record_from_the_other_environment_is_reported_as_unreproducible(self):
        """Derived from what this machine actually offers, so it holds on a Leiden
        machine and a Louvain one rather than pinning today's environment."""
        self._store()
        config.CLUSTERING_RECORD_PATH.write_text(
            json.dumps({"partitioner": self._other_partitioner()}), encoding="utf-8"
        )
        self.assertIn("will NOT reproduce", self._reported())

    def test_a_record_from_this_environment_reports_the_match(self):
        self._store()
        here, _ = record_clustering.available_partitioner()
        config.CLUSTERING_RECORD_PATH.write_text(
            json.dumps({"partitioner": here}), encoding="utf-8"
        )
        self.assertIn("the same partitioner", self._reported())

    def test_the_check_does_not_read_the_graph(self):
        """`status` must stay cheap enough to run any time, which its module
        docstring promises and a graph load would break. An unparseable graph
        beside the record is the cheap proof: enriching this check with a
        community count from the graph would raise here instead of printing.
        """
        self._store()
        config.GRAPH_PATH.write_text("this is not JSON", encoding="utf-8")
        (config.GRAPH_PATH.parent / "graph.json.gz").write_bytes(b"not gzip either")
        self.assertIn("Clustering partitioner:", self._reported())

    def test_status_main_drives_the_check(self):
        """Behaviour tested through a function while nothing drove `main()` is how
        two checks in this module shipped unwired."""
        self._store()
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            status.main([])
        self.assertIn("Clustering partitioner:", out.getvalue())


class Stage(unittest.TestCase):
    def test_the_stage_is_registered_between_deployments_and_summaries(self):
        """Run order is the documentation: the record is written by the clustering
        step, which sits after `deployments` and before any summary work."""
        from knowledgestore import cli

        names = list(cli.STAGES)
        self.assertIn("record-clustering", names)
        self.assertLess(names.index("deployments"), names.index("record-clustering"))
        self.assertLess(names.index("record-clustering"), names.index("summaries"))


if __name__ == "__main__":
    unittest.main()


class TheCommittedGraphCanBeDescribed(SettingsIsolated):
    """The escape hatch the stage's own warning names must actually work.

    Reported by a store operator against v0.12.0: the default run described a stale
    uncompressed `graph.json` left by a discarded verification run, the warning said
    "pass --graph to be explicit", and doing so with the only artefact that store
    ships died on the gzip magic byte:

        UnicodeDecodeError: 'utf-8' codec can't decode byte 0x8b in position 1

    So the fix is in `io.read_json`, not here: three other call sites read
    `GRAPH_PATH` with the same reader.
    """

    def setUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.tmp)
        (self.root / "graphify-out").mkdir()
        config.configure(root=str(self.root))

    def _write(self, name: str, communities: int, nodes: int) -> Path:
        graph = {
            "nodes": [
                {"id": f"n{i}", "community": i % communities, "label": f"L{i}"}
                for i in range(nodes)
            ]
        }
        path = self.root / "graphify-out" / name
        if name.endswith(".gz"):
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                json.dump(graph, handle)
        else:
            path.write_text(json.dumps(graph))
        return path

    def test_a_gzipped_graph_can_be_read(self):
        committed = self._write("graph.json.gz", communities=7, nodes=140)
        out = io_module.StringIO()
        with contextlib.redirect_stdout(out):
            code = record_clustering.main(["--graph", str(committed)])
        self.assertEqual(code, 0, out.getvalue())
        record = json.loads((self.root / "graphify-out" / "clustering-inputs.json").read_text())
        self.assertEqual(record["communities"], 7)
        self.assertEqual(record["clustered_nodes"], 140)
        self.assertEqual(record["described"], "graph.json.gz")

    def test_the_counterpart_is_found_in_both_directions(self):
        uncompressed = self.root / "graphify-out" / "graph.json"
        self.assertEqual(
            record_clustering.counterpart(uncompressed).name,
            "graph.json.gz",
            "the .gz beside a .json",
        )
        self.assertEqual(
            record_clustering.counterpart(self.root / "graphify-out" / "graph.json.gz").name,
            "graph.json",
            "and back again - with_suffix produced graph.json.json.gz here, which never exists",
        )

    def test_a_stale_counterpart_is_named_with_both_counts(self):
        """The operator had to diff the two files by hand. That is the stage's job.

        Asserts on the numbers, not merely that something was said: a warning that
        fires without naming which file holds what is the same puzzle one step on.
        """
        self._write("graph.json", communities=5, nodes=120)
        self._write("graph.json.gz", communities=7, nodes=140)
        err = io_module.StringIO()
        with contextlib.redirect_stdout(io_module.StringIO()), contextlib.redirect_stderr(err):
            code = record_clustering.main([])
        self.assertEqual(code, 0)
        message = err.getvalue()
        self.assertIn("MISMATCH", message)
        self.assertIn("graph.json has 5 communities over 120", message)
        self.assertIn("graph.json.gz has 7 over 140", message)

    def test_agreeing_graphs_say_nothing(self):
        """Otherwise the finding fires on every normal refresh and stops being read."""
        self._write("graph.json", communities=7, nodes=140)
        self._write("graph.json.gz", communities=7, nodes=140)
        err = io_module.StringIO()
        with contextlib.redirect_stdout(io_module.StringIO()), contextlib.redirect_stderr(err):
            record_clustering.main([])
        self.assertNotIn("MISMATCH", err.getvalue())

    def test_an_explicit_graph_skips_the_comparison(self):
        """--graph answers "which file did you mean", so re-reading the other is cost
        for nothing - and on a real estate the other file is tens of megabytes."""
        self._write("graph.json", communities=5, nodes=120)
        committed = self._write("graph.json.gz", communities=7, nodes=140)
        err = io_module.StringIO()
        with contextlib.redirect_stdout(io_module.StringIO()), contextlib.redirect_stderr(err):
            record_clustering.main(["--graph", str(committed)])
        self.assertNotIn("MISMATCH", err.getvalue())
