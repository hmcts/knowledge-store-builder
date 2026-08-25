"""Fan-out progress must come from the artefacts, and must name what was never sent (#131).

Two properties of the semantic fan-out cost an operator real work, and both are
bookkeeping rather than extraction:

- **The concurrency ceiling rejects rather than queues**, so a chunk can be recorded
  as dispatched and never launched. Nothing will ever produce it, and it looks
  exactly like an agent still working. Dispatch is plan-ordered, so a rejected
  low-numbered chunk hides behind every higher-numbered one that followed.
- **A dispatch log is a cache of intent, not a record of fact.** A coverage gap of
  ninety-odd chunks was reported by diffing the plan against a log without
  intersecting disk, and a redundant round of agents was launched for it. Separately,
  a log corrupted by appending files with no trailing newline produced tokens
  matching no chunk, which counted as dispatched-but-absent and for several rounds
  inflated `in flight` and deflated `NEVER SENT` while every total stayed plausible.

Each test below names the production change that should make it fail. Expected values
are derived by hand from fixtures written here, never from the code under test.
"""

from __future__ import annotations

import io as _io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledgestore import chunk_status  # noqa: E402


class ChunkStatusTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.chunks = self.root / "graphify-out"
        self.chunks.mkdir(parents=True)
        self.plan_path = self.chunks / ".graphify_chunk_plan.json"

    # --- fixtures -----------------------------------------------------------

    def plan(self, *identifiers: str) -> None:
        """A plan naming exactly these chunk ids, each with one file."""
        self.plan_path.write_text(
            json.dumps({i: [f"repositories/demo/doc-{i}.md"] for i in identifiers}),
            encoding="utf-8",
        )

    def extraction(self, identifier: str, nodes: int = 1) -> None:
        (self.chunks / f".graphify_chunk_{identifier}.json").write_text(
            json.dumps({"nodes": [{"id": f"n{n}"} for n in range(nodes)], "edges": []}),
            encoding="utf-8",
        )

    def log(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_stage(self, *argv: str) -> tuple[int, str]:
        buffer = _io.StringIO()
        with redirect_stdout(buffer):
            code = chunk_status.main(
                ["--plan", str(self.plan_path), "--chunks", str(self.chunks), *argv]
            )
        return code, buffer.getvalue()

    # --- progress comes from disk -------------------------------------------

    def test_a_chunk_on_disk_but_absent_from_the_log_counts_as_done(self):
        """Breaks if `done` is derived from the dispatch log instead of from disk.

        This is the reported defect exactly: the log did not cover the early rounds,
        so a plan-minus-log diff announced a gap of chunks whose extractions were on
        disk the whole time, and a redundant round of agents was launched for it.
        """
        self.plan("0001", "0002", "0003")
        self.extraction("0001")
        self.extraction("0002")
        code, output = self.run_stage("--dispatched", str(self.log("d.txt", "0003\n")))

        self.assertEqual(code, 0)
        self.assertIn("done        2 of 3 planned chunk(s), read from disk", output)

    def test_a_chunk_in_the_log_with_no_output_is_not_done(self):
        """Breaks if the log can mark a chunk complete.

        A dispatch record is intent. Counting it as fact is what makes a fan-out
        report itself finished with work outstanding, which is unrecoverable later
        because the archive is then merged and the gap is invisible.
        """
        self.plan("0001", "0002")
        self.extraction("0001")
        _code, output = self.run_stage("--dispatched", str(self.log("d.txt", "0001\n0002\n")))

        self.assertIn("done        1 of 2 planned chunk(s), read from disk", output)
        self.assertIn("in flight   1 dispatched, no output yet", output)

    def test_an_unusable_chunk_file_is_not_counted_as_done(self):
        """Breaks if a truncated or empty chunk file counts as progress.

        An agent killed mid-write leaves malformed JSON, and an agent that hit the
        output limit leaves a fragment. Both are files, so a reader that counts files
        reports the chunk as extracted and it is never redone. `io.read_json_dict`
        raises on malformed JSON, so counting on it alone would also abort the report.
        """
        self.plan("0001", "0002")
        self.extraction("0001")
        (self.chunks / ".graphify_chunk_0002.json").write_text('{"nodes": [{"id"', encoding="utf-8")

        code, output = self.run_stage()

        self.assertEqual(code, 0, "a malformed chunk file must not abort the report")
        self.assertIn("done        1 of 2 planned chunk(s), read from disk", output)
        self.assertIn("UNUSABLE    1 chunk file(s)", output)
        self.assertIn(".graphify_chunk_0002.json", output)

    def test_a_well_formed_file_with_no_nodes_key_is_not_done(self):
        """Breaks if any parseable JSON counts as an extraction.

        Distinct from the malformed case above, and the one that matters more: an
        agent that died having written only its opening object, or a wrapper that
        wrote a status object instead of an extraction, leaves valid JSON holding no
        extraction at all. `merge-chunks` refuses such a file too, so counting it here
        reports a chunk done that the merge will not read - a gap that surfaces only
        once the archive has been assembled.
        """
        self.plan("0001", "0002")
        self.extraction("0001")
        (self.chunks / ".graphify_chunk_0002.json").write_text('{"status": "ok"}', encoding="utf-8")

        _code, output = self.run_stage()

        self.assertIn("done        1 of 2 planned chunk(s), read from disk", output)
        self.assertIn("UNUSABLE    1 chunk file(s)", output)

    def test_the_plan_is_not_mistaken_for_an_extraction(self):
        """Breaks if the plan file is globbed as a chunk output.

        `.graphify_chunk_plan.json` matches `.graphify_chunk_*.json`. Counting it
        would add a phantom completed chunk and, worse, report a plan id the plan
        does not name - a wrong denominator and a wrong numerator at once.
        """
        self.plan("0001")
        code, output = self.run_stage()

        self.assertEqual(code, 0)
        self.assertIn("done        0 of 1 planned chunk(s), read from disk", output)
        self.assertNotIn("UNPLANNED", output)
        self.assertNotIn("UNUSABLE", output)

    # --- never sent is separated, and reported first ------------------------

    def test_never_sent_is_separated_from_in_flight(self):
        """Breaks if the two causes of "no output" are merged.

        They need opposite responses: an in-flight chunk needs waiting for, a
        never-sent chunk needs dispatching and will otherwise wait forever, because
        the ceiling rejected it rather than queuing it.
        """
        self.plan("0001", "0002", "0003", "0004")
        self.extraction("0001")
        _code, output = self.run_stage("--dispatched", str(self.log("d.txt", "0001\n0002\n")))

        self.assertIn(
            "NEVER SENT  2 planned chunk(s) with no output and no dispatch record", output
        )
        self.assertIn("0003, 0004", output)
        self.assertIn("in flight   1 dispatched, no output yet", output)

    def test_never_sent_is_reported_before_everything_else(self):
        """Breaks if the report is reordered so never-sent follows the totals.

        The reported failure was one of visibility, not of computation: rejected
        low-numbered chunks sat unnoticed behind ninety higher-numbered ids because
        dispatch was plan-ordered. A correct number printed last is what happened
        already.
        """
        self.plan("0001", "0002")
        self.extraction("0001")
        _code, output = self.run_stage("--dispatched", str(self.log("d.txt", "0001\n")))

        lines = [line for line in output.splitlines() if line and not line.startswith(" ")]
        self.assertTrue(lines[0].startswith("NEVER SENT"), f"first line was {lines[0]!r}")
        self.assertTrue(any(line.startswith("done") for line in lines))

    def test_without_a_log_the_split_is_reported_as_unknown(self):
        """Breaks if an absent log is read as "nothing was dispatched".

        Silently reporting every outstanding chunk as NEVER SENT would send an
        operator to redispatch work that is in progress - the opposite error, and
        equally expensive.
        """
        self.plan("0001", "0002")
        self.extraction("0001")
        _code, output = self.run_stage()

        self.assertIn("NEVER SENT  unknown: no --dispatched log given", output)
        self.assertIn("1 chunk(s) without output", output)

    def test_a_named_log_that_does_not_exist_is_reported(self):
        """Breaks if a mistyped log path is silently treated as an empty one.

        An empty log makes every outstanding chunk NEVER SENT, which reads as a
        catastrophic dispatch failure. The tool must say the log is missing rather
        than describe the estate wrongly.
        """
        self.plan("0001")
        _code, output = self.run_stage("--dispatched", str(self.root / "absent.txt"))

        self.assertIn("WARNING: 1 dispatch log(s) do not exist", output)

    # --- the log is validated, never trusted --------------------------------

    def test_a_log_token_matching_no_chunk_is_reported_not_counted(self):
        """Breaks if unrecognised log tokens are counted as dispatched.

        This is the corrupt-log defect. Counted, the fused tokens became
        dispatched-but-absent entries: `in flight` rose, `NEVER SENT` fell, and every
        total stayed plausible for several rounds. Both numbers are asserted here,
        because a version that reports the corruption *and* counts it would pass a
        check on the warning alone.
        """
        self.plan("0001", "0002")
        _code, output = self.run_stage(
            "--dispatched", str(self.log("d.txt", "0001\nnot-a-chunk\n"))
        )

        self.assertIn("CORRUPT LOG 1 dispatch-log token(s) match no chunk in the plan", output)
        self.assertIn("not-a-chunk", output)
        self.assertIn("in flight   1 dispatched, no output yet", output)
        self.assertIn("NEVER SENT  1 planned chunk(s)", output)

    def test_fused_ids_are_diagnosed_as_a_concatenation(self):
        """Breaks if the tool reports "unrecognised" without naming the shape.

        The cause was `cat batch_*.txt >> log` where no batch file ended in a
        newline, so the last id of one fused onto the first of the next. Diagnosing
        it is the difference between a puzzle and a one-line fix.
        """
        self.plan("0001", "0002", "0003")
        _code, output = self.run_stage("--dispatched", str(self.log("d.txt", "00010002\n0003\n")))

        self.assertIn("look like ids run together - 00010002 is 0001 + 0002", output)

    def test_a_fused_token_is_diagnosed_but_never_repaired(self):
        """Breaks if the diagnosis silently credits the ids it recovered.

        Repairing the log here would hide a corrupt log and make its numbers
        authoritative, which is the state that misled an operator for several rounds.
        The two ids must still be NEVER SENT.
        """
        self.plan("0001", "0002", "0003")
        self.extraction("0003")
        _code, output = self.run_stage("--dispatched", str(self.log("d.txt", "00010002\n")))

        self.assertIn("NEVER SENT  2 planned chunk(s)", output)
        self.assertIn("0001, 0002", output)

    def test_a_log_of_chunk_filenames_is_understood(self):
        """Breaks if only bare ids are accepted.

        Operators log what they dispatched, and the dispatched thing is a file. A
        reader that rejects filenames reports a whole log as corrupt, which is a
        false alarm expensive enough that the tool gets abandoned.
        """
        self.plan("0001", "0002")
        _code, output = self.run_stage(
            "--dispatched", str(self.log("d.txt", ".graphify_chunk_0001.json\n"))
        )

        self.assertIn("in flight   1 dispatched, no output yet", output)
        self.assertNotIn("CORRUPT LOG", output)

    def test_several_logs_are_read_together(self):
        """Breaks if only the last `--dispatched` argument survives.

        Dispatch happens in rounds and each round is its own file; an operator who
        passes five and is answered about one gets a confidently wrong NEVER SENT
        list, which is what a redundant round of agents costs.
        """
        self.plan("0001", "0002", "0003")
        first = self.log("round-1.txt", "0001\n")
        second = self.log("round-2.txt", "0002\n")
        _code, output = self.run_stage("--dispatched", str(first), "--dispatched", str(second))

        self.assertIn("in flight   2 dispatched, no output yet", output)
        self.assertIn("NEVER SENT  1 planned chunk(s)", output)

    def test_several_logs_may_follow_one_flag(self):
        """Breaks if a shell glob expanded after `--dispatched` is rejected or truncated.

        Dispatch happens in rounds and each round is its own file, so the natural
        invocation names several at once. Accepting only the first would answer about
        one round and report the rest as NEVER SENT.
        """
        self.plan("0001", "0002", "0003")
        first = self.log("round-1.txt", "0001\n")
        second = self.log("round-2.txt", "0002\n")
        _code, output = self.run_stage("--dispatched", str(first), str(second))

        self.assertIn("in flight   2 dispatched, no output yet", output)

    def test_the_tokens_read_are_counted_and_the_logs_named(self):
        """Breaks if the log parse is silent.

        A tool's own count is not verification unless it says what it counted. The
        reconciliation an operator needs is between the ids they dispatched and the
        ids this stage read, and that is impossible if it names neither.
        """
        self.plan("0001", "0002")
        _code, output = self.run_stage("--dispatched", str(self.log("round-1.txt", "0001\n0002\n")))

        self.assertIn("read 2 dispatch token(s) from round-1.txt", output)

    def test_a_log_holding_no_tokens_is_called_out(self):
        """Breaks if an empty log is silently read as "nothing was dispatched".

        Identical in the report to a fan-out where dispatch failed entirely: every
        outstanding chunk becomes NEVER SENT. The operator has to be told the log was
        empty, not the estate.
        """
        self.plan("0001")
        _code, output = self.run_stage("--dispatched", str(self.log("round-1.txt", "\n\n")))

        self.assertIn("NONE of them held a token", output)

    # --- output the plan does not account for --------------------------------

    def test_output_for_an_unplanned_chunk_is_reported(self):
        """Breaks if extractions from a superseded plan are ignored.

        Chunk numbering is the archive's only index and it moves when `--chunk-size`
        changes. Silently ignoring such a file lets an operator believe an archive is
        addressable by the current plan when part of it is not.
        """
        self.plan("0001")
        self.extraction("0001")
        self.extraction("0099")
        _code, output = self.run_stage()

        self.assertIn("UNPLANNED   1 chunk file(s) whose id the plan does not name", output)
        self.assertIn(".graphify_chunk_0099.json", output)
        self.assertIn("done        1 of 1 planned chunk(s)", output)

    # --- refusing rather than guessing ---------------------------------------

    def test_no_plan_is_refused_rather_than_estimated(self):
        """Breaks if the stage reports progress with no denominator.

        Without the plan there is no way to name a missing chunk, so any figure would
        be derived from the log alone - the exact reasoning error this stage exists to
        remove. It must refuse, and the non-zero code is what stops a script
        proceeding on it.
        """
        self.extraction("0001")
        code, output = self.run_stage()

        self.assertEqual(code, 2)
        self.assertIn("Run `knowledgestore chunk-plan` first", output)
        self.assertNotIn("done ", output)

    def test_an_incomplete_fanout_still_exits_zero(self):
        """Breaks if coverage gaps become a non-zero exit.

        A fan-out in progress is incomplete by definition; failing on that would make
        the tool unusable in the loop it exists for. Only being unable to answer is a
        failure.
        """
        self.plan("0001", "0002")
        code, _output = self.run_stage()

        self.assertEqual(code, 0)

    # --- the report is not vacuous -------------------------------------------

    def test_a_complete_fanout_names_no_outstanding_chunk(self):
        """Breaks if the outstanding lists are populated unconditionally.

        Without this, every assertion above could be satisfied by a report that
        always prints every id, which would be noise indistinguishable from a
        finding.
        """
        self.plan("0001", "0002")
        self.extraction("0001")
        self.extraction("0002")
        _code, output = self.run_stage("--dispatched", str(self.log("d.txt", "0001\n0002\n")))

        self.assertIn("NEVER SENT  none: every outstanding chunk appears in a dispatch log", output)
        self.assertIn("done        2 of 2 planned chunk(s)", output)
        self.assertNotIn("in flight", output)


class FusedIdsTest(unittest.TestCase):
    """The diagnosis must not fire on a token that merely looks long.

    A false positive here tells an operator their log is corrupt in a specific way it
    is not, and sends them to rewrite a correct file.
    """

    PLAN = {"0001", "0002", "0003"}

    def test_a_concatenation_of_plan_ids_is_recognised(self):
        """Breaks if the width arithmetic is wrong - the whole diagnosis."""
        self.assertEqual(chunk_status.fused_ids("00030001", self.PLAN), ["0003", "0001"])

    def test_a_token_whose_parts_are_not_plan_ids_is_not_diagnosed(self):
        """Breaks if any correctly-sized token is declared fused.

        `00019999` is eight characters and splits cleanly, but `9999` names no chunk,
        so this is a token of unknown origin rather than two ids run together.
        """
        self.assertEqual(chunk_status.fused_ids("00019999", self.PLAN), [])

    def test_a_single_id_width_token_is_not_diagnosed(self):
        """Breaks if a plain unknown id is called a concatenation of one."""
        self.assertEqual(chunk_status.fused_ids("9999", self.PLAN), [])

    def test_a_token_of_the_wrong_length_is_not_diagnosed(self):
        """Breaks if a token that is not a whole multiple of the id width is split."""
        self.assertEqual(chunk_status.fused_ids("000100", self.PLAN), [])

    def test_mixed_width_plan_ids_disable_the_diagnosis(self):
        """Breaks if the diagnosis runs where it cannot be sound.

        With ids of several widths a split is ambiguous, and a guess presented as a
        diagnosis is worse than silence.
        """
        self.assertEqual(chunk_status.fused_ids("00010002", {"0001", "0002", "1"}), [])


if __name__ == "__main__":
    unittest.main()
