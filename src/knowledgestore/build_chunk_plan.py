"""Write the semantic fan-out's chunk plan, so it is a file rather than a memory.

graphify's skill says only *"split into chunks of 20-25 files each"* and leaves it
to the dispatching agent. Nothing writes the split down, so it exists in one
agent's context and nowhere else. Every store running the fan-out therefore
invents its own plan, its own prompt generation, coverage check and merge - one
estate accumulated six scripts around a plan file the library had never heard of.

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
from collections import defaultdict
from pathlib import Path

from . import config, io, store_paths

# Content categories only. Code is covered structurally by the AST pass, and
# flattening every category here makes subagents re-read every source file.
# Video is transcribed to a document before this runs.
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


def plan_chunks(
    detect: dict, chunk_size: int = DEFAULT_CHUNK_SIZE, only: set[str] | None = None
) -> dict[str, list[str]]:
    """The chunk plan: chunk id -> file list, absolute as detected.

    `only` restricts the plan to a set of paths - the uncached ones - without
    changing how the rest is grouped.

    A directory larger than `chunk_size` is split across consecutive chunks rather
    than given an oversized one: an over-long FILE_LIST is what pushes an agent
    into the output limit that destroys its whole batch (#131).
    """
    files = content_files(detect)
    if only is not None:
        files = {kind: [f for f in paths if f in only] for kind, paths in files.items()}

    chunks: list[list[str]] = []
    # An image per chunk: vision needs its own context, and mixing images with
    # documents makes an agent do two jobs in one prompt.
    chunks.extend([image] for image in files.get("image", []))

    chunks.extend(
        chunk_groups(
            group_by_directory([*files.get("document", []), *files.get("paper", [])]), chunk_size
        )
    )
    return {f"{index + 1:04d}": chunk for index, chunk in enumerate(chunks)}


def chunk_groups(groups: list[list[str]], chunk_size: int) -> list[list[str]]:
    """One directory per chunk, split when a directory exceeds `chunk_size`.

    **`chunk_size` is a maximum, not a target, and chunks are never mixed.** An
    earlier version closed a chunk at a directory boundary only once it held at
    least half the target, which let a *small* directory pull the next one in - the
    opposite of the intent. On a realistic estate of twelve three-file directories
    at the suggested size of 22, every chunk mixed four directories.

    That was measured against the only real chunk plan in existence: 1,556 chunks
    over 17,539 files, min 1, max 22, mean 11.3, with **51% holding fewer than 20**.
    Small chunks are the deliberate consequence of grouping, not drift from the
    skill's "20-25 files" - and the skill asks for both, which cannot be satisfied
    at once. Grouping wins, because cross-file relationships are the reason the
    semantic layer exists and padding a chunk with unrelated files from elsewhere
    buys an agent nothing while asking it to relate things that have no relation.
    """
    chunks: list[list[str]] = []
    for group in groups:
        for start in range(0, len(group), chunk_size):
            chunks.append(group[start : start + chunk_size])
    return chunks


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
        "--uncached",
        action="store_true",
        help="plan only files graphify's cache check left to extract; without this the plan "
        "covers every content file, which is what makes the chunk archive readable",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    if arguments.chunk_size < 1:
        print("--chunk-size must be at least 1", flush=True)
        return 2

    detect = io.read_json_dict(config.DETECT_PATH)
    if not detect:
        print(
            f"No detection results at {config.DETECT_PATH}. graphify writes this when it "
            "scans the corpus, so run its detect step first - a plan invented without it "
            "would name files nobody has confirmed are there.",
            flush=True,
        )
        return 2

    only = None
    if arguments.uncached:
        text = (
            config.UNCACHED_PATH.read_text(encoding="utf-8")
            if config.UNCACHED_PATH.is_file()
            else ""
        )
        only = {line.strip() for line in text.splitlines() if line.strip()}

    plan = plan_chunks(detect, arguments.chunk_size, only)
    counted = content_files(detect)
    if not plan:
        # Not a failure: a code-only estate never runs the fan-out at all, and
        # saying so is more useful than writing an empty file it will not read.
        print(
            "No document, paper or image files detected, so the semantic fan-out has "
            "nothing to split. Nothing written.",
            flush=True,
        )
        return 0

    stored = store_paths.store_relative_plan(plan)
    io.write_json(config.CHUNK_PLAN_PATH, stored, indent=2)
    sizes = sorted(len(files) for files in plan.values())
    print(
        f"{len(plan):,} chunks over {sum(sizes):,} files -> {config.CHUNK_PLAN_PATH}\n"
        f"  per chunk: smallest {sizes[0]}, largest {sizes[-1]}, target {arguments.chunk_size}\n"
        f"  detected: " + ", ".join(f"{kind} {len(paths):,}" for kind, paths in counted.items()),
        flush=True,
    )
    if only is not None:
        print(f"  restricted to {len(only):,} uncached file(s)", flush=True)
    print(
        "  Paths are stored relative to the store root. A dispatcher must call "
        "`store_paths.load_plan()`, which resolves them - the extraction spec requires "
        "agents to receive and echo paths verbatim and absolute.",
        flush=True,
    )

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
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
