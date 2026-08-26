"""What the last build measured, so this build can say what moved.

Every count this pipeline prints - join cardinality, layer sizes, indexed rows -
lived only in one build's scrollback, so nothing could notice a number moving.
Each silent failure found across two estates was a plausible number in isolation
and an implausible one next to its predecessor: a file-to-ticket join reported as
a healthy-looking fraction of the graph that should have been three times larger,
a corpus inventory that collapsed to a fraction of its file count, a summary layer
that lost most of its attributions after a re-cluster. All three were printed by a
green build. None was compared with anything.

This is the witness rule applied to the pipeline's own telemetry: a stage records
what it measured to a small committed artefact, and the *next* run of that stage
compares its fresh number against the recorded one before overwriting it. The
comparison is against the store's own history, which is what makes it possible at
all - two estates measured AST-to-semantic node ratios of roughly 0.5:1 and 57:1,
a factor of a hundred, so no constant this library could ship would mean anything
on both. There is deliberately no threshold here.

**Integers only, and that is a rule rather than a simplification.** A rate
recorded as `17.6` cannot be checked against anything: the population it was a
percentage of is gone, so the arithmetic cannot be re-derived and a later build
cannot tell a shrinking numerator from a growing denominator. Record the numerator
and the denominator, and the rate is recoverable for both builds; record the rate,
and the counts are not. Every wrong measurement this codebase has shipped was
correct code answering a neighbouring question, and a stored rate is how that
happens by construction.

**Reporting, not failing.** A legitimate estate change moves every one of these
numbers - an intentional content cut is *supposed* to shrink the AST layer - and a
gate that fires on every intentional change gets suppressed, which is worse than
no gate because somebody has then explicitly decided to ignore it. The single
exception is the one condition that is unambiguous without knowing anything about
the estate: a measurement that was non-zero and is now zero. That is the floor
`io.report_join_cardinality` asserts for the join, generalised to any recorded
measurement by the existence of a predecessor to compare against.

The record holds the last value recorded for each metric, not a history. Which
build a value came from is a question for `git log knowledge/telemetry.json` - the
artefact is committed precisely so the diff is reviewable, and so no wall-clock
timestamp is needed in it. Nothing here reads a clock: two runs over the same
inputs write byte-identical bytes.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import config, io


# `stage.measurement`, lower case. Namespaced because stages record into one
# shared document: an unqualified `nodes` from two stages is one key, and the
# second build of the day would silently compare the explorer's node count
# against the layer merge's. The pattern also keeps the committed file diffable
# by making the key order meaningful rather than incidental.
METRIC = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


@dataclass(frozen=True)
class Movement:
    """One measurement, beside what was recorded for it last time."""

    metric: str
    previous: int | None
    current: int

    @property
    def first(self) -> bool:
        """Nothing was recorded for this metric before, so nothing moved."""
        return self.previous is None

    @property
    def collapsed(self) -> bool:
        """Was non-zero when last recorded and is zero now.

        The only condition worth treating as a defect without knowing the estate.
        A build may legitimately produce fewer nodes, fewer rows or a smaller
        page; a measurement that had a population and now has none is the shape
        of a dead join, an unread corpus or a layer that failed to load.
        """
        return bool(self.previous) and self.current == 0

    def describe(self) -> str:
        """One line naming the previous value, not only the change.

        The previous value is the point: `12,732 (-88.0%)` still leaves the
        reader working out what it fell from, and a number nobody can restate is
        one nobody compares.
        """
        if self.previous is None:
            return f"{self.metric}: {self.current:,} (first recorded)"
        if self.previous == self.current:
            return f"{self.metric}: {self.current:,} (unchanged)"
        if self.previous == 0:
            return f"{self.metric}: 0 -> {self.current:,} (was zero)"
        change = (self.current - self.previous) / self.previous
        return f"{self.metric}: {self.previous:,} -> {self.current:,} ({change:+.1%})"


def _validate(measurements: Mapping[str, int]) -> None:
    """Refuse a record that cannot be compared later.

    Both refusals are about the *next* build, which is why they are errors rather
    than warnings: a rate or a stray key is written once and read for as long as
    the store exists.
    """
    for metric, value in measurements.items():
        if not METRIC.match(metric):
            raise ValueError(
                f"telemetry metric {metric!r} must be named `stage.measurement` in lower "
                "case: stages record into one shared document, and an unqualified name "
                "collides with another stage's."
            )
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"telemetry metric {metric!r} must be a whole count, not {value!r}. Record "
                "a rate as its numerator and its denominator - a stored rate cannot be "
                "re-derived, so a later build cannot tell a shrinking numerator from a "
                "growing denominator."
            )


def read() -> dict[str, int]:
    """The last recorded value of every metric, or {} when nothing is recorded.

    Non-integer values are dropped rather than trusted: the artefact is committed,
    so it gets hand-edited and merge-resolved, and a string where a count belongs
    would otherwise reach the arithmetic below.
    """
    document = io.read_json_dict(config.TELEMETRY_PATH)
    measurements = document.get("measurements")
    if not isinstance(measurements, dict):
        return {}
    return {
        metric: value
        for metric, value in measurements.items()
        if isinstance(metric, str) and isinstance(value, int) and not isinstance(value, bool)
    }


def movements(previous: Mapping[str, int], current: Mapping[str, int]) -> list[Movement]:
    """Each current measurement beside its recorded predecessor, name-ordered.

    Sorted by metric name rather than by size or by the caller's dict order: two
    runs over the same inputs must produce the same report, and a caller building
    its dict from a set would otherwise reorder the output per process.
    """
    return [Movement(metric, previous.get(metric), current[metric]) for metric in sorted(current)]


def display_path() -> str:
    path = config.TELEMETRY_PATH
    try:
        return str(Path(path).relative_to(config.ROOT))
    except ValueError:
        return str(path)


def report(moved: list[Movement], out=None, err=None) -> None:
    """Print what moved: statistics to stdout, a collapse to stderr.

    The split follows the join report's: a measurement is a statistic and a
    population that has gone to zero is a defect, and the two are different kinds
    of output rather than the same output with different wording.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    if not moved:
        return
    if all(movement.first for movement in moved):
        print(
            f"Telemetry: first record in {display_path()} - nothing to compare against yet, "
            "so this build becomes the baseline the next one is measured against:",
            file=out,
        )
    else:
        print(f"Telemetry, against the last record in {display_path()}:", file=out)
    for movement in moved:
        if movement.collapsed:
            print(
                f"WARNING: {movement.metric} fell to zero: {movement.previous:,} -> 0. It was "
                "non-zero when last recorded, so this is a break rather than a thin estate. "
                "Nothing here guesses at how much is enough, but a population that had "
                "members and now has none is not a judgement call.",
                file=err,
            )
        else:
            print(f"  {movement.describe()}", file=out)


def record(measurements: Mapping[str, int], out=None, err=None) -> list[Movement]:
    """Compare against the record, say what moved, then become the record.

    Reads before it writes, which is the whole mechanism: the comparison is
    against a value written by an earlier build rather than against anything this
    one computed. Metrics other stages recorded are carried forward untouched -
    the explorer must not erase what the layer merge measured, or each stage's
    record would only ever survive until the next stage ran.

    Returns the movements so a caller can act on them. Nothing in this library
    does yet, and that is the issue's answer to *fail or report*: an estate
    change moves these numbers legitimately.
    """
    _validate(measurements)
    previous = read()
    moved = movements(previous, measurements)
    report(moved, out=out, err=err)
    io.write_json(
        config.TELEMETRY_PATH,
        {"measurements": dict(sorted({**previous, **measurements}.items()))},
        indent=1,
    )
    return moved


def recorded_lines() -> list[str]:
    """The record as aligned lines, for a reporting stage to print.

    Deliberately without a comparison: `status` measures none of these itself, and
    a report that printed a fresh number beside a recorded one it had not measured
    would be claiming a comparison it never made.
    """
    recorded = read()
    if not recorded:
        return []
    width = max(len(metric) for metric in recorded)
    return [f"  {metric:<{width}}  {value:>12,}" for metric, value in sorted(recorded.items())]
