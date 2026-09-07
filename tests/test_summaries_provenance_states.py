"""The provenance split must name what the remap did, not that a report mentions it.

`verify` splits its grounding flag rate by provenance so a retention figure is
never read bare. It read the split from membership of the remap report's carried
map, which computes **"present in the last remap report"** while the line claimed
**"written for a different cluster and carried across the move"**. The two
coincide only when a remap actually moved something.

The break these tests catch: an identity remap - unchanged clustering, every
summary retained - puts every summary in the report, so prose authored against
this very clustering was reported as carried. Nothing fails and no group comes
back empty when that happens; the line simply gives the alarming reading for the
harmless case, and a figure that is alarming when it should be quiet gets
discounted, taking the genuinely alarming one with it (#314).

Three halves, because they fail differently:

- an identity remap must not report its summaries as carried across a move;
- a remap that genuinely re-keys prose onto a changed set must still report it
  as carried across a move - without this, labelling everything "unchanged"
  passes the first half and removes the signal the product calls load-bearing;
- the rate must be computed per group. A single rate printed against three
  labels satisfies both halves above and says nothing about any of them.

The pipeline cases run through the real stages - `extract`, `snapshot`, `remap`,
`verify` over a real clustered graph - because the distinction being reported is
written by one stage and read by another, and a hand-made report would agree
with whatever this module decided it should say.
"""

from __future__ import annotations

import io as _io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import config  # noqa: E402


MOVED = "carried across a move"
UNCHANGED = "carried unchanged"
UNRECORDED = "carried with no record of the move"


class ProvenanceStatesTest(SettingsIsolated):
    """A real store, remapped twice: once onto itself, once onto a changed set."""

    # Every community has to clear config.MIN_COMMUNITY_SIZE to get a digest -
    # without one its summary is orphaned rather than checked - and there have to
    # be at least `remap`'s floor of summaries for it to run at all. So the
    # membership fixtures below move nodes *between* communities and never shrink
    # one: sizes stay at SIZE and every summary stays in the checked population.
    COMMUNITIES = 12
    SIZE = 25
    SWAPPED = 5

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        (self.root / "graphify-out").mkdir(parents=True)
        config.configure(root=str(self.root))

    # --- fixtures ------------------------------------------------------------

    def even_membership(self) -> dict[str, list[str]]:
        """Every community holding its own SIZE nodes."""
        return {
            str(community): [f"c{community}n{index}" for index in range(self.SIZE)]
            for community in range(1, self.COMMUNITIES + 1)
        }

    def swapped_membership(self) -> dict[str, list[str]]:
        """The same estate with SWAPPED nodes traded between communities 1 and 2.

        A trade rather than a transfer, so both communities keep SIZE members and
        both keep their digests. Both summaries then describe a set the graph no
        longer holds while still clearing the overlap tolerance, which is the
        state `remap` marks `"exact": false`.
        """
        membership = self.even_membership()
        kept = self.SIZE - self.SWAPPED
        traded = range(kept, self.SIZE)
        membership["1"] = [f"c1n{i}" for i in range(kept)] + [f"c2n{i}" for i in traded]
        membership["2"] = [f"c2n{i}" for i in range(kept)] + [f"c1n{i}" for i in traded]
        return membership

    def write_graph(self, membership: dict[str, list[str]]) -> None:
        nodes = []
        for community, ids in membership.items():
            for node in ids:
                name = "Widget" + node
                nodes.append(
                    {
                        "id": node,
                        "label": name,
                        "community": int(community),
                        "repo": "svc-alpha",
                        "source_file": f"src/{name}.java",
                    }
                )
        config.GRAPH_PATH.write_text(json.dumps({"nodes": nodes, "links": []}), encoding="utf-8")

    def write_prose(self, extra: dict[str, str] | None = None) -> None:
        """Prose citing nothing shaped like an identifier, plus any `extra`.

        Nothing is flagged unless a test asks for it, so every rate on the line
        is 0% and each group's denominator is the group's own size - which is the
        quantity these tests are about. `extra` is how a test buys one flagged
        summary and a rate it can check by hand.
        """
        prose = {
            str(community): "Handling for the intake side of this area."
            for community in range(1, self.COMMUNITIES + 1)
        }
        prose.update(extra or {})
        config.SUMMARIES_PATH.write_text(json.dumps(prose), encoding="utf-8")

    def run_stage(self, call) -> tuple[str, str]:
        out, err = _io.StringIO(), _io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            call()
        return out.getvalue(), err.getvalue()

    def identity_remap(self) -> None:
        """A store whose last remap moved nothing: the reported case."""
        self.write_graph(self.even_membership())
        self.run_stage(summaries.extract)
        self.write_prose()
        self.run_stage(summaries.snapshot)
        self.run_stage(summaries.remap)

    def remap_onto_a_changed_set(self) -> None:
        """The same store re-clustered, re-extracted and remapped for real.

        `--carry overlap`, because set equality is the default criterion and it
        withdraws prose rather than carrying it onto a changed set - so the
        tolerance is the only way a summary reaches the "carried across a move"
        state at all.
        """
        self.identity_remap()
        self.write_graph(self.swapped_membership())
        self.run_stage(summaries.extract)
        self.run_stage(lambda: summaries.remap(carry=summaries.CARRY_OVERLAP))

    def split_line(self, out: str) -> str:
        for line in out.splitlines():
            if "grounding by provenance" in line:
                return line
        self.fail(f"no provenance split in the output:\n{out}")

    def report(self) -> dict:
        return json.loads(config.REMAP_REPORT_PATH.read_text(encoding="utf-8"))

    # --- the half that must go quiet -----------------------------------------

    def test_an_identity_remap_reports_nothing_carried_across_a_move(self):
        """The reported defect: 12 summaries authored here, then a no-op remap.

        Hand-derived: the remap retains all 12 onto the sets they were written
        about, so 12 are carried unchanged, none moved, none authored, and the
        prose cites nothing a digest could fail to hold.
        """
        self.identity_remap()
        out, _ = self.run_stage(summaries.verify)
        self.assertIn(f"{MOVED} n/a (0 checked)", out)
        self.assertIn(f"{UNCHANGED} 0% (0 of {self.COMMUNITIES})", out)
        self.assertIn("authored n/a (0 checked)", out)

    def test_the_identity_remap_left_every_summary_in_the_carried_map(self):
        """Or the test above passes for the wrong reason.

        The line goes quiet either because the states are distinguished or
        because the report is empty and the split never printed. This pins the
        fixture to the shape the defect needs: every summary carried, so
        "present in the report" is true of all 12.
        """
        self.identity_remap()
        carried = self.report()["carried"]
        self.assertEqual(sorted(carried, key=int), [str(c) for c in range(1, 13)])
        self.assertTrue(all(entry["exact"] for entry in carried.values()))

    # --- the half that must stay noisy ---------------------------------------

    def test_prose_re_keyed_onto_a_changed_set_is_reported_as_carried_across_a_move(self):
        """Hand-derived from the trade: communities 1 and 2 each lost SWAPPED
        members and gained SWAPPED others, so their prose describes a set the
        graph no longer holds - 2 carried across a move. The other 10 communities
        did not move, and nothing is authored because every summary was carried.
        """
        self.remap_onto_a_changed_set()
        out, _ = self.run_stage(summaries.verify)
        self.assertIn(f"{MOVED} 0% (0 of 2)", out)
        self.assertIn(f"{UNCHANGED} 0% (0 of {self.COMMUNITIES - 2})", out)
        self.assertIn("authored n/a (0 checked)", out)

    def test_the_remap_marked_exactly_the_two_communities_whose_members_moved(self):
        """The fixture's own evidence, in the same run as the test that needs it.

        A trade that failed to change any membership would leave the test above
        asserting "0 of 2" against a report that moved nothing, and it would pass
        for a reason unrelated to the product.
        """
        self.remap_onto_a_changed_set()
        carried = self.report()["carried"]
        moved = sorted((cid for cid, entry in carried.items() if not entry["exact"]), key=int)
        self.assertEqual(moved, ["1", "2"])
        self.assertEqual(len(carried), self.COMMUNITIES)

    # --- the rate has to belong to its group ---------------------------------

    def test_each_group_carries_its_own_flag_rate(self):
        """One fabricated citation in a moved summary, and nowhere else.

        Hand-derived: 1 of the 2 moved summaries is flagged (50%), 0 of the 10
        unchanged ones (0%). A single rate computed over the whole checked
        population and printed against every label would read 8% (1 of 12)
        against all three, which is the shape that made one number stand for a
        quantity it never measured.
        """
        self.remap_onto_a_changed_set()
        self.write_prose({"1": "Handling routed through FabricatedWidget before intake."})
        out, _ = self.run_stage(summaries.verify)
        self.assertIn(f"{MOVED} 50% (1 of 2)", out)
        self.assertIn(f"{UNCHANGED} 0% (0 of {self.COMMUNITIES - 2})", out)

    def test_every_checked_summary_lands_in_exactly_one_group(self):
        """The groups partition the checked population, or the line describes a
        population the store does not have. Hand-derived: 12 summaries checked,
        so the denominators sum to 12 however they are distributed."""
        self.remap_onto_a_changed_set()
        out, _ = self.run_stage(summaries.verify)
        line = self.split_line(out)
        denominators = [int(n) for n in re.findall(r"of (\d+)\)", line)]
        self.assertEqual(sum(denominators), self.COMMUNITIES, line)

    # --- a report that never recorded the distinction ------------------------

    def test_a_report_predating_the_record_is_not_reported_as_unchanged(self):
        """A missing `"exact"` is not a true one.

        Reports written before `remap` recorded whether it moved a summary carry
        no such field, and no version of this library can produce one now - so
        the report is aged by stripping the field from a real one rather than by
        writing a shape from scratch. Reading it as "unchanged" would print the
        reassuring half of a measurement nobody took, which is the failure the
        precision report already learned once.
        """
        self.identity_remap()
        report = self.report()
        report["carried"] = {
            cid: {key: value for key, value in entry.items() if key != "exact"}
            for cid, entry in report["carried"].items()
        }
        config.REMAP_REPORT_PATH.write_text(json.dumps(report, indent=1), encoding="utf-8")
        out, _ = self.run_stage(summaries.verify)
        self.assertIn(f"{UNRECORDED} 0% (0 of {self.COMMUNITIES})", out)
        self.assertNotIn(f"{UNCHANGED} 0% (0 of {self.COMMUNITIES})", out)
        self.assertIn(f"{MOVED} n/a (0 checked)", out)
        self.assertIn("summaries remap", out, "a reader has to be told how to record it")

    def test_the_unrecorded_group_is_absent_when_every_summary_has_a_record(self):
        """It would otherwise sit on every line at zero and train the eye past
        the two states that matter."""
        self.identity_remap()
        out, _ = self.run_stage(summaries.verify)
        self.assertNotIn(UNRECORDED, out)


if __name__ == "__main__":
    unittest.main()
