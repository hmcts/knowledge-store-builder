"""`summaries snapshot` and `remap` must say which graph file they read.

`config.GRAPH_PATH` is the *uncompressed* `graphify-out/graph.json`. Stores commit
the compressed `.gz` and gitignore the plain file, so on a fresh clone the plain
path is either absent or whatever a discarded verification run left behind. A
stage that reads it without saying so reports confidently on the wrong artefact.

`record-clustering` already carries this guard, and its docstring records the
estate that earned it: a discarded run left `graph.json` in the tree and the
record described 42,572 communities over 785,610 nodes while the committed `.gz`
held 42,627 over 785,493. The same class then reached `summaries snapshot`, where
it is worse — a snapshot is the remap's baseline, so mis-keying it mis-keys the
entire carry of committed prose.

The break each test catches is named on the test. Note what the existing
`_remap_refusal` guard cannot do here: it refuses when the snapshot and the graph
share *no* node ids, and a snapshot taken from the stale file shares every id with
that same stale file. Consistent and wrong is exactly the case it cannot see,
which is why these tests assert on the reported source rather than on a refusal.
"""

from __future__ import annotations

import contextlib
import gzip
import io
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
    nodes = [
        {"id": node, "label": node, "community": int(cid)}
        for cid, ids in members.items()
        for node in ids
    ]
    return {"directed": False, "multigraph": False, "graph": {}, "nodes": nodes, "links": []}


class GraphPair:
    """Setup shared by both classes: a store holding two disagreeing graph files.

    A mixin rather than a base test class. Inheriting the tests as well as the
    helpers ran the no-other-graph case under a setUp that writes both files,
    which failed for the right reason and the wrong cause.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        (self.root / "graphify-out").mkdir(parents=True)
        self._old_root = config.ROOT
        config.configure(root=str(self.root))

    def tearDown(self):
        config.configure(root=str(self._old_root))
        self._tmp.cleanup()
        super().tearDown()

    # --- helpers -------------------------------------------------------------

    def write_plain(self, members):
        config.GRAPH_PATH.write_text(json.dumps(_graph(members)), encoding="utf-8")

    def write_gz(self, members):
        path = config.GRAPH_PATH.with_name(config.GRAPH_PATH.name + ".gz")
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(_graph(members), handle)
        return path

    def run_snapshot(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = summaries.snapshot()
        return code, out.getvalue()


class GraphAmbiguityTest(GraphPair, SettingsIsolated):
    """`summaries snapshot` and `remap` must say which graph file they read."""

    def test_snapshot_names_the_other_graph_file_when_both_exist(self):
        """Breaks if `snapshot` reads `graph.json` without disclosing that a
        committed `.gz` sits beside it holding a different graph. That silence is
        how a stale snapshot became a mis-keyed remap baseline on a real estate."""
        self.write_plain({"0": ["a"], "1": ["b"]})
        self.write_gz({"0": ["a"], "1": ["b"], "2": ["c"]})

        code, output = self.run_snapshot()

        self.assertEqual(code, 0, "an ambiguous graph must not be a hard refusal")
        self.assertIn("graph.json.gz", output, "the other graph file must be named")

    def test_snapshot_reports_the_disagreeing_counts(self):
        """Breaks if the note is a generic caution rather than a measurement. A
        warning that cannot say the two files disagree gets read as boilerplate;
        the counts are what make an operator look.

        Asserted as `<count> communities` adjacent to each filename, not as bare
        digits. The first version of this test looked for `"2"` and `"3"` anywhere
        in the output and passed against the unfixed code — `Snapshotted 2
        communities` supplies the 2 and the temp path supplied the 3. A test that
        cannot fail is decoration, and the incidental-substring match is how it
        happens."""
        self.write_plain({"0": ["a"], "1": ["b"]})
        self.write_gz({"0": ["a"], "1": ["b"], "2": ["c"]})

        _code, output = self.run_snapshot()

        note = "\n".join(line for line in output.splitlines() if "graph.json.gz" in line)
        self.assertTrue(note, "no line names the other graph file")
        self.assertRegex(note, r"graph\.json\b(?![.\w])[^\n]*?\bhas 2 communities over 2\b")
        self.assertRegex(note, r"graph\.json\.gz\b[^\n]*?\bhas 3 over 3\b")

    def test_counts_test_can_fail_on_a_wrong_count(self):
        """The sensitivity check for the test above, in the same run. If the note
        reported the same count for both files the assertion must not still pass —
        otherwise it is measuring the presence of a number rather than agreement
        between two of them."""
        self.write_plain({"0": ["a"], "1": ["b"]})
        self.write_gz({"0": ["a"], "1": ["b"], "2": ["c"]})

        _code, output = self.run_snapshot()
        note = "\n".join(line for line in output.splitlines() if "graph.json.gz" in line)

        forged = note.replace("has 3 over 3", "has 2 over 2")
        with self.assertRaises(AssertionError):
            self.assertRegex(forged, r"graph\.json\.gz\b[^\n]*?\bhas 3 over 3\b")

    def test_snapshot_is_quiet_when_there_is_no_other_graph(self):
        """Breaks if the note fires unconditionally. A store with only the plain
        file has no ambiguity to report, and a warning on every run is how the
        real one gets ignored."""
        self.write_plain({"0": ["a"], "1": ["b"]})

        _code, output = self.run_snapshot()

        self.assertNotIn("graph.json.gz", output)

    def test_snapshot_still_writes_the_snapshot_it_read(self):
        """Breaks if disclosing the ambiguity also changes which file is read.
        This adds reporting, not adjudication — silently switching to the `.gz`
        would be a behaviour change consumers never asked for."""
        self.write_plain({"0": ["a"], "1": ["b"]})
        self.write_gz({"0": ["a"], "1": ["b"], "2": ["c"]})

        code, _output = self.run_snapshot()

        self.assertEqual(code, 0)
        with gzip.open(config.SUMMARIES_SNAPSHOT_PATH, "rt", encoding="utf-8") as handle:
            members = json.load(handle)
        self.assertEqual(
            sorted(members), ["0", "1"], "the snapshot must still come from graph.json"
        )

    def test_remap_names_the_other_graph_file_too(self):
        """Breaks if only `snapshot` got the warning. `remap` reads the same
        uncompressed path and is where the damage lands — it rewrites the committed
        summaries file — so a note on the snapshot alone leaves the expensive half
        silent. This is the call site the mutation gate has an entry for; without a
        test driving it, removing the line would keep the suite green."""
        members = {str(i): [f"n{i}"] for i in range(12)}
        self.write_plain(members)
        self.write_gz({**members, "99": ["n99"]})
        with gzip.open(config.SUMMARIES_SNAPSHOT_PATH, "wt", encoding="utf-8") as handle:
            json.dump(members, handle)
        config.SUMMARIES_PATH.write_text(
            json.dumps({str(i): f"summary {i}" for i in range(12)}), encoding="utf-8"
        )

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = summaries.remap()
        output = out.getvalue()

        self.assertEqual(code, 0, output)
        self.assertIn("graph.json.gz", output, "remap must name the other graph file")
        self.assertIn("MISMATCH", output)


class ArtefactWritersNameTheGraphTest(GraphPair, SettingsIsolated):
    """Every stage that reads the graph and writes a committed artefact.

    Scoped here after a store operator showed the distinction I had used to leave
    them out was false. I had reasoned that these "read to report", so a wrong read
    is a wrong report rather than a corrupted artefact. On their estate all of them
    write *tracked* files - the explorer page, the semantic layer, the deep dives -
    and the page is the worst case rather than the marginal one, because it is the
    artefact consumed by people who have no graph and no CLI to check it against.
    They had a real instance: a refresh where the page embedded a three-day-stale
    layer beside a brand-new graph and thirteen gates passed.

    One test per call site. The behaviour is shared, so what these actually assert
    is the wiring - and unwired call sites are the most repeated escape in this
    repository's mutation gate.
    """

    def setUp(self):
        super().setUp()
        self.write_plain({"0": ["a"], "1": ["b"]})
        self.write_gz({"0": ["a"], "1": ["b"], "2": ["c"]})

    def _stderr_of(self, call):
        out = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(out):
            with contextlib.suppress(Exception):
                call()
        return out.getvalue()

    def test_explorer_names_the_other_graph(self):
        """Breaks if the page can be built from a stale graph in silence. This is
        the call site the operator asked for first: the page ships to readers with
        no way to notice it disagrees with the graph it claims to describe."""
        from knowledgestore import build_explorer

        self.assertIn("MISMATCH", self._stderr_of(build_explorer.load_inputs))

    def test_deep_dives_names_the_other_graph(self):
        """Breaks if `dives.json` - a committed artefact - can be built from a
        stale graph in silence."""
        from knowledgestore import build_deep_dives

        self.assertIn("MISMATCH", self._stderr_of(lambda: build_deep_dives.extract("a")))

    def test_semantic_index_names_the_other_graph(self):
        """Breaks if the semantic layer's vocabulary can be collected from a stale
        graph in silence - the layer is committed and the page embeds it."""
        from knowledgestore import build_semantic_index

        self.assertIn("MISMATCH", self._stderr_of(build_semantic_index.collect_vocabulary))

    def test_the_estate_check_names_the_other_graph(self):
        """Breaks if the truthfulness gate can run against the wrong graph in
        silence. The strongest case of the class: a check reading the wrong
        artefact passes on the wrong data, and its silence then licenses a claim
        about something it never looked at."""
        self.assertIn("MISMATCH", self._stderr_of(summaries.estate_identifiers))


if __name__ == "__main__":
    unittest.main()
