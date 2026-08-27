"""A stale-graph message must not prescribe a fix for a direction it cannot know (#243).

The guard in `graph_files` compares the store's two graph files and says correctly
that *one of them* is stale. Its remedy then asserted which: "Decompress the
committed graph over graph.json (gunzip -kf graph.json.gz) and re-run, or remove
graph.json." Both branches destroy `graph.json`, so both are right only when the
archive is the good copy.

Mid-rebuild it is the other way round. The archive is the previous build and
`graph.json` is the fresh merge that has just cost a full extraction pass, so the
message names the most expensive artefact in the store as the thing to discard -
and the command it gives **succeeds**, so nothing reports the loss. It was
reported firing on three of these stages in one documented rebuild sequence.

The fix is not a cleverer guess. The counts of both files are already printed, so
the message can name both directions and the signal that separates them and leave
the one decision it cannot make to the operator.

Each test names the break it catches. The predicate below is checked against the
wording that shipped in the same run - a test asserting the *absence* of an
instruction passes when the message says nothing at all, so it has to be shown to
report the instruction it was written for.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_community_summaries as summaries  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import graph_files  # noqa: E402

# Words naming an action that destroys one of the two graph files: the shell
# commands and the prose forms the shipped wording used. "overwrite" is absent
# deliberately - the refusal uses it to describe what the *stage* would do, which
# is the reason it refuses rather than an instruction to the operator.
DESTROYING = ("gunzip", "gzip", "decompress", "remove", "delete", "rm ")

# A condition the reader can evaluate before typing the command beside it. The
# test is per line, because a reader who copies the first command they see must
# find its condition without scrolling.
CONDITIONAL = re.compile(r"\b(if|when|unless)\b", re.IGNORECASE)

# The wording that shipped, kept verbatim with invented counts as the sensitivity
# fixture for `unconditional_instructions`.
SHIPPED_REFUSAL = (
    "Refusing to run: graph.json has 2 communities over 4 clustered nodes and "
    "graph.json.gz has 3 over 5, so one is stale. This stage rewrites both from "
    "graph.json, which would overwrite graph.json.gz and lose its clustering. "
    "Decompress the committed graph over graph.json (gunzip -kf graph.json.gz) and "
    "re-run, or remove graph.json."
)
SHIPPED_NOTE = (
    "  MISMATCH: graph.json has 2 communities over 4 clustered nodes; graph.json.gz "
    "has 3 over 5. One of them is stale. explorer.html will be built from graph.json. "
    "Decompress the committed graph over it, or remove the stale file, and re-run."
)


def unconditional_instructions(message: str) -> list[str]:
    """Lines telling an operator to destroy a graph file without saying when to."""
    return [
        line.strip()
        for line in message.splitlines()
        if any(word in line.lower() for word in DESTROYING) and not CONDITIONAL.search(line)
    ]


def names_as_stale(message: str, name: str) -> bool:
    """Whether `message` says this file is a candidate for being the stale one.

    Matched on the file name and the claim together. `graph.json` is a prefix of
    `graph.json.gz`, so the lookahead is load-bearing: without it a message naming
    only the archive direction would read as naming both.
    """
    pattern = re.compile(
        rf"{re.escape(name)}(?!\.gz)\s+(?:is|may be|might be|could be)\s+(?:the\s+)?stale",
        re.IGNORECASE,
    )
    return bool(pattern.search(message))


def _graph(members: dict[int, list[str]]) -> dict:
    return {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {"id": node, "label": node, "community": community}
            for community, ids in members.items()
            for node in ids
        ],
        "links": [],
    }


# Hand-counted: 2 communities over 4 clustered nodes, and 3 over 5. Distinct in
# both figures, so no assertion on one can be satisfied by the other's number.
SMALLER = {1: ["a", "b"], 2: ["c", "d"]}
LARGER = {1: ["a", "b"], 2: ["c", "d"], 3: ["e"]}


class StaleMessageFixture(SettingsIsolated):
    """A store holding two disagreeing graph files, with the mtimes set explicitly."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "graphify-out").mkdir(parents=True)
        (self.root / "knowledge" / "summaries").mkdir(parents=True)
        self._old_root = config.ROOT
        self.addCleanup(lambda: config.configure(root=str(self._old_root)))
        config.configure(root=str(self.root))
        self.plain = config.GRAPH_PATH
        self.packed = config.GRAPH_PATH.with_name(config.GRAPH_PATH.name + ".gz")

    def _write_plain(self, members):
        self.plain.write_text(json.dumps(_graph(members)), encoding="utf-8")

    def _write_packed(self, members):
        with gzip.open(self.packed, "wt", encoding="utf-8") as handle:
            json.dump(_graph(members), handle)

    def _mid_rebuild(self):
        """The reported case: `graph.json` is the fresh merge, the archive is older."""
        self._write_packed(SMALLER)
        self._write_plain(LARGER)
        os.utime(self.packed, (1_600_000_000, 1_600_000_000))
        os.utime(self.plain, (1_700_000_000, 1_700_000_000))

    def _abandoned_run(self):
        """The case the guard was written for: `graph.json` is a stale leftover."""
        self._write_plain(SMALLER)
        self._write_packed(LARGER)
        os.utime(self.plain, (1_600_000_000, 1_600_000_000))
        os.utime(self.packed, (1_700_000_000, 1_700_000_000))


class TheRefusalCarriesItsUncertainty(StaleMessageFixture):
    """`graph_files.stale_refusal`, the message that fires before a graph is rewritten."""

    def test_no_destroying_command_appears_without_its_condition(self):
        """The defect itself. Breaks if the refusal goes back to prescribing a
        command for one direction as though it knew which was stale - the reader
        who copies it mid-rebuild destroys a graph that cost a full extraction
        pass, and the command exits 0, so nothing reports the loss."""
        self._mid_rebuild()

        refusal = graph_files.stale_refusal(self.plain)

        self.assertTrue(refusal, "precondition: the two files must disagree")
        self.assertEqual(
            unconditional_instructions(refusal),
            [],
            "a line tells the operator to destroy a graph file without saying when",
        )

    def test_the_check_reports_the_wording_that_shipped(self):
        """The sensitivity check for the test above, in the same run. Asserting the
        absence of an instruction passes over a message that says nothing, so the
        predicate has to be shown to report the instruction it was written for."""
        self.assertEqual(
            unconditional_instructions(SHIPPED_REFUSAL),
            [" ".join(SHIPPED_REFUSAL.split())],
        )
        self.assertEqual(
            unconditional_instructions(SHIPPED_NOTE),
            [" ".join(SHIPPED_NOTE.split())],
        )

    def test_the_refusal_names_both_stale_directions(self):
        """Breaks if the refusal names only one of the two files as the possibly
        stale one. Naming one is what made the shipped remedy readable as an
        answer; the guard refuses precisely because it has no answer."""
        self._mid_rebuild()

        refusal = graph_files.stale_refusal(self.plain)

        self.assertTrue(
            names_as_stale(refusal, self.packed.name),
            f"the archive is never named as the stale one:\n{refusal}",
        )
        self.assertTrue(
            names_as_stale(refusal, self.plain.name),
            f"the plain file is never named as the stale one:\n{refusal}",
        )

    def test_the_refusal_states_the_signal_that_separates_them(self):
        """Breaks if the refusal names both directions and leaves the operator no
        way to tell which they are in. Both counts are printed, so the signal -
        lower counts on the file that predates the other - can be applied without
        running anything; without it the reader has to guess, which is the state
        the fix was meant to end."""
        self._mid_rebuild()

        refusal = graph_files.stale_refusal(self.plain).lower()

        self.assertIn("lower", refusal, "the counts are printed but never read for the reader")
        self.assertRegex(refusal, r"predates|older", "nothing relates the two files in time")
        self.assertRegex(refusal, r"rebuild|merge", "the mid-rebuild direction is not situated")
        self.assertIn("leftover", refusal, "the abandoned-run direction is not situated")

    def test_each_direction_gets_its_own_command(self):
        """Breaks if one direction keeps a command and the other is left as prose -
        the asymmetry the shipped wording had, which is what made one of the two
        readings look like the supported one."""
        self._mid_rebuild()

        refusal = graph_files.stale_refusal(self.plain)

        self.assertEqual(
            refusal.count(f"gzip -kf {self.plain.name}"),
            1,
            f"no single command re-compresses the archive from the merge:\n{refusal}",
        )
        self.assertEqual(
            refusal.count(f"gunzip -kf {self.packed.name}"),
            1,
            f"no single command restores the plain file from the archive:\n{refusal}",
        )

    def test_both_files_counts_still_appear(self):
        """Breaks if the added prose displaces the measurement. The signal is
        useless without the two numbers it is applied to, and an operator who
        cannot see them has to count two graphs by hand."""
        self._mid_rebuild()

        refusal = graph_files.stale_refusal(self.plain)

        self.assertRegex(refusal, r"graph\.json\b(?![.\w])[^\n]*?\bhas 3 communities over 5\b")
        self.assertRegex(refusal, r"graph\.json\.gz\b[^\n]*?\bhas 2 over 4\b")

    def test_a_leftover_plain_file_still_refuses_and_names_that_direction(self):
        """Breaks if widening the message loses the case it was written for. A
        leftover `graph.json` beside a refreshed archive is the original defect,
        and it must still stop the stage and still offer its own way out."""
        self._abandoned_run()

        refusal = graph_files.stale_refusal(self.plain)

        self.assertIn("Refusing to run", refusal)
        self.assertTrue(names_as_stale(refusal, self.plain.name), refusal)
        self.assertIn(f"gunzip -kf {self.packed.name}", refusal)
        self.assertEqual(unconditional_instructions(refusal), [])

    def test_two_agreeing_graphs_produce_no_refusal_at_all(self):
        """The control for every assertion above: they read the text of a refusal,
        and would all pass while the guard fired on the normal case. Both files
        hold the same graph after a successful run, and the archive is tracked, so
        a refusal here makes the stage unreachable."""
        self._write_plain(LARGER)
        self._write_packed(LARGER)

        self.assertEqual(graph_files.stale_refusal(self.plain), "")


class TheAdvisoryNotesCarryItToo(StaleMessageFixture):
    """The non-fatal `MISMATCH` line, which repeated the same prescription.

    Same sentence, quieter consequence: these stages describe the graph rather
    than rewriting it, so the loss is not theirs to cause - but the instruction
    they print destroys the same artefact when it is followed.
    """

    def test_the_shared_artefact_note_gives_no_unconditional_destroying_instruction(self):
        """Breaks if the note shared by the four artefact-writing stages goes back
        to telling an operator to decompress over the plain file or remove it."""
        self._mid_rebuild()
        nodes = _graph(LARGER)["nodes"]

        note = graph_files.stale_note(self.plain, nodes, "explorer.html")

        self.assertIn("MISMATCH", note, "precondition: the two files must disagree")
        self.assertEqual(unconditional_instructions(note), [])

    def test_the_shared_artefact_note_names_both_directions(self):
        """Breaks if the note drops the instruction and says nothing in its place.
        Removing the wrong answer is not the fix; the reader still has to decide,
        and the note is where the two counts are shown."""
        self._mid_rebuild()
        nodes = _graph(LARGER)["nodes"]

        note = graph_files.stale_note(self.plain, nodes, "explorer.html")

        self.assertTrue(names_as_stale(note, self.packed.name), note)
        self.assertTrue(names_as_stale(note, self.plain.name), note)

    def test_the_snapshot_note_gives_no_unconditional_destroying_instruction(self):
        """Breaks if `summaries snapshot` keeps its own copy of the prescription.
        It has one, written separately from the shared note, and a fix to one
        reads as a fix to both."""
        self._mid_rebuild()

        out = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(out):
            code = summaries.snapshot()
        printed = out.getvalue()

        self.assertEqual(code, 0, printed)
        self.assertIn("MISMATCH", printed, "precondition: the two files must disagree")
        self.assertEqual(unconditional_instructions(printed), [])
        self.assertTrue(names_as_stale(printed, self.packed.name), printed)
        self.assertTrue(names_as_stale(printed, self.plain.name), printed)


if __name__ == "__main__":
    unittest.main()
