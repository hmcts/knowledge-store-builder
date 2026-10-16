"""Compare what new chunk extractions yield per file against the layer they replace.

    knowledgestore check-yield --chunks DIR --baseline FILE [--floor 0.5] [--file-floor 0.5]

The chunk gate checks shape, never quantity: a wave can pass `check-chunk` on every
chunk and still hold a fraction of the nodes the layer it replaces held for the same
files. This counts nodes per `source_file` on both sides and compares only the files
present in both, so a baseline covering the whole estate can be checked against a
delta. The files each side holds alone are counted and reported, not compared.

A file the new chunks hold no node for is on the baseline side only, so it does not
lower the ratio. `check-chunk` requires a node for every file of a chunk, so run it
first: over a wave that passed it, no planned file is missing from the new side.

Exit 0 when the overall ratio is at or above `--floor`, 1 when it is below, and 2
when an input cannot be read or no file is in both: a check that compared nothing
must not pass (CLAUDE.md 2.10).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

from . import check_chunk, chunk_status
from . import io as pio

BELOW_FLOOR = 1
UNUSABLE = 2
WORST = 20


class Unusable(Exception):
    """An input that cannot be compared; ends the run with exit 2 and this message."""


def _count(found: list, into: Counter) -> int:
    """Add each node's `source_file` to `into`; return how many nodes named none."""
    unnamed = 0
    for node in found:
        source = node.get("source_file") if isinstance(node, dict) else None
        if isinstance(source, str) and source:
            into[source] += 1
        else:
            unnamed += 1
    return unnamed


def chunk_counts(directory: Path) -> tuple[Counter, int, int]:
    """(nodes per source_file, chunk files read, nodes naming no file) over `directory`.

    The files are the ones `chunk-status` and `merge-chunks` read, parsed by
    `check-chunk`'s own reader, so a file those call unusable is unusable here too.
    """
    if not directory.is_dir():
        raise Unusable(f"no chunk directory at {directory}")
    counts: Counter = Counter()
    read = unnamed = 0
    problems: list[str] = []
    for path in sorted(directory.glob(chunk_status.CHUNK_GLOB)):
        # The plan and its variants share the prefix; a chunk's id is a number.
        if not chunk_status.chunk_id(path.name).isdigit():
            continue
        payload, violation = check_chunk._read_chunk(path, path.name)
        if violation is not None or payload is None:
            problems.append(str(violation))
            continue
        read += 1
        unnamed += _count(payload["nodes"], counts)
    if problems:
        raise Unusable(
            "these chunk files cannot be read, so a ratio would describe part of the wave:\n"
            + "\n".join(f"  {p}" for p in problems)
        )
    if not read:
        raise Unusable(f"no {chunk_status.CHUNK_GLOB} files in {directory}")
    return counts, read, unnamed


def baseline_counts(path: Path) -> tuple[Counter, int]:
    """(nodes per source_file, nodes naming no file) in a graph or extraction, gzipped or not."""
    try:
        payload = pio.read_json(path)
    except (OSError, ValueError, EOFError) as error:
        raise Unusable(f"the baseline {path} cannot be read: {error}") from error
    if payload is None:
        raise Unusable(f"no baseline at {path}")
    found = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(found, list):
        raise Unusable(f"the baseline {path} has no `nodes` list")
    counts: Counter = Counter()
    unnamed = _count(found, counts)
    return counts, unnamed


def _times(value: Fraction) -> str:
    return f"{float(value):.2f}x"


def compare(
    new: Counter, base: Counter, floor: Fraction, file_floor: Fraction
) -> tuple[list[str], bool]:
    """The report lines, and whether the overall ratio is below `floor`."""
    shared = new.keys() & base.keys()
    if not shared:
        example_new = min(new) if new else "(none)"
        example_base = min(base) if base else "(none)"
        raise Unusable(
            "no file is in both the new chunks and the baseline, so nothing was compared. "
            "Check the two cite files the same way - for example, both absolute:\n"
            f"  new chunks cite:  {example_new}\n"
            f"  baseline cites:   {example_base}"
        )
    ratios = {f: Fraction(new[f], base[f]) for f in shared}
    new_total = sum(new[f] for f in shared)
    base_total = sum(base[f] for f in shared)
    overall = Fraction(new_total, base_total)
    below = sorted(f for f in shared if ratios[f] < file_floor)
    ranked = sorted(shared, key=lambda f: (ratios[f], f))
    lines = [
        f"yield: {new_total:,} nodes against {base_total:,} over {len(shared):,} shared files "
        f"= {_times(overall)} (floor {_times(floor)})",
        f"files only in the new chunks: {len(new.keys() - shared):,}; "
        f"only in the baseline: {len(base.keys() - shared):,}",
        f"files below {_times(file_floor)} their baseline: {len(below):,} of {len(shared):,}",
        f"worst {min(WORST, len(ranked))} of {len(ranked):,}:",
    ]
    lines += [f"  {_times(ratios[f])}  {new[f]:>6} / {base[f]:>6}  {f}" for f in ranked[:WORST]]
    failed = overall < floor
    if failed:
        lines.append(f"BELOW FLOOR: {_times(overall)} is under {_times(floor)}")
    return lines, failed


def _ratio(text: str) -> Fraction:
    """A non-negative ratio exactly as typed: `0.38` is 38/100, not the nearest float."""
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError) as error:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from error
    if value < 0:
        raise argparse.ArgumentTypeError(f"{text} is negative")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore check-yield",
        description="Compare nodes per file in new chunk extractions against the layer "
        "they replace, over the files both hold.",
    )
    parser.add_argument(
        "--chunks",
        type=Path,
        required=True,
        help="directory holding the new .graphify_chunk_*.json files",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        required=True,
        help="the layer being replaced: a graph or extraction JSON with `nodes`, .gz or not",
    )
    parser.add_argument(
        "--floor",
        type=_ratio,
        default="0.5",
        help="exit 1 when new nodes over baseline nodes, on the shared files, is below "
        "this (default 0.5)",
    )
    parser.add_argument(
        "--file-floor",
        type=_ratio,
        default="0.5",
        help="count the files whose own ratio is below this (default 0.5); reported only",
    )
    # A string default goes through `type` like a typed value, so both floors are
    # Fractions whether or not they were given.
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        new, read, new_unnamed = chunk_counts(arguments.chunks)
        base, base_unnamed = baseline_counts(arguments.baseline)
        lines, failed = compare(new, base, arguments.floor, arguments.file_floor)
    except Unusable as error:
        print(f"check-yield: {error}", file=sys.stderr)
        return UNUSABLE
    print(
        f"read: {read:,} chunk files in {arguments.chunks}; baseline {arguments.baseline}; "
        f"nodes naming no file: {new_unnamed:,} new, {base_unnamed:,} baseline"
    )
    for line in lines:
        print(line)
    return BELOW_FLOOR if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
