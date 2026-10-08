"""Write the semantic fan-out's chunk plan, so it is a file rather than a memory.

graphify's skill says only *"split into chunks of 20-25 files each"* and leaves it
to the dispatching agent. Nothing writes the split down.

**The consequence is worse than every store inventing its own plan.** A plan
produced in an agent's context cannot be characterised, audited or reproduced at
all. Measured on the one estate that has such a file: eight scripts read it and
**none writes it** - it was partitioned in-session during a build, and its operator
could not regenerate it if the file were lost, which would leave the archive it
indexes permanently unaddressable.

That has a second cost, subtler and already paid. Its operator described the plan to
me as grouping by directory and refusing to mix; measured, 47% of its chunks span
multiple directories. Neither of us could have known: there was no code to read. An
ad-hoc partition is not merely unrepeatable, it is **unfalsifiable** - any claim
about it, including its own author's, is a guess.

Two things follow from the plan being ad-hoc, and the second is the reason this
stage exists at all:

**The committed chunk archive is unreadable without it.** The plan is the only map
from chunk number to file list. An archive of extraction results whose inputs
cannot be named is evidence of nothing.

**It stored absolute paths.** On a doc-heavy estate that is ~17,500 of them,
tracked, so a clone receives one build machine's directory layout - and relocating
the working directory rewrote every entry, twice in one day.

    knowledgestore chunk-plan          -> graphify-out/.graphify_chunk_plan.json

Written **relative at rest and resolved to absolute at dispatch**, via
`store_paths`. That split matters and is not tidiness: the extraction spec
requires agents to receive and echo paths *verbatim and absolute*, because
`source_file` conformance depends on it. So `store_paths.load_plan()` hands a
dispatcher absolute paths from a file that commits none.

## What the split is for

Chunk boundaries are not arbitrary. Files from one directory are extracted
together because cross-file relationships are what the semantic layer exists to
find, and an agent cannot relate two files it never saw together. Images get a
chunk each, because vision needs its own context.

Neither choice is free, and the plan records the sizes so the trade is visible
rather than assumed.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import statistics
import sys
from collections import defaultdict
from collections.abc import Container, Iterable
from pathlib import Path
from typing import TextIO

from . import config, content_set, drift, io, store_paths

# graphify's own detect categories. Video is transcribed to a document before this
# runs, so it is never planned directly.
KNOWN_KINDS = ("code", "document", "paper", "image")

# The default: prose and diagrams, because code is the AST layer's job on most
# estates and semantically re-extracting it would pay twice for the same nodes.
#
# **On an infrastructure estate this default covers a quarter of the corpus.**
# graphify classifies YAML and Terraform as `code`, so on one real estate the
# semantically interesting content - Flux Kustomizations, Helm values,
# `variables.tf` - is 12,888 files this default excludes against 3,857 it includes:
# 4,651 of 17,539 planned paths, 27%. That estate's fan-out extracted from the code
# files deliberately, because an AST pass over a Kustomization tells you the shape
# of the YAML and nothing about which environment it deploys.
#
# So `kinds` is an operator choice with a default, not a fixed set.
CONTENT_KINDS = ("document", "paper", "image")

# The skill says 20-25. 22 sits in the middle; the flag exists because the right
# number depends on how large an estate's documents are, which the library cannot
# know.
DEFAULT_CHUNK_SIZE = 22


def content_files(detect: dict, kinds: tuple[str, ...] = CONTENT_KINDS) -> dict[str, list[str]]:
    """Detected files per content kind, sorted so a plan is reproducible."""
    files = detect.get("files") or {}
    return {kind: sorted(files.get(kind) or []) for kind in kinds}


def group_by_directory(paths: list[str]) -> list[list[str]]:
    """Files grouped by parent directory, directories in sorted order.

    The grouping is the point: an agent relates files it sees together, so keeping
    a directory intact is what produces cross-file edges rather than a chunk
    boundary running through the middle of a subsystem.
    """
    by_directory: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        by_directory[str(Path(path).parent)].append(path)
    return [sorted(by_directory[key]) for key in sorted(by_directory)]


def detected_images(detect: dict) -> list[str]:
    """The files the detect result classifies as images, as it wrote them."""
    return list((detect.get("files") or {}).get("image") or [])


def image_chunks(files: Iterable[str], images: Container[str]) -> tuple[list[list[str]], list[str]]:
    """(a chunk of its own for each image, sorted; every other file, in the order given).

    The one place a plan decides which files are extracted alone. A full plan and a
    delta both call it, so a change to the rule reaches both together: an image
    gets its own chunk because vision needs its own context, and mixing images with
    documents makes an agent do two jobs in one prompt.
    """
    listed = list(files)
    own = [[path] for path in sorted(path for path in listed if path in images)]
    return own, [path for path in listed if path not in images]


def plan_chunks(
    detect: dict,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    only: set[str] | None = None,
    kinds: tuple[str, ...] = CONTENT_KINDS,
) -> dict[str, list[str]]:
    """The chunk plan: chunk id -> file list, absolute as detected.

    `only` restricts the plan to a set of paths - the uncached ones - without
    changing how the rest is grouped.

    A directory larger than `chunk_size` is split across consecutive chunks rather
    than given an oversized one: an over-long FILE_LIST is what pushes an agent
    into the output limit that destroys its whole batch (#131).
    """
    files = content_files(detect, kinds)
    if only is not None:
        files = {kind: [f for f in paths if f in only] for kind, paths in files.items()}

    images = set(files.get("image", []))
    chunks, grouped = image_chunks((path for paths in files.values() for path in paths), images)
    chunks.extend(chunk_groups(group_by_directory(grouped), chunk_size))
    return {f"{index + 1:04d}": chunk for index, chunk in enumerate(chunks)}


def chunk_groups(groups: list[list[str]], chunk_size: int) -> list[list[str]]:
    """One directory per chunk, split when a directory exceeds `chunk_size`.

    **`chunk_size` is a maximum, not a target, and chunks are never mixed.** An
    earlier version closed a chunk at a directory boundary only once it held at
    least half the target, which let a *small* directory pull the next one in - the
    opposite of the intent. On a realistic estate of twelve three-file directories
    at the suggested size of 22, every chunk mixed four directories.

    The skill asks for both "20-25 files each" and "group files from the same
    directory together", which cannot both hold. Grouping wins here, because
    cross-file relationships are the reason the semantic layer exists and padding a
    chunk with unrelated files asks an agent to relate things that have no relation.

    **That is this library's choice, and there is no convention to follow.** An
    earlier version of this docstring cited the one existing chunk plan as
    directory-first grouping. It is not - 47% of its chunks span multiple directories
    - and more to the point it is not an algorithm at all: nothing generates it, so
    it resolves the skill's conflict neither way. It is one agent's partition. The
    reasoning above therefore stands on its own, with no observed practice behind it,
    and that is the honest state of the evidence.

    **The cost of strictness is real and falls on deep, thin trees.** On an estate of
    12,888 code files spread across deep directories, one-directory-per-chunk yields
    a mean of 3.3 files per chunk against that estate's ad-hoc 22 - roughly nine
    times the agent dispatches. Whether some middle ground is better (merging sibling
    directories under a common parent, say) is unmeasured, and inventing a heuristic
    here is what produced the mixing defect this function was just corrected for. So
    the cost is documented rather than optimised away.
    """
    chunks: list[list[str]] = []
    for group in groups:
        for start in range(0, len(group), chunk_size):
            chunks.append(group[start : start + chunk_size])
    return chunks


def requested_kinds(raw: str) -> tuple[str, ...] | None:
    """The kinds asked for, or None when the request cannot be honoured.

    A misspelled kind is refused rather than ignored: planning nothing for
    `documnet` looks exactly like an estate with no documents, which is the wrong
    answer to a typo.
    """
    kinds = tuple(k.strip() for k in raw.split(",") if k.strip())
    unknown = [k for k in kinds if k not in KNOWN_KINDS]
    if unknown or not kinds:
        print(
            f"--kinds must name detect categories from {', '.join(KNOWN_KINDS)}"
            + (f"; not {', '.join(unknown)}" if unknown else ""),
            flush=True,
        )
        return None
    return kinds


def uncached_paths() -> set[str]:
    """The paths graphify's cache check left to extract."""
    if not config.UNCACHED_PATH.is_file():
        return set()
    text = config.UNCACHED_PATH.read_text(encoding="utf-8")
    return {line.strip() for line in text.splitlines() if line.strip()}


def report(
    plan: dict[str, list[str]],
    counted: dict[str, list[str]],
    chunk_size: int,
    destination: str,
    stream: TextIO,
) -> None:
    """What was planned, and from what. Every number names the quantity it counts."""
    sizes = sorted(len(files) for files in plan.values())
    print(
        f"{len(plan):,} chunks over {sum(sizes):,} files -> {destination}\n"
        f"  per chunk: smallest {sizes[0]}, largest {sizes[-1]}, maximum {chunk_size}\n"
        "  detected: " + ", ".join(f"{kind} {len(paths):,}" for kind, paths in counted.items()),
        file=stream,
        flush=True,
    )


def delta_files(path: Path) -> list[str]:
    """What a resync has to extract: the drift's new and changed files, store-relative.

    Deleted files are not planned - there is nothing left to read.
    """
    document = io.read_json_dict(path)
    files = [*(document.get("new") or []), *(document.get("changed") or [])]
    return sorted({store_paths.relative(f) for f in files})


def next_chunk_number(plan_path: Path) -> int | None:
    """The number after the plan's last chunk, or None when there is no plan to extend."""
    plan = io.read_json_dict(plan_path)
    numbers = [int(key) for key in plan if str(key).isdigit()]
    return max(numbers) + 1 if numbers else None


def pack_delta(
    files: list[str], chunk_size: int, first: int, images: set[str] | frozenset[str] = frozenset()
) -> dict[str, list[str]]:
    """A delta's chunks: one repository at a time, in directory order, filled to `chunk_size`.

    Images first, one chunk each, as the full plan does: vision needs its own context,
    and a delta must extract what a full build would. `images` comes from the same
    detect result the full plan reads.

    **Not the full plan's one-directory rule, deliberately.** A delta touches a file or
    two per directory, so one directory per chunk pays a worker's fixed cost once per
    directory: measured on one large internal estate, 1,130 chunks for 2,048 files,
    against 192 packed this way. Keeping a repository's files together, in directory
    order, keeps the relationships an agent can find; padding across repositories would
    ask it to relate files that have no relation.
    """
    alone, rest = image_chunks(files, images)
    by_repository: dict[str, list[str]] = defaultdict(list)
    for path in rest:
        by_repository[drift.repository_of(path)].append(path)
    packed = list(alone)
    for repository in sorted(by_repository):
        ordered = sorted(by_repository[repository], key=lambda p: (posixpath.dirname(p), p))
        packed.extend(
            ordered[start : start + chunk_size] for start in range(0, len(ordered), chunk_size)
        )
    return {f"{first + index:04d}": chunk for index, chunk in enumerate(packed)}


def write_batches(plan: dict[str, list[str]], directory: Path, chunk_out: Path) -> int:
    """One batch file per chunk, in the shape the extraction agents and the chunk gate take.

    One chunk per batch because a worker extracting the last of several chunks carries
    every earlier chunk's reads and writes in its context, and its cost per turn grows
    with each. Files are absolute, as the extraction spec requires an agent to receive
    and echo them; `out` is where the chunk's extraction goes, which a trial points away
    from the live directory so nothing live is touched.
    """
    for key in sorted(plan, key=int):
        batch = {
            "chunks": [
                {
                    "n": int(key),
                    "out": str(chunk_out / f".graphify_chunk_{key}.json"),
                    "files": [store_paths.absolute(f) for f in plan[key]],
                }
            ]
        }
        io.write_json(directory / f"batch_{int(key):04d}.json", batch, indent=1)
    return len(plan)


def emit(stored: dict[str, list[str]], out: str | None) -> str:
    """Write a plan to `out`, or to stdout when none is named; say where it went."""
    if out is None:
        print(json.dumps(stored, indent=2, ensure_ascii=False), flush=True)
        return "stdout"
    io.write_json(Path(out), stored, indent=2)
    return out


def plan_delta(arguments: argparse.Namespace) -> int:
    """`--delta`: the resync's chunks, numbered after the plan's, written beside it."""
    stream = sys.stdout if arguments.out else sys.stderr
    delta_path = Path(arguments.delta)
    if not delta_path.is_file():
        print(f"No drift at {delta_path}; `knowledgestore drift` writes one.", file=stream)
        return 2
    first = next_chunk_number(config.CHUNK_PLAN_PATH)
    if first is None:
        # Restarting at 0001 would collide with chunk files already on disk.
        print(
            f"No chunk plan at {config.CHUNK_PLAN_PATH} to number the delta after. A delta "
            "extends the plan the committed layer was extracted from.",
            file=stream,
            flush=True,
        )
        return 2
    detect = io.read_json_dict(config.DETECT_PATH)
    if not detect:
        # Read as "no images", a missing detect result would pack every image in with
        # documents, which no full build does.
        print(
            f"A delta needs the detection results at {config.DETECT_PATH} for the reason "
            "the full plan does - to give each image its own chunk - and there are none. "
            + content_set.DETECT_PRODUCER,
            file=stream,
            flush=True,
        )
        return 2
    files = delta_files(delta_path)
    if not files:
        # Not a failure, and not an empty mapping either: a resync would append that
        # as a plan that succeeded.
        print(
            f"The drift at {delta_path} holds no new or changed files, so there is nothing "
            "to extract. Nothing written.",
            file=stream,
            flush=True,
        )
        return 0

    images = {store_paths.relative(f) for f in detected_images(detect)}
    additions = pack_delta(files, arguments.chunk_size, first, images)
    placed = [f for chunk in additions.values() for f in chunk]
    if sorted(placed) != files:
        # Every file in exactly one chunk: a file in two is extracted twice and
        # merged as duplicates; a file in none is silently never re-extracted.
        print(
            f"Refusing: {len(placed):,} placements for {len(files):,} files - the delta "
            "would extract a file twice or not at all. Nothing written.",
            file=stream,
            flush=True,
        )
        return 2

    # Written beside the plan, never into it: the resync appends it once the chunks
    # are extracted, so the plan never names an extraction that does not exist.
    destination = emit(store_paths.store_relative_plan(additions), arguments.out)
    sizes = [len(chunk) for chunk in additions.values()]
    keys = list(additions)
    print(
        f"{len(files):,} new and changed files -> {len(additions):,} chunks "
        f"{keys[0]}..{keys[-1]} -> {destination}\n"
        f"  per chunk: median {statistics.median(sizes):g}, largest {max(sizes)}, "
        f"maximum {arguments.chunk_size}\n"
        "  Append these to the plan once the chunks are extracted.",
        file=stream,
        flush=True,
    )
    if arguments.one_per_batch:
        batches(additions, arguments, stream)
    return 0


def batches(plan: dict[str, list[str]], arguments: argparse.Namespace, stream: TextIO) -> None:
    directory = Path(arguments.one_per_batch)
    chunk_out = Path(arguments.chunk_out) if arguments.chunk_out else config.CHUNK_PLAN_PATH.parent
    written = write_batches(plan, directory, chunk_out)
    print(
        f"  {written:,} one-chunk batch files -> {directory}, extractions to {chunk_out}",
        file=stream,
        flush=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore chunk-plan",
        description="Write the semantic fan-out's chunk plan for the dispatching agent.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="MAXIMUM files per chunk (default 22). Chunks hold one directory each and are "
        "not padded, so most will be smaller - on the one real estate that runs this, half "
        "of them hold fewer than 20",
    )
    parser.add_argument(
        "--kinds",
        default=",".join(CONTENT_KINDS),
        help="comma-separated detect categories to plan, from "
        + "/".join(KNOWN_KINDS)
        + f" (default {','.join(CONTENT_KINDS)}). An infrastructure estate should add "
        "`code` deliberately: graphify classifies YAML and Terraform there, and on one "
        "such estate the default covers 27 per cent of the corpus",
    )
    parser.add_argument(
        "--uncached",
        action="store_true",
        help="plan only files graphify's cache has not seen - not the files a sync changed, "
        "which `knowledgestore drift` measures. Written to --out or stdout, never over the "
        "committed plan",
    )
    parser.add_argument(
        "--out",
        help="where to write the plan (default: graphify-out/.graphify_chunk_plan.json, or "
        "stdout for --uncached and --delta)",
    )
    parser.add_argument(
        "--delta",
        help="plan the new and changed files in a `knowledgestore drift` output, packed per "
        "repository and numbered after the committed plan's last chunk. The mapping goes to "
        "--out or stdout, for appending to the plan once the chunks are extracted",
    )
    parser.add_argument(
        "--one-per-batch",
        metavar="DIR",
        help="also write one batch file per chunk to DIR, in the shape "
        '{"chunks": [{"n", "out", "files"}]} with absolute file paths',
    )
    parser.add_argument(
        "--chunk-out",
        metavar="DIR",
        help="where each batch's `out` points (default: graphify-out/). Point a trial "
        "elsewhere so nothing live is touched",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    if arguments.chunk_size < 1:
        print("--chunk-size must be at least 1", flush=True)
        return 2

    if arguments.delta and arguments.uncached:
        print("--delta and --uncached answer different questions; pass one.", flush=True)
        return 2
    if arguments.delta:
        return plan_delta(arguments)

    kinds = requested_kinds(arguments.kinds)
    if kinds is None:
        return 2

    detect = io.read_json_dict(config.DETECT_PATH)
    if not detect:
        print(
            f"No detection results at {config.DETECT_PATH}. {content_set.DETECT_PRODUCER} "
            "A plan invented without it would name files nobody has confirmed are there.",
            flush=True,
        )
        return 2

    only = uncached_paths() if arguments.uncached else None

    plan = plan_chunks(detect, arguments.chunk_size, only, kinds)
    counted = content_files(detect, kinds)
    if not plan:
        # Not a failure: a code-only estate never runs the fan-out at all, and
        # saying so is more useful than writing an empty file it will not read.
        print(
            f"No {' or '.join(kinds)} files detected, so there is nothing to split. "
            "Nothing written. If this estate's content is YAML or Terraform, graphify "
            "classifies that as `code` - pass --kinds code,document to include it.",
            flush=True,
        )
        return 0

    return write_full_plan(plan, counted, only, arguments)


def write_full_plan(
    plan: dict[str, list[str]],
    counted: dict[str, list[str]],
    only: set[str] | None,
    arguments: argparse.Namespace,
) -> int:
    """Write a full or uncached plan, its batches if asked, and say what was written."""
    stored = store_paths.store_relative_plan(plan)
    # A measurement must not rewrite the artefact the chunk archive is keyed on, so
    # an uncached plan goes where the caller says, or to stdout - never over the
    # committed plan.
    to_stdout = arguments.out is None and arguments.uncached
    stream = sys.stderr if to_stdout else sys.stdout
    if to_stdout:
        destination = emit(stored, None)
    else:
        destination = emit(stored, arguments.out or str(config.CHUNK_PLAN_PATH))
    sizes = sorted(len(files) for files in plan.values())
    report(plan, counted, arguments.chunk_size, destination, stream)
    if only is not None:
        print(f"  restricted to {len(only):,} uncached file(s)", file=stream, flush=True)
    print(
        "  Paths are stored relative to the store root. A dispatcher must call "
        "`store_paths.load_plan()`, which resolves them - the extraction spec requires "
        "agents to receive and echo paths verbatim and absolute.",
        file=stream,
        flush=True,
    )
    if arguments.one_per_batch:
        batches(stored, arguments, stream)

    # Counted and named, because committing absolute paths is the whole defect this
    # stage exists to remove, and relativising is silent when it cannot be done: a
    # corpus outside the store root stays absolute and the file looks fine. On a
    # doc-heavy estate that was ~17,500 tracked paths carrying one build machine's
    # directory layout, and relocating the working directory rewrote every one.
    remaining = [path for files in stored.values() for path in files if path.startswith("/")]
    if remaining:
        print(
            f"  WARNING: {len(remaining):,} of {sum(sizes):,} paths could not be made "
            f"relative and are absolute in the plan, starting with {remaining[0]}.\n"
            "  They are outside the store root, so this plan carries this machine's "
            "layout and will not survive a relocation or a clone. Point graphify at a "
            "corpus inside the store, or do not commit the plan.",
            file=stream,
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
