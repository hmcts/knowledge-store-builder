"""Both opt-in `status` reports must run on the graph a store actually ships (#262).

Every store gitignores `graphify-out/graph.json` and commits `graph.json.gz`, so a
checkout — and a store cleaned up after a refresh — holds only the archive. Both
readers already accepted it: `graph_stream.iter_array` picks its opener by suffix,
and `near_duplicates` handed the `.gz` directly returns a full ranking. Only the
guard refused, so `--duplicates` and `--central` were unavailable in the state a
store spends most of its life in, and the way out was a multi-gigabyte `gunzip` for
a report documented as cheap and opt-in.

The fallback is one helper rather than a third copy because three call sites now
have to agree about which of a store's two graphs a stage read. Each of them then
has to *say* which: the two files can disagree — on one estate a leftover plain
graph described 42,572 communities where the committed archive held 42,627 — so the
same count means different things depending on the answer, and a report that does
not name its file leaves the reader to guess.

These tests assert the printed report and the returned path. Nothing here asserts
that a fallback function was called, because a report naming a file it did not read
would pass that and fail an operator.
"""

from __future__ import annotations

import contextlib
import gzip
import io as io_module
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import graph_files  # noqa: E402
from knowledgestore import status  # noqa: E402


def estate(repo: str, twin: str, label: str) -> dict:
    """Two repositories sharing every (label, path) pair, as merge-graphs leaves them.

    Ids namespaced per repository, so nothing collides by construction — which is
    why no count downstream can see the copy. The links make the same fixture serve
    `--central`: `<repo>::0` carries two endpoints and every other node one, so the
    ranking is `<repo>::0` first at degree 2.
    """
    nodes = []
    for name in (repo, twin):
        for index, (node_label, source) in enumerate(
            ((label, f"src/{label.lower()}.java"), (f"{label}Helper", "src/helper.java"))
        ):
            nodes.append(
                {
                    "id": f"{name}::{index}",
                    "label": node_label,
                    "source_file": source,
                    "repo": name,
                }
            )
    return {
        "nodes": nodes,
        "links": [
            {"source": f"{repo}::0", "target": f"{repo}::1"},
            {"source": f"{repo}::0", "target": f"{twin}::0"},
        ],
    }


def unshared_estate() -> dict:
    """Three repositories sharing no (label, path) pair with each other."""
    nodes = []
    for name in ("alpha", "beta", "gamma"):
        for index in range(3):
            nodes.append(
                {
                    "id": f"{name}::{index}",
                    "label": f"{name}-symbol-{index}",
                    "source_file": f"src/{name}/{index}.txt",
                    "repo": name,
                }
            )
    return {"nodes": nodes, "links": [{"source": "alpha::0", "target": "alpha::1"}]}


class GraphToReadTest(unittest.TestCase):
    """The helper's returned path, which is the thing the three call sites share."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plain = Path(tmp.name) / "graph.json"
        self.archive = Path(tmp.name) / "graph.json.gz"

    def _write_plain(self):
        self.plain.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")

    def _write_archive(self):
        with gzip.open(self.archive, "wt", encoding="utf-8") as handle:
            json.dump({"nodes": [], "links": []}, handle)

    def test_the_plain_graph_is_preferred_when_both_exist(self):
        """Breaks if the fallback becomes the first choice.

        A mid-pipeline stage has just written the plain file and the archive beside
        it is the previous build, so reading the archive there would report on the
        graph the run has already superseded.
        """
        self._write_plain()
        self._write_archive()

        self.assertEqual(graph_files.graph_to_read(self.plain), self.plain)

    def test_the_committed_archive_is_read_when_the_plain_graph_is_absent(self):
        """Breaks if a stage is unrunnable on a checkout.

        `graph.json` is gitignored, so after a clone or a cleanup only the `.gz`
        exists. Streaming it costs the same as streaming the plain file.
        """
        self._write_archive()

        self.assertEqual(graph_files.graph_to_read(self.plain), self.archive)

    def test_neither_file_present_yields_no_path(self):
        """Breaks if the helper hands back a path that does not exist.

        The caller's refusal is driven by `None`; a returned non-file would be
        opened and the stage would die on an OSError instead of reporting.
        """
        self.assertIsNone(graph_files.graph_to_read(self.plain))

    def test_the_archive_is_offered_from_a_path_that_already_names_it(self):
        """Breaks if the fallback only works in one direction.

        `counterpart` handles both, because a `--graph` flag may name either file,
        and a helper that assumed `.json` would return the plain file's path on a
        store that only ships the archive — a path that does not exist.
        """
        self._write_archive()

        self.assertEqual(graph_files.graph_to_read(self.archive), self.archive)


class StatusReportsFromTheCommittedArchive(SettingsIsolated):
    """`status --duplicates` and `--central` end to end: real files, real streamed reads."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "graphify-out").mkdir(parents=True)
        old_root = config.ROOT
        self.addCleanup(config.configure, root=str(old_root))
        config.configure(root=str(self.root))

    def write_plain(self, payload: dict) -> None:
        config.GRAPH_PATH.write_text(json.dumps(payload), encoding="utf-8")

    def write_archive(self, payload: dict) -> None:
        with gzip.open(str(config.GRAPH_PATH) + ".gz", "wt", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def _run(self, argv):
        """Only what the two opt-in reports printed, driven through the real CLI.

        `main` runs both reports before it prints `Provenance:`, so the slice is
        exactly their output. Sliced rather than taken whole because the rest of
        `status` names other `.json.gz` artefacts of its own, and an assertion that
        a file name is absent would pass or fail on those instead.
        """
        out = io_module.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            # A temporary root is not a git checkout, so a later failure in `main`
            # is not this test's business - the reports run first.
            with contextlib.suppress(Exception):
                status.main(argv)
        printed = out.getvalue()
        self.assertIn("Provenance:", printed, "`status` stopped before it reached the reports")
        return printed.split("Provenance:")[0]

    def test_duplicates_ranks_from_the_archive_alone(self):
        """Breaks if the guard refuses a store holding only the file it ships.

        The #262 defect. `alpha` and `alpha-fork` hold the same two (label, path)
        pairs, so the overlap is 2 of 2 - 100.0% - and the report was instead
        `Repository overlap: no graph at .../graph.json`.
        """
        self.write_archive(estate("alpha", "alpha-fork", "Widget"))

        output = self._run(["--duplicates"])

        self.assertIn("alpha / alpha-fork", output)
        self.assertIn("100.0%", output)
        self.assertIn("2 shared pair(s)", output)

    def test_central_ranks_from_the_archive_alone(self):
        """Breaks if the guard refuses a store holding only the file it ships.

        The #262 defect on the other report. `alpha::0` carries two of the three
        edge endpoints, so it ranks first at degree 2.
        """
        self.write_archive(estate("alpha", "alpha-fork", "Widget"))

        output = self._run(["--central"])

        self.assertIn("Most connected", output)
        self.assertIn("Widget", output)
        self.assertIn("2  Widget", output)

    def test_duplicates_names_the_file_it_read(self):
        """Breaks if the report states counts without saying which graph they describe.

        A store's two graphs can disagree, so `2 shared pair(s)` from the archive
        and the same figure from a stale leftover are different claims. Naming the
        file is what lets a reader tell them apart, and its absence is what made an
        operator diff two graphs by hand.
        """
        self.write_archive(estate("alpha", "alpha-fork", "Widget"))

        output = self._run(["--duplicates"])

        self.assertIn("graph.json.gz", output)

    def test_central_names_the_file_it_read(self):
        """Breaks if the ranking states degrees without saying which graph they came from.

        Same reason as the overlap report: the two files can hold different graphs,
        so a ranking that does not name its source cannot be checked against
        anything.
        """
        self.write_archive(estate("alpha", "alpha-fork", "Widget"))

        output = self._run(["--central"])

        self.assertIn("graph.json.gz", output)

    def test_duplicates_reads_the_plain_graph_when_both_exist(self):
        """Breaks if the fallback becomes the preference.

        Mid-pipeline the plain file is the fresh merge and the archive is the
        previous build. Written with different repository names in each file, so
        which one was read is visible in the output rather than inferred.
        """
        self.write_plain(estate("alpha", "alpha-fork", "Widget"))
        self.write_archive(estate("bravo", "bravo-copy", "Sprocket"))

        output = self._run(["--duplicates"])

        self.assertIn("alpha / alpha-fork", output)
        self.assertNotIn("bravo", output)
        self.assertIn("graph.json", output)
        self.assertNotIn("graph.json.gz", output)

    def test_central_reads_the_plain_graph_when_both_exist(self):
        """Breaks if the fallback becomes the preference.

        Same reason as the overlap report. The two files carry different labels, so
        the ranking names which graph produced it.
        """
        self.write_plain(estate("alpha", "alpha-fork", "Widget"))
        self.write_archive(estate("bravo", "bravo-copy", "Sprocket"))

        output = self._run(["--central"])

        self.assertIn("Widget", output)
        self.assertNotIn("Sprocket", output)
        self.assertIn("graph.json", output)
        self.assertNotIn("graph.json.gz", output)

    def test_duplicates_refuses_when_neither_file_exists(self):
        """Breaks if a store with no graph at all prints nothing under the flag.

        Silence is this report's answer for "no near-copies", so it must not also
        be its answer for "no graph to look at".
        """
        output = self._run(["--duplicates"])

        self.assertIn("Repository overlap: no graph", output)

    def test_central_refuses_when_neither_file_exists(self):
        """Breaks if a store with no graph at all prints nothing under the flag."""
        output = self._run(["--central"])

        self.assertIn("Most connected: no graph", output)

    def test_neither_refusal_points_at_only_one_of_the_two_files(self):
        """Breaks if the refusal names `graph.json` alone.

        `graph.json` is gitignored and may have been deliberately cleaned up, so a
        message naming only it reads as "restore the file you just removed" when
        the actual state is that the committed archive is missing too. Both files
        have to be on the line for the operator to know which one to produce.
        """
        for flag in ("--duplicates", "--central"):
            with self.subTest(flag=flag):
                output = self._run([flag])

                self.assertIn(str(config.GRAPH_PATH), output)
                self.assertIn(".gz", output)

    def test_an_estate_sharing_no_pair_still_prints_nothing(self):
        """Breaks if naming the file read turns the report into one that always speaks.

        The sensitivity control. Silence is the answer for an estate with no
        near-copies, so the file name has to sit inside the header the report only
        prints when it has something to state - not on a line of its own.
        """
        self.write_archive(unshared_estate())

        output = self._run(["--duplicates"])

        self.assertNotIn("Repository (label, path) overlap", output)
        self.assertNotIn("Repository overlap", output)


if __name__ == "__main__":
    unittest.main()
