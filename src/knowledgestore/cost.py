"""What a run consumed, from the API's own usage records. Writes nothing.

    knowledgestore cost <file or directory> [...]

Two kinds of record are read, and each file is read as one of them by its suffix:

    *.json             a headless run's result, as `claude -p --output-format json`
                       prints it: `usage` and `total_cost_usd`, summed per run
    *.jsonl, *.output  a Claude Code transcript, such as a subagent's; usage is
                       summed once per distinct message id

## Why transcripts are de-duplicated

A transcript writes one line per content block of an assistant turn, and every
line carries the whole turn's usage under the same message id. A turn that thinks,
writes and calls a tool appears three times, so summing every line counts it three
times. Measured on one large internal estate, the naive sum read 796M input tokens
and the de-duplicated one 414M. The naive figure is printed beside the real one so
the gap is visible rather than explained.

The seen ids are shared across every file read, not reset per file, because a
transcript that carries turns over from another - a copied, resumed or forked
session - repeats their ids. Files are resolved before reading as well: a task's
`.output` file is a link to the subagent's transcript, so a directory holding both
reaches it twice, and a linked result file is still one run.

## What the numbers are

Input is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`:
everything the model read that turn, which for an agent is its whole context
re-sent. Repeated lines of one turn agree on those three fields, so the first line
is taken.

Output on transcript lines is **under-reported**. The lines are written while the
turn streams, so their `output_tokens` grows from line to line and the last value
is still not the final count. The largest per turn is reported as a floor, never as
the turn's output. Headless results report output, and cost, for the whole run.

No price is applied to transcript usage: the per-token price depends on the model
and changes, and a price table here would be an estimate dressed as a measurement.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RESULT_SUFFIXES = frozenset({".json"})
TRANSCRIPT_SUFFIXES = frozenset({".jsonl", ".output"})


def input_tokens(usage: dict[str, Any]) -> int:
    """Everything the model read: uncached input plus cache writes plus cache reads."""
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
    )


@dataclass
class Headless:
    """Summed over headless result files, one per run."""

    runs: int = 0
    turns: int = 0
    input: int = 0
    output: int = 0
    dollars: float = 0.0

    def add(self, result: dict[str, Any]) -> None:
        usage = result.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        self.runs += 1
        self.turns += int(result.get("num_turns") or 0)
        self.input += input_tokens(usage)
        self.output += int(usage.get("output_tokens") or 0)
        self.dollars += float(result.get("total_cost_usd") or 0.0)


@dataclass
class Transcripts:
    """Summed over transcript lines, once per distinct message id."""

    files: int = 0
    records: int = 0
    input: int = 0
    naive_input: int = 0
    unidentified: int = 0
    largest_output: dict[str, int] = field(default_factory=dict)
    unidentified_output: int = 0

    @property
    def messages(self) -> int:
        return len(self.largest_output) + self.unidentified

    @property
    def output(self) -> int:
        """A floor: the largest value any line of a turn carried, summed over turns."""
        return sum(self.largest_output.values()) + self.unidentified_output

    def add(self, message: dict[str, Any]) -> None:
        usage = message["usage"]
        tokens = input_tokens(usage)
        output = int(usage.get("output_tokens") or 0)
        self.records += 1
        self.naive_input += tokens
        message_id = message.get("id")
        if not isinstance(message_id, str):
            # Nothing says two id-less lines are one turn, so each is its own.
            self.unidentified += 1
            self.input += tokens
            self.unidentified_output += output
            return
        if message_id not in self.largest_output:
            self.input += tokens
            self.largest_output[message_id] = output
        else:
            self.largest_output[message_id] = max(self.largest_output[message_id], output)


@dataclass
class Tally:
    headless: Headless = field(default_factory=Headless)
    transcripts: Transcripts = field(default_factory=Transcripts)
    skipped: int = 0


def files_under(paths: Iterable[Path]) -> list[Path]:
    """Every file named or under a named directory, resolved, each once, sorted."""
    found: set[Path] = set()
    for path in paths:
        candidates = path.rglob("*") if path.is_dir() else [path]
        found.update(candidate.resolve() for candidate in candidates if candidate.is_file())
    return sorted(found)


def assistant_messages(path: Path) -> Iterator[dict[str, Any]]:
    """The assistant messages carrying usage, one per transcript line."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get("message") if isinstance(record, dict) else None
            if (
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and isinstance(message.get("usage"), dict)
            ):
                yield message


def headless_result(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return None
    if isinstance(value, dict) and value.get("type") == "result":
        return value
    return None


def read_transcript(path: Path, tally: Tally) -> None:
    before = tally.transcripts.records
    for message in assistant_messages(path):
        tally.transcripts.add(message)
    if tally.transcripts.records > before:
        tally.transcripts.files += 1
    else:
        tally.skipped += 1


def measure(paths: Iterable[Path]) -> Tally:
    tally = Tally()
    for path in files_under(paths):
        if path.suffix in TRANSCRIPT_SUFFIXES:
            read_transcript(path, tally)
            continue
        result = headless_result(path) if path.suffix in RESULT_SUFFIXES else None
        if result is None:
            tally.skipped += 1
        else:
            tally.headless.add(result)
    return tally


def report_headless(headless: Headless) -> list[str]:
    if not headless.runs:
        return []
    return [
        f"Headless results: {headless.runs:,} runs, {headless.turns:,} turns",
        f"  input {headless.input:,} tokens, output {headless.output:,} tokens",
        f"  cost ${headless.dollars:,.2f} (total_cost_usd, as the runs reported it)",
    ]


def report_transcripts(transcripts: Transcripts) -> list[str]:
    if not transcripts.files:
        return []
    lines = [
        f"Transcripts: {transcripts.files:,} files, {transcripts.records:,} usage records, "
        f"{transcripts.messages:,} distinct messages",
        f"  input {transcripts.input:,} tokens, de-duplicated by message id "
        f"(summing every record would read {transcripts.naive_input:,})",
        f"  output at least {transcripts.output:,} tokens - transcript records under-report it",
        "  no cost: transcripts carry no price; run headless for total_cost_usd",
    ]
    if transcripts.unidentified:
        lines.append(
            f"  {transcripts.unidentified:,} records carried no message id and were "
            "each counted as a turn of their own"
        )
    return lines


def report(tally: Tally) -> list[str]:
    lines = report_headless(tally.headless) + report_transcripts(tally.transcripts)
    if tally.skipped:
        lines.append(
            f"Skipped {tally.skipped:,} files that were neither, and read nothing from them"
        )
    return lines


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore cost",
        description="Report what a run consumed, from headless result files and "
        "transcripts, de-duplicating transcript usage by message id. Writes nothing.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="result files, transcripts, or directories holding them (read recursively)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    tally = measure(arguments.paths)
    for line in report(tally):
        print(line, flush=True)
    if not tally.headless.runs and not tally.transcripts.files:
        print(
            "No headless result or transcript was read, so there is no cost to report - "
            "a zero here would be a measurement of nothing.",
            file=sys.stderr,
            flush=True,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
