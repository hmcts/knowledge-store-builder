"""Fan-out progress read from the artefacts, not from the dispatch log (#131).

    knowledgestore chunk-status [--dispatched LOG ...]

The semantic fan-out is the one stage where the pipeline hands work to a crowd of
agents and has to work out afterwards what came back. Nothing here reported that,
so every operator running the fan-out wrote their own tally - and the tally is the
part that goes wrong.

**A dispatch log is a cache of intent. Disk is the only ground truth.** The
reported cost of forgetting that: a coverage gap of ninety-odd chunks announced by
diffing the plan against a dispatch log without intersecting disk, and a redundant
round of a dozen agents launched to fill a gap that did not exist. The log did not
cover the early rounds; the extractions were on disk the whole time. So `done`
here is derived from files and from nothing else, and a log can only ever *split*
the outstanding set - it can never make a chunk done.

**"No output on disk" has two causes, and separating them is the whole point.**
The concurrent-agent ceiling *rejects* the excess rather than queuing it, so a
chunk can be recorded as dispatched and never launched, and it will then sit there
forever because nothing will ever produce it. An agent still working looks
identical. Reported: a run of consecutive chunks rejected in an early round sat
unnoticed behind ninety higher-numbered ids for a long time, because dispatch was
plan-ordered. Never-sent is therefore reported *first*, ahead of in-flight and
ahead of the totals.

**A log token that matches no chunk is reported, never counted.** One operator
appended fifty-odd batch files to a log with `cat`, none of which ended in a
newline, so every append fused the last id of one file onto the first of the next.
The fused tokens matched no chunk, counted as dispatched-but-absent, and for
several rounds inflated `in flight` and deflated `NEVER SENT` - with totals that
stayed plausible throughout. A status tool that launders a corrupt log into a
confident number is worse than no tool, because it is trusted. So every token is
validated against the plan, anything unrecognised is named, and a token that looks
like several ids run together is diagnosed as such.

**This stage derives its target from the plan and from nothing else**, because the
plan is the only map from chunk number to file list. Without it there is no
denominator and no way to name what is missing, so an absent plan is refused
rather than guessed around.

Exit status is 0 whenever the question could be answered: an incomplete fan-out is
the normal state of a fan-out in progress, and this stage reports so humans decide.
It returns 2 only when it cannot answer at all - no plan.

None of this applies to a code-only corpus. graphify's semantic pass has a fast
path that writes an empty layer for a corpus with no documents, papers or images,
so such an estate produces no chunks and never reaches the fan-out at all.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import config, io

CHUNK_GLOB = ".graphify_chunk_*.json"


def chunk_id(name: str) -> str:
    """The chunk a file or log token names.

    Filenames, paths and bare ids all reduce to the same thing, so a log written as
    filenames is read as well as one written as ids.
    """
    return Path(name).name.removeprefix(".graphify_chunk_").removesuffix(".json")


def extractions_on_disk(directory: Path) -> tuple[set[str], list[str]]:
    """(chunk ids holding a usable extraction, filenames present but unusable).

    An unusable file is not progress and must not be counted as any. The fan-out's
    own failure modes produce exactly this: an agent killed mid-write leaves
    truncated JSON, and an agent that hit the output limit leaves nothing or a
    fragment. Both look like *something is there*, which is how a broken artefact
    becomes a confident number.

    The decode error is caught here rather than left to the reader because
    `io.read_json_dict` raises on malformed JSON - correct for a stage that cannot
    proceed, wrong for one whose job is to describe the mess.
    """
    done: set[str] = set()
    unusable: list[str] = []
    for path in sorted(directory.glob(CHUNK_GLOB)):
        if path.name.endswith("_plan.json"):
            continue
        try:
            payload = io.read_json_dict(path)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            unusable.append(path.name)
            continue
        if "nodes" not in payload:
            unusable.append(path.name)
            continue
        done.add(chunk_id(path.name))
    return done, unusable


def log_tokens(paths: list[Path]) -> list[str]:
    """Every whitespace-separated token in the dispatch logs, in order.

    Split on whitespace deliberately: the corruption this stage exists to notice is
    ids fused together with no separator at all, and a reader that resynchronised on
    id width would silently repair it instead of reporting it.
    """
    tokens: list[str] = []
    for path in paths:
        for raw in path.read_text(encoding="utf-8").split():
            token = raw.strip(",").strip()
            if token:
                tokens.append(token)
    return tokens


def recognised(tokens: list[str], plan_ids: set[str]) -> tuple[set[str], list[str]]:
    """(chunk ids the log actually names, tokens matching no chunk).

    Validated against the plan rather than against a pattern: a token can be four
    digits and still name no chunk, which is precisely the fused-id case.
    """
    dispatched: set[str] = set()
    unrecognised: list[str] = []
    for token in tokens:
        candidate = chunk_id(token)
        if candidate in plan_ids:
            dispatched.add(candidate)
        else:
            unrecognised.append(token)
    return dispatched, unrecognised


def fused_ids(token: str, plan_ids: set[str]) -> list[str]:
    """The chunk ids an unrecognised token appears to be a concatenation of.

    A diagnosis, never a repair. Naming the shape is what turns several rounds of
    implausible-but-plausible numbers into a one-line fix; silently splitting the
    token would hide the corrupt log that produced it.
    """
    widths = {len(identifier) for identifier in plan_ids}
    if len(widths) != 1:
        return []
    width = widths.pop()
    if len(token) <= width or len(token) % width:
        return []
    parts = [token[start : start + width] for start in range(0, len(token), width)]
    return parts if all(part in plan_ids for part in parts) else []


def classify(
    plan_ids: set[str], on_disk: set[str], dispatched: set[str]
) -> tuple[list[str], list[str], list[str], list[str]]:
    """(done, never sent, in flight, output for chunks the plan does not name).

    `done` intersects the plan with disk and consults no log. That is the whole
    correction: a chunk absent from the log but present on disk is done, and a chunk
    named by the log with no file is not.
    """
    done = sorted(plan_ids & on_disk)
    outstanding = plan_ids - on_disk
    never_sent = sorted(outstanding - dispatched)
    in_flight = sorted(outstanding & dispatched)
    unplanned = sorted(on_disk - plan_ids)
    return done, never_sent, in_flight, unplanned


def _listing(identifiers: list[str], limit: int = 20) -> str:
    shown = ", ".join(identifiers[:limit])
    if len(identifiers) > limit:
        return f"{shown}, ... (+{len(identifiers) - limit:,} more)"
    return shown


def _outstanding_lines(never_sent: list[str], in_flight: list[str], had_log: bool) -> list[str]:
    """The head of the report: never-sent first, whenever it can be known.

    Ordered first rather than by severity, because dispatch is plan-ordered and a
    rejected low-numbered chunk hides behind every higher-numbered one that came
    after it.

    **Without a log the split cannot be made, and must not be asserted.** Every
    outstanding chunk falls out of `classify` as never-sent when nothing was
    dispatched, and printing that as a finding tells an operator to redispatch work
    that is in progress - the opposite error to the one this stage exists to fix, and
    just as expensive.
    """
    if not had_log:
        outstanding = sorted(never_sent + in_flight)
        lines = [
            f"NEVER SENT  unknown: no --dispatched log given, so the {len(outstanding):,} "
            "chunk(s) without output cannot be split into never-launched and still-working. "
            "Pass the log you dispatched from."
        ]
        if outstanding:
            lines.append(
                f"no output   {len(outstanding):,} planned chunk(s), status unknown\n"
                f"  {_listing(outstanding)}"
            )
        return lines

    if never_sent:
        lines = [
            f"NEVER SENT  {len(never_sent):,} planned chunk(s) with no output and no dispatch "
            f"record.\n  {_listing(never_sent)}\n"
            "  Nothing will ever produce these: the concurrent-agent ceiling rejects rather "
            "than queues, so a rejected chunk waits forever. Dispatch them."
        ]
    else:
        lines = ["NEVER SENT  none: every outstanding chunk appears in a dispatch log."]
    if in_flight:
        lines.append(
            f"in flight   {len(in_flight):,} dispatched, no output yet\n  {_listing(in_flight)}"
        )
    return lines


def report(
    plan_ids: set[str],
    done: list[str],
    never_sent: list[str],
    in_flight: list[str],
    unplanned: list[str],
    unusable: list[str],
    unrecognised: list[str],
    had_log: bool,
) -> None:
    """Every number names the quantity it counts, and never-sent comes first."""
    lines = _outstanding_lines(never_sent, in_flight, had_log)
    lines.append(f"done        {len(done):,} of {len(plan_ids):,} planned chunk(s), read from disk")

    if unusable:
        lines.append(
            f"UNUSABLE    {len(unusable):,} chunk file(s) present but not readable as an "
            f"extraction, starting with {unusable[0]}. Not counted as done - a truncated file "
            "is what an agent killed mid-write leaves behind, and counting it hides the chunk "
            "that has to be redone."
        )
    if unplanned:
        lines.append(
            f"UNPLANNED   {len(unplanned):,} chunk file(s) whose id the plan does not name, "
            f"starting with .graphify_chunk_{unplanned[0]}.json. Either the plan was "
            "rewritten since they were "
            "extracted - chunk numbering is the archive's only index and it moves with "
            "--chunk-size - or they came from another corpus."
        )
    if unrecognised:
        lines.extend(_corrupt_log_lines(unrecognised, plan_ids))
    print("\n".join(lines), flush=True)


def _corrupt_log_lines(unrecognised: list[str], plan_ids: set[str]) -> list[str]:
    """What a log token matching no chunk means, and the shape it usually has."""
    lines = [
        f"CORRUPT LOG {len(unrecognised):,} dispatch-log token(s) match no chunk in the plan, "
        f"starting with {unrecognised[0]}. Reported rather than counted: counted, they inflate "
        "`in flight` and deflate `NEVER SENT` while every total stays plausible."
    ]
    joined = [(token, fused_ids(token, plan_ids)) for token in unrecognised]
    joined = [(token, parts) for token, parts in joined if parts]
    if joined:
        token, parts = joined[0]
        lines.append(
            f"            {len(joined):,} of them look like ids run together - {token} is "
            f"{' + '.join(parts)}. That is what appending batch files whose last line carries "
            "no newline produces. Fix the log rather than letting a reader guess."
        )
    return lines


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore chunk-status",
        description="Report semantic fan-out progress from the extractions on disk, "
        "separating chunks that were never launched from chunks still being worked on.",
    )
    parser.add_argument(
        "--dispatched",
        type=Path,
        action="extend",
        nargs="+",
        default=None,
        metavar="LOG",
        help="dispatch logs of chunk ids or chunk filenames; repeatable, and several may "
        "follow one flag. Without one, chunks with no output cannot be split into "
        "never-launched and in-flight - and never-launched is the one nothing will ever fix",
    )
    parser.add_argument(
        "--chunks",
        type=Path,
        help="directory holding .graphify_chunk_*.json (default: graphify-out/)",
    )
    parser.add_argument("--plan", type=Path, help=f"default: {config.CHUNK_PLAN_PATH.name}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    directory = arguments.chunks or (config.ROOT / "graphify-out")
    plan_path = arguments.plan or config.CHUNK_PLAN_PATH

    plan = io.read_json_dict(plan_path) if plan_path.is_file() else {}
    if not plan:
        print(
            f"No chunk plan at {plan_path}. Run `knowledgestore chunk-plan` first.\n"
            "  Refused rather than estimated: the plan is the only map from chunk number to "
            "file list, so without it there is no denominator and nothing to name as missing. "
            "A progress figure derived from a dispatch log alone is a guess.",
            flush=True,
        )
        return 2

    requested = arguments.dispatched or []
    logs = [path for path in requested if path.is_file()]
    absent = [path for path in requested if not path.is_file()]
    if absent:
        print(
            f"  WARNING: {len(absent)} dispatch log(s) do not exist, starting with {absent[0]}. "
            "An absent log reads as 'nothing was dispatched', which reports every outstanding "
            "chunk as NEVER SENT.",
            flush=True,
        )

    plan_ids = set(plan)
    on_disk, unusable = extractions_on_disk(directory)
    tokens = log_tokens(logs)
    if logs:
        # Named and counted, because the alternative is a silent parse. A log read as
        # zero tokens is indistinguishable in the report from a fan-out where nothing
        # was dispatched, and it reports every outstanding chunk as NEVER SENT.
        print(
            f"  read {len(tokens):,} dispatch token(s) from "
            + ", ".join(path.name for path in logs)
            + ("" if tokens else " - NONE of them held a token, so nothing reads as dispatched"),
            flush=True,
        )
    dispatched, unrecognised = recognised(tokens, plan_ids)
    done, never_sent, in_flight, unplanned = classify(plan_ids, on_disk, dispatched)

    report(
        plan_ids,
        done,
        never_sent,
        in_flight,
        unplanned,
        unusable,
        unrecognised,
        had_log=bool(logs),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
