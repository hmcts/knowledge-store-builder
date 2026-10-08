"""The cost stage: what a run consumed, read from the API's own usage records.

Every fixture is forged, carrying the real record shape and no real content. A
Claude Code transcript writes one line per content block of an assistant turn, and
every one of those lines carries the whole turn's usage under the same message id -
so a turn that produced a thinking block, a text block and a tool call appears three
times. Summing every line is the mistake this stage exists to prevent: measured on
one large internal estate, the naive sum read 796M where the de-duplicated one read
414M.

Expected values are worked out by hand in each fixture's comment, never with the
code under test.
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import os
import tempfile
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import cli, cost


def _usage(uncached: int, write: int, read: int, output: int) -> dict:
    return {
        "input_tokens": uncached,
        "cache_creation_input_tokens": write,
        "cache_read_input_tokens": read,
        "output_tokens": output,
        "service_tier": "standard",
    }


def _assistant(message_id: str, usage: dict, block: str) -> dict:
    """One transcript line: one content block of one assistant turn."""
    return {
        "type": "assistant",
        "isSidechain": True,
        "requestId": "req_forged",
        "message": {
            "id": message_id,
            "role": "assistant",
            "model": "forged-model",
            "content": [{"type": block}],
            "usage": usage,
        },
    }


def _other(kind: str) -> dict:
    return {"type": kind, "message": {"role": "user", "content": "forged"}}


# Turn msg_a: 100 uncached + 1,000 cache write + 5,000 cache read = 6,100, written
# as three content blocks; output streams 1 -> 1 -> 40.
# Turn msg_b: 10 + 0 + 7,000 = 7,010, written as two blocks; output 5 -> 9.
# De-duplicated input: 6,100 + 7,010 = 13,110.
# Naive input: 3 x 6,100 + 2 x 7,010 = 18,300 + 14,020 = 32,320.
# Output, largest per turn: 40 + 9 = 49 (a sum over lines would read 56).
TRANSCRIPT = [
    _other("user"),
    _assistant("msg_a", _usage(100, 1000, 5000, 1), "thinking"),
    _assistant("msg_a", _usage(100, 1000, 5000, 1), "text"),
    _assistant("msg_a", _usage(100, 1000, 5000, 40), "tool_use"),
    _other("attachment"),
    _other("user"),
    _assistant("msg_b", _usage(10, 0, 7000, 5), "text"),
    _assistant("msg_b", _usage(10, 0, 7000, 9), "tool_use"),
]


def _result(uncached: int, write: int, read: int, output: int, dollars: float, turns: int):
    """The object `claude -p --output-format json` prints for one headless run."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": turns,
        "result": "forged",
        "session_id": "forged",
        "total_cost_usd": dollars,
        "usage": _usage(uncached, write, read, output),
    }


class CostStage(SettingsIsolated):
    def _dir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name).resolve()

    def _jsonl(self, path: Path, lines: list[dict]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        return path

    def _json(self, path: Path, value: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _run(self, *arguments: str) -> tuple[int, str]:
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["cost", *arguments])
        return code, out.getvalue()

    def test_a_turn_written_as_several_content_blocks_is_counted_once(self):
        """Break: summing every usage line instead of one per message id."""
        root = self._dir()
        self._jsonl(root / "agent-1.jsonl", TRANSCRIPT)
        tally = cost.measure([root])
        self.assertEqual(tally.transcripts.input, 13_110)
        self.assertEqual(tally.transcripts.naive_input, 32_320)
        self.assertEqual(tally.transcripts.records, 5)
        self.assertEqual(tally.transcripts.messages, 2)

    def test_the_report_gives_the_de_duplicated_figure_as_the_input(self):
        """Break: the printed report quoting the naive sum as the run's input."""
        root = self._dir()
        self._jsonl(root / "agent-1.jsonl", TRANSCRIPT)
        code, out = self._run(str(root))
        self.assertEqual(code, 0, out)
        self.assertIn("input 13,110", out)
        self.assertIn("32,320", out, "the naive figure is shown so the gap is visible")
        self.assertNotIn("input 32,320", out)

    def test_output_is_the_largest_record_per_turn(self):
        """Break: summing output over lines (56) or keeping the first line's (6)."""
        root = self._dir()
        self._jsonl(root / "agent-1.jsonl", TRANSCRIPT)
        self.assertEqual(cost.measure([root]).transcripts.output, 49)

    def test_one_turn_seen_in_two_transcripts_is_counted_once(self):
        """Break: resetting the seen ids per file. A transcript that carries turns over
        from another - a copied, resumed or forked session - repeats their ids."""
        root = self._dir()
        self._jsonl(root / "agent-1.jsonl", TRANSCRIPT)
        self._jsonl(root / "copy" / "agent-1.jsonl", TRANSCRIPT)
        tally = cost.measure([root])
        self.assertEqual(tally.transcripts.input, 13_110)
        self.assertEqual(tally.transcripts.files, 2)

    def test_a_transcript_reached_through_a_link_is_read_once(self):
        """Break: reading a link and its target as two files. A task's `.output` file is
        a link to the subagent's transcript, so a directory holding both reaches it twice."""
        root = self._dir()
        target = self._jsonl(root / "subagents" / "agent-1.jsonl", TRANSCRIPT)
        os.symlink(target, root / "task.output")
        tally = cost.measure([root])
        self.assertEqual(tally.transcripts.files, 1)
        self.assertEqual(tally.transcripts.records, 5)

    def test_a_record_with_no_message_id_is_counted_on_its_own(self):
        """Break: keying the seen set on a missing id, which folds every id-less record
        into the first one and under-counts."""
        root = self._dir()
        line = _assistant("x", _usage(1, 2, 3, 4), "text")
        del line["message"]["id"]
        self._jsonl(root / "agent-1.jsonl", [line, line])
        tally = cost.measure([root])
        # 1 + 2 + 3 = 6 per record, two records with nothing to say they are one turn.
        self.assertEqual(tally.transcripts.input, 12)
        self.assertEqual(tally.transcripts.unidentified, 2)

    def test_headless_results_sum_usage_and_cost(self):
        """Break: dropping the cache fields from input, or not summing total_cost_usd."""
        root = self._dir()
        # Run 1: 3 + 20,000 + 100,000 = 120,003 input, 900 output, $0.25, 12 turns.
        # Run 2: 7 + 5,000 + 40,000 = 45,007 input, 300 output, $0.50, 8 turns.
        self._json(root / "batch_1.result.json", _result(3, 20_000, 100_000, 900, 0.25, 12))
        self._json(root / "batch_2.result.json", _result(7, 5_000, 40_000, 300, 0.5, 8))
        tally = cost.measure([root])
        self.assertEqual(tally.headless.runs, 2)
        self.assertEqual(tally.headless.input, 165_010)
        self.assertEqual(tally.headless.output, 1_200)
        self.assertEqual(tally.headless.turns, 20)
        self.assertAlmostEqual(tally.headless.dollars, 0.75)
        code, out = self._run(str(root))
        self.assertEqual(code, 0, out)
        self.assertIn("input 165,010", out)
        self.assertIn("$0.75", out)

    def test_a_result_file_reached_twice_is_counted_once(self):
        """Break: reading a linked result file as a second run."""
        root = self._dir()
        target = self._json(root / "runs" / "batch_1.result.json", _result(1, 2, 3, 4, 0.1, 1))
        os.symlink(target, root / "latest.json")
        self.assertEqual(cost.measure([root]).headless.runs, 1)

    def test_files_that_are_neither_are_skipped_and_reported(self):
        """Break: counting a JSON file that is not a run result, or hiding that it was
        passed over - a directory of the wrong files must not read as a cheap run."""
        root = self._dir()
        self._json(root / "batch_1.result.json", _result(1, 2, 3, 4, 0.1, 1))
        self._json(root / "chunk.json", {"nodes": [], "edges": []})
        (root / "notes.txt").write_text("forged", encoding="utf-8")
        tally = cost.measure([root])
        self.assertEqual(tally.headless.runs, 1)
        self.assertEqual(tally.skipped, 2)

    def test_reading_nothing_measurable_exits_two(self):
        """Break: reporting zero as a measurement when no run was read."""
        root = self._dir()
        self._json(root / "chunk.json", {"nodes": []})
        code, out = self._run(str(root))
        self.assertEqual(code, 2, out)
        self.assertIn("No headless result or transcript", out)
