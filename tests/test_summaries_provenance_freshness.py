"""The carried-versus-authored split must describe the summaries it is printed beside.

`verify` splits its grounding flag rate by provenance, reading `remap-report.json`
to decide which summaries were carried across a re-cluster and which were authored
on their own digest. The docstring on that line says retention must never be read
without it - which is exactly why it must not be readable when it describes other
data.

The break these tests catch: the split computed from a report written for a
previous partition. Nothing fails and nothing comes back empty when that happens,
because community ids are small integers and are reused across re-clusters - every
id in the old report still resolves against the current summaries, so the wrong
partition is reported to two significant figures and reads as ordinary. On the run
that prompted this, the two groups summed to a population the store did not have
and no output said so.

Two halves, because they fail differently:

- a report describing another clustering must not produce a split;
- a report describing *this* clustering must still produce one. Suppressing the
  line unconditionally would pass the first half and remove a signal the product
  calls load-bearing.

Both run through the real stages - `extract`, `snapshot`, `remap`, `verify` over a
real clustered graph - because the identity being compared is written by one stage
and recomputed by another, and a fixture standing in for either would agree with
itself.
"""

from __future__ import annotations

import io as _io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import config  # noqa: E402


SPLIT = "grounding by provenance: carried"


class ProvenanceFreshnessTest(SettingsIsolated):
    """The pipeline case: a real report, a real re-cluster, real digests."""

    # Communities have to clear config.MIN_COMMUNITY_SIZE to get a digest, and
    # there have to be at least `remap`'s floor of summaries for it to run.
    COMMUNITIES = 12
    SIZE = 25

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        (self.root / "graphify-out").mkdir(parents=True)
        config.configure(root=str(self.root))

    def write_graph(self, sizes: dict[str, int]) -> None:
        """A clustered graph holding `sizes` nodes in each named community."""
        nodes = []
        for community, size in sizes.items():
            for index in range(size):
                name = f"Widget{community}x{index}"
                nodes.append(
                    {
                        "id": f"c{community}n{index}",
                        "label": name,
                        "community": int(community),
                        "repo": "svc-alpha",
                        "source_file": f"src/{name}.java",
                    }
                )
        config.GRAPH_PATH.write_text(json.dumps({"nodes": nodes, "links": []}), encoding="utf-8")

    def write_prose(self, communities: list[str]) -> None:
        config.SUMMARIES_PATH.write_text(
            json.dumps(
                {
                    community: f"Handling around Widget{community}x0 in svc-alpha."
                    for community in communities
                }
            ),
            encoding="utf-8",
        )

    def even_clustering(self) -> dict[str, int]:
        return {str(c): self.SIZE for c in range(1, self.COMMUNITIES + 1)}

    def run_stage(self, call) -> tuple[str, str]:
        out, err = _io.StringIO(), _io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            call()
        return out.getvalue(), err.getvalue()

    def build_store(self) -> None:
        """A store whose remap report was written for the clustering it holds."""
        self.write_graph(self.even_clustering())
        self.run_stage(summaries.extract)
        self.write_prose([str(c) for c in range(1, self.COMMUNITIES + 1)])
        self.run_stage(summaries.snapshot)
        self.run_stage(summaries.remap)

    def recluster(self) -> None:
        """The same estate clustered differently, and re-extracted.

        Every community id survives, so every id the report carries still
        resolves against the current summaries - the shape that made the stale
        split look ordinary. Only the membership moved.
        """
        moved = self.even_clustering()
        moved["1"] = self.SIZE + 6
        moved["2"] = self.SIZE + 3
        self.write_graph(moved)
        self.run_stage(summaries.extract)

    # --- the half that must stay noisy ---------------------------------------

    def test_a_report_written_for_this_clustering_still_reports_the_split(self):
        self.build_store()
        out, _ = self.run_stage(summaries.verify)
        # Every summary was carried by a remap onto the clustering it already
        # described, and every one cites only its own digest: 12 carried with
        # their membership unmoved, none flagged, nothing left for the authored
        # group. Which state they land in is #314's subject and is pinned in
        # test_summaries_provenance_states; what this asserts is that the line
        # is printed and describes all 12.
        self.assertIn(f"carried unchanged 0% (0 of {self.COMMUNITIES})", out)
        self.assertIn("authored n/a (0 checked)", out)

    # --- the half that must go quiet -----------------------------------------

    def test_a_report_from_a_previous_clustering_does_not_produce_a_split(self):
        self.build_store()
        self.recluster()
        out, _ = self.run_stage(summaries.verify)
        self.assertNotIn(SPLIT, out)

    def test_ids_reused_by_the_new_clustering_are_not_read_as_a_match(self):
        """The trap: the stale report's ids all resolve, so resolution is no evidence."""
        self.build_store()
        carried = set(json.loads(config.REMAP_REPORT_PATH.read_text(encoding="utf-8"))["carried"])
        self.recluster()
        current = set(json.loads(config.SUMMARIES_PATH.read_text(encoding="utf-8")))
        self.assertLessEqual(
            carried, current, "the fixture must reuse every id, or it proves nothing"
        )
        out, _ = self.run_stage(summaries.verify)
        self.assertNotIn(SPLIT, out)

    def test_the_withheld_split_says_why_on_stderr(self):
        self.build_store()
        self.recluster()
        _, err = self.run_stage(summaries.verify)
        self.assertIn(config.REMAP_REPORT_PATH.name, err)
        self.assertIn("different partition", err)
        self.assertIn("reused", err, "a reader has to be told why the ids resolving means nothing")

    def test_the_report_records_the_clustering_it_was_written_for(self):
        """Without a recorded identity the check falls back to timestamps."""
        self.build_store()
        report = json.loads(config.REMAP_REPORT_PATH.read_text(encoding="utf-8"))
        self.assertTrue(report.get("clustering"), "remap must record what it remapped onto")
        self.recluster()
        self.run_stage(summaries.remap)
        rewritten = json.loads(config.REMAP_REPORT_PATH.read_text(encoding="utf-8"))
        self.assertNotEqual(
            report["clustering"],
            rewritten["clustering"],
            "a different clustering must not fingerprint the same",
        )


class ProvenanceFreshnessWithoutARecordTest(SettingsIsolated):
    """Reports written before the clustering was recorded, which every store has.

    Nothing can be compared against, so freshness rests on the one signal left:
    `extract` rewrites the digests for a new partition, so a report older than
    them may describe a previous one. That is a proxy - it fires on a re-extract
    that changed nothing, and it misses a checkout that rewrote both timestamps -
    and it fails towards withholding the number, which is the direction a
    quantitative claim about the wrong data should fail in.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        config.configure(root=str(self.root))
        config.SUMMARIES_INPUT_PATH.write_text(
            json.dumps(
                [
                    {
                        "id": community,
                        "label": f"Community {community}",
                        "size": 30,
                        "repositories": ["svc-alpha"],
                        "top_nodes": [f"Widget{community} (src/Widget{community}.java)"],
                        "business_features": [],
                        "tickets": [],
                    }
                    for community in (1, 2)
                ]
            ),
            encoding="utf-8",
        )
        config.SUMMARIES_PATH.write_text(
            json.dumps(
                {
                    "1": "Handling around Widget1 in svc-alpha.",
                    "2": "Handling around Widget2 in svc-alpha.",
                }
            ),
            encoding="utf-8",
        )
        config.REMAP_REPORT_PATH.write_text(
            json.dumps({"carried": {"2": {"from": "9", "share": 0.9}}, "displaced": {}}),
            encoding="utf-8",
        )

    def age_the_report(self) -> None:
        digests = config.SUMMARIES_INPUT_PATH.stat().st_mtime
        os.utime(config.REMAP_REPORT_PATH, (digests - 3600, digests - 3600))

    def run_verify(self) -> tuple[str, str]:
        out, err = _io.StringIO(), _io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            summaries.verify()
        return out.getvalue(), err.getvalue()

    def test_a_report_newer_than_the_digests_still_reports_the_split(self):
        out, _ = self.run_verify()
        self.assertIn(SPLIT, out)

    def test_a_report_older_than_the_digests_does_not_produce_a_split(self):
        self.age_the_report()
        out, err = self.run_verify()
        self.assertNotIn(SPLIT, out)
        self.assertIn(config.SUMMARIES_INPUT_PATH.name, err)


if __name__ == "__main__":
    unittest.main()
