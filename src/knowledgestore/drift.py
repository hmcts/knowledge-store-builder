"""What a sync made stale in the semantic layer, measured from the recorded shas.

    knowledgestore drift --before <provenance the layer was extracted from> [--out PATH]

`chunk-plan --uncached` answers a different question: which files graphify's cache
has not seen. On a store whose chunks were written by anything other than
`merge-chunks` that is most of the corpus, so it is not the cost of a resync. Measured
on one large internal estate after a one-day sync, the cache planned 11,379 files and
the diff between the recorded shas gave 2,070.
Only the second is what a resync has to pay for.

So this stage reads two provenance files - the one the committed layer was extracted
from, and the one sync just wrote - and for each repository whose sha moved asks git
what changed between them. A repository whose recorded sha did not move contributes
nothing, whatever its clone holds now.

## What counts

**Stale means extracted and now out of date**, so the set measured against is the
chunk plan the committed layer was extracted from, not the content set. A content
file the plan never held is unextracted rather than stale - `--uncached` is the
question for that - and counting it here mixes the two answers into one number.

    changed   modified (or type-changed) and in the plan
    deleted   deleted and in the plan
    new       added, with an extension the plan already holds

The extension rule for `new` stands in for graphify's detect categories, which a
file that did not exist at the last scan has no entry in. It is derived from the
plan and from nothing else: a delta must extract what a full build would, so an
extensionless file counts only when the plan already holds an extensionless path.

**A rename is its old path deleted and its new path new.** The diff runs with
`--no-renames`, so git reports the pair as a deletion and an addition whatever the
user's `diff.renames` says - the count cannot depend on a setting outside the store.
`-z` keeps a path with a space or a non-ASCII character verbatim, where the default
output quotes it and the quoted form matches nothing in the plan.

## Which plan

**The plan has to be the one as it stood at `--before`**, and after a resync it no
longer is: the resync appends its new chunks, so the committed plan already holds
the files the sync added, and they would read as unchanged. Take it from the commit
that recorded the "before" provenance:

    git show <before-commit>:graphify-out/.graphify_chunk_plan.json > plan-before.json

Handing it the rewritten plan is refused rather than measured. The test is a file
the diff reports as added after "before" and the plan already holds - it did not
exist at "before", so no extraction made then can have read it. That costs nothing
beyond the diff already run, and it does not misfire on a file that is in the plan
but not in git, such as a converted document: an untracked file never appears in a
diff. It cannot see a rewritten plan for a sync that added no planned file, and
then the rewritten plan measures the same as the right one.

## What it writes

    {"stale": [...], "new": [...], "changed": [...], "deleted": [...]}

Store-relative and sorted, to `--out` or to stdout. `chunk-plan --delta` reads it.
Nothing is written when any repository's diff fails: a partial drift reads as a
small one, which is the wrong answer to a clone that lost a commit.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import TextIO

from . import config, io, store_paths

TOP_REPOSITORIES = 5


def recorded_shas(path: Path) -> dict[str, str]:
    """Repository name -> recorded sha, from a provenance file."""
    data = io.read_json_dict(path)
    repositories = data.get("repositories")
    if not isinstance(repositories, dict):
        return {}
    return {
        name: entry["sha"]
        for name, entry in repositories.items()
        if isinstance(entry, dict) and isinstance(entry.get("sha"), str)
    }


def plan_files(path: Path) -> set[str]:
    """Every file the plan holds, store-relative whatever form it was written in."""
    plan = io.read_json_dict(path)
    return {store_paths.relative(f) for files in plan.values() for f in files}


def name_status(clone: Path, before: str, after: str) -> tuple[list[tuple[str, str]], str]:
    """(status, path) per change between two shas, or ([], git's complaint)."""
    completed = subprocess.run(
        ["git", "-C", str(clone), "diff", "--no-renames", "-z", "--name-status", before, after],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode:
        return [], (completed.stderr.strip().splitlines() or ["git diff failed"])[0]
    fields = completed.stdout.split("\0")
    return list(zip(fields[0:-1:2], fields[1::2])), ""


class Changes:
    """What the diffs said, accumulated over every repository that moved."""

    def __init__(self, planned: set[str]) -> None:
        self.planned = planned
        # Derived from the plan alone, "" included only when the plan holds an
        # extensionless path: admitting one the plan never held would extract a file
        # a full build would not.
        self.admitted = {posixpath.splitext(path)[1] for path in planned}
        self.new: set[str] = set()
        self.changed: set[str] = set()
        self.deleted: set[str] = set()
        self.premature: list[str] = []

    def add(self, status: str, relative: str) -> None:
        """Classify one diff record. A rename never arrives: the diff runs --no-renames."""
        if status == "A":
            self.added(relative)
        elif status == "D":
            self.deleted.add(relative)
        else:
            self.changed.add(relative)

    def added(self, relative: str) -> None:
        # In the plan already: the plan postdates --before, which is refused.
        if relative in self.planned:
            self.premature.append(relative)
        if posixpath.splitext(relative)[1] in self.admitted:
            self.new.add(relative)

    def drift(self) -> dict[str, list[str]]:
        changed = self.changed & self.planned
        deleted = self.deleted & self.planned
        return {
            "stale": sorted(self.new | changed | deleted),
            "new": sorted(self.new),
            "changed": sorted(changed),
            "deleted": sorted(deleted),
        }


def measure(
    before: dict[str, str], after: dict[str, str], planned: set[str], repositories: Path
) -> tuple[dict[str, list[str]], list[str], dict[str, str], list[str]]:
    """(the drift, the repositories that moved, the ones whose diff failed, and the
    planned files the diff says were added after "before")."""
    changes = Changes(planned)
    moved = [name for name in sorted(set(before) & set(after)) if before[name] != after[name]]
    failed: dict[str, str] = {}
    for name in moved:
        records, error = name_status(repositories / name, before[name], after[name])
        if error:
            failed[name] = error
        for status, path in records:
            changes.add(status, store_paths.relative(repositories / name / path))
    return changes.drift(), moved, failed, sorted(changes.premature)


def repository_of(path: str) -> str:
    parts = path.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "repositories" else parts[0]


def report(
    result: dict[str, list[str]],
    planned: int,
    moved: int,
    compared: int,
    unpaired: int,
    stream: TextIO,
) -> None:
    """One line of counts, then which repositories carry them."""
    stale = result["stale"]
    by_repository = Counter(repository_of(path) for path in stale)
    share = 100 * len(stale) / planned if planned else 0.0
    print(
        f"{len(stale):,} stale ({share:.1f}% of the {planned:,}-file extraction plan) = "
        f"{len(result['new']):,} new + {len(result['changed']):,} changed + "
        f"{len(result['deleted']):,} deleted across {len(by_repository):,} repositories",
        file=stream,
        flush=True,
    )
    # Count first, then name: a tie must not be broken by hash order.
    top = sorted(by_repository.items(), key=lambda item: (-item[1], item[0]))[:TOP_REPOSITORIES]
    print(
        f"  {moved:,} of {compared:,} repositories moved"
        + (f"; top: {', '.join(f'{name} {count:,}' for name, count in top)}" if top else ""),
        file=stream,
        flush=True,
    )
    if unpaired:
        print(
            f"  {unpaired:,} repositories are in only one provenance file and were not measured",
            file=stream,
            flush=True,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore drift",
        description="Measure which extracted files a sync made stale, from the recorded shas.",
    )
    parser.add_argument(
        "--before",
        required=True,
        help="the provenance the committed layer was extracted from - "
        "`git show <before-commit>:knowledge/provenance.json > <file>`",
    )
    parser.add_argument(
        "--after",
        help="the provenance sync just wrote (default: knowledge/provenance.json)",
    )
    parser.add_argument(
        "--plan",
        help="the extraction plan as it stood at the --before provenance - "
        "`git show <before-commit>:graphify-out/.graphify_chunk_plan.json > <file>`. "
        "The default, graphify-out/.graphify_chunk_plan.json, is that plan only until a "
        "resync rewrites it; a rewritten one is refused",
    )
    parser.add_argument("--out", help="where to write the drift (default: stdout)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    # The report goes wherever the JSON does not, so a caller can pipe the JSON.
    stream = sys.stdout if arguments.out else sys.stderr
    plan_path = Path(arguments.plan) if arguments.plan else config.CHUNK_PLAN_PATH
    if not plan_path.is_file():
        print(
            f"No chunk plan at {plan_path}. Stale means extracted and now out of date, so "
            "drift measures against the plan the layer was extracted from; pass --plan.",
            file=stream,
            flush=True,
        )
        return 2
    before = recorded_shas(Path(arguments.before))
    after = recorded_shas(Path(arguments.after) if arguments.after else config.PROVENANCE_PATH)
    if not before or not after:
        print(
            "Both provenance files must record a sha per repository; "
            f"--before holds {len(before)} and --after {len(after)}.",
            file=stream,
            flush=True,
        )
        return 2

    planned = plan_files(plan_path)
    result, moved, failed, premature = measure(before, after, planned, config.REPOSITORIES_DIR)
    if failed:
        print(
            f"git diff failed in {len(failed)} of {len(moved)} moved repositories, so nothing "
            "was written - a partial drift reads as a small one:",
            file=stream,
            flush=True,
        )
        for name, error in failed.items():
            print(f"  {name}: {error}", file=stream, flush=True)
        return 2
    if premature:
        print(
            f"The plan holds {len(premature):,} file(s) added after the --before provenance, "
            f"starting with {premature[0]}, so it is not the plan as it stood at --before - "
            "a resync has rewritten it. Nothing was written. Take the plan from the commit "
            "that recorded --before:\n"
            "  git show <before-commit>:graphify-out/.graphify_chunk_plan.json > plan-before.json\n"
            "and pass it as --plan.",
            file=stream,
            flush=True,
        )
        return 2

    text = json.dumps(result, indent=1, ensure_ascii=False)
    if arguments.out:
        io.write_json(Path(arguments.out), result, indent=1)
    else:
        print(text, flush=True)
    paired = set(before) & set(after)
    report(
        result,
        len(planned),
        len(moved),
        len(paired),
        len(set(before) ^ set(after)),
        stream,
    )
    if arguments.out:
        print(f"  -> {arguments.out}", file=stream, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
