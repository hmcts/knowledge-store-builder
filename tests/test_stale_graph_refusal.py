"""Stages that write the graph back must refuse a stale uncompressed one (#198).

`build_deployments`, `build_package_edges` and `extract_gherkin` read
`graphify-out/graph.json`, add a layer, and write **both** `graph.json` and
`graph.json.gz`. Every store gitignores the plain file and commits the archive,
so a leftover `graph.json` from an earlier run is ordinary rather than exotic —
and it is the precondition for the committed graph being overwritten from it,
losing whatever the newer clustering produced.

This is the only failure in the two-graph-file class that destroys an artefact
instead of describing one wrongly, which is why these stages refuse where the six
in #197 report. A `MISMATCH` line cannot help: by the time it reaches a log, the
write has happened.

Each test names the break it catches. The wiring tests assert the **archive is
byte-identical afterwards**, not merely that the exit code was 1 — a guard that
returns 1 after writing has prevented nothing, and the exit code cannot tell the
two apart.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import graph_files  # noqa: E402


def _graph(node_ids):
    return {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [{"id": n, "label": n, "community": 1} for n in node_ids],
        "links": [],
    }


class StaleRefusalTest(SettingsIsolated):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "graphify-out").mkdir(parents=True)
        self._old_root = config.ROOT
        config.configure(root=str(self.root))
        self.plain = config.GRAPH_PATH
        self.packed = config.GRAPH_PATH.with_name(config.GRAPH_PATH.name + ".gz")

    def tearDown(self):
        config.configure(root=str(self._old_root))
        self._tmp.cleanup()
        super().tearDown()

    # --- helpers -------------------------------------------------------------

    def write_pair(self, agree: bool):
        """Both files present; `agree` decides whether they hold the same graph.

        Content rather than timestamps. A timestamp comparison was tried first and
        the suite rejected it: these stages write the plain file and then the
        archive, so the plain file is always older after a successful run, and a
        guard on that refused every re-run.
        """
        self.plain.write_text(json.dumps(_graph(["a", "b"])), encoding="utf-8")
        packed = _graph(["a", "b"]) if agree else _graph(["a", "b", "c"])
        with gzip.open(self.packed, "wt", encoding="utf-8") as handle:
            json.dump(packed, handle)

    # --- the refusal itself --------------------------------------------------

    def test_it_refuses_when_the_two_files_disagree(self):
        """Breaks if the defect condition stops being detected. A leftover
        `graph.json` disagrees with an archive refreshed since it was written."""
        self.write_pair(agree=False)

        refusal = graph_files.stale_refusal(self.plain)

        self.assertIn("Refusing to run", refusal)
        self.assertIn("graph.json.gz", refusal)
        self.assertIn("gunzip -kf", refusal, "the refusal must name the way out")
        self.assertRegex(refusal, r"graph\.json\b[^\n]*?\bhas 1 communities over 2\b")
        self.assertRegex(refusal, r"graph\.json\.gz\b[^\n]*?\bhas 1 over 3\b")

    def test_it_allows_two_files_that_agree(self):
        """Breaks if the guard refuses on both files merely existing.

        The archive is tracked, so both are present on every refresh after the
        first, and after a successful run they hold the same graph. A both-exist
        guard fires on the normal case and makes the stage unreachable, which is
        how an earlier version of this class of warning was withdrawn.
        """
        self.write_pair(agree=True)
        self.assertEqual(graph_files.stale_refusal(self.plain), "")

    def test_a_rerun_is_allowed(self):
        """Breaks if the guard refuses the case that broke its first version.

        A re-run reads the plain file and rewrites both, leaving the archive newer
        by mtime and identical by content. The mtime version of this guard refused
        here, which is what a passing suite would otherwise have hidden.
        """
        self.write_pair(agree=True)
        stamp_old, stamp_new = 1_600_000_000, 1_700_000_000
        os.utime(self.plain, (stamp_old, stamp_old))
        os.utime(self.packed, (stamp_new, stamp_new))
        self.assertEqual(graph_files.stale_refusal(self.plain), "")

    def test_no_counterpart_is_allowed(self):
        """Breaks if a store holding only the uncompressed graph is blocked."""
        self.plain.write_text(json.dumps(_graph(["a"])), encoding="utf-8")
        self.assertEqual(graph_files.stale_refusal(self.plain), "")

    # --- wiring, one per stage ----------------------------------------------

    def _archive_bytes(self):
        return self.packed.read_bytes()

    def _assert_stage_refuses(self, main):
        before = self._archive_bytes()
        code = main()
        self.assertEqual(code, 1)
        self.assertEqual(
            self._archive_bytes(),
            before,
            "the committed archive was modified despite the refusal",
        )

    def test_deployments_refuses_and_writes_nothing(self):
        """Breaks if this stage can still overwrite the archive from a stale graph."""
        self.write_pair(agree=False)
        from knowledgestore import build_deployments

        self._assert_stage_refuses(build_deployments.main)

    def test_package_edges_refuses_and_writes_nothing(self):
        """Breaks if this stage is left unguarded when the others are fixed.

        The clones directory is created deliberately. Without it this stage returns
        1 from its *next* check, so the test passed with the guard removed and
        asserted nothing — the mutation gate caught that, not review. With an empty
        clones directory the stage proceeds all the way to rewriting both files, so
        the refusal is the only thing standing between it and the archive.
        """
        self.write_pair(agree=False)
        config.REPOSITORIES_DIR.mkdir(parents=True, exist_ok=True)
        self.assertTrue(
            config.REPOSITORIES_DIR.is_dir(),
            "precondition: the stage must get past its clones check, or this test is vacuous",
        )
        from knowledgestore import build_package_edges

        self._assert_stage_refuses(build_package_edges.main)

    def test_gherkin_refuses_and_writes_nothing(self):
        """Breaks if the third call site is missed. Three stages share one defect,
        and a two-of-three fix reads as done while the third still loses data."""
        self.write_pair(agree=False)
        from knowledgestore import extract_gherkin

        self._assert_stage_refuses(extract_gherkin.main)


if __name__ == "__main__":
    unittest.main()
