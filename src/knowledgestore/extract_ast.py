"""Drive AST extraction one repository at a time, over the content set the pipeline computed.

    knowledgestore extract-ast   -> graphify-out/.graphify_ast.json

The AST layer's producer, and the missing first half of `merge-layers`: that
stage reads `.graphify_ast.json` and nothing in this library wrote it.

Requires the `ast` extra, because extraction itself is a third-party parser:

    pip install 'hmcts-knowledge-store-builder[ast]'
    knowledgestore extract-ast

## What this replaces

The documented route hands the whole corpus to the extractor in one call. On a
several-hundred-repository estate that was reported as running for over eight
minutes at full CPU and producing nothing at all - no detect file, no layer, not
one cache entry - with no way from outside to tell a hung run from a slow one,
because the extractor emits nothing per repository. Driving the same extraction
API one repository at a time over the same corpus returned a complete layer in
seconds.

The speed is not the point and is not what this stage is for. Three properties
are:

**A pathological repository is identifiable by name, and the run ends.** One
multi-megabyte minified bundle can dominate a whole-corpus parse, and in a single
call there is nothing to attribute it to. `--timeout` bounds each repository,
because a name nobody is awake to read is not an improvement on no name - see
`repository_time_limit` for what that bound does and does not cover.

**One repository blowing up does not lose the run.** Failures are caught, named,
and counted; the remaining repositories still extract.

**The partial result is usable.** A layer covering every repository but the
failing ones, with those named, is worth more than no layer and a traceback, so
it is written even when repositories failed - and the exit code is still
non-zero, because a partial layer that reports success is how a store commits a
hole.

## Nodes per repository, against the last run

Reconciling this stage's own count against the content set it was handed catches
input dropped *within* a run. It cannot catch a repository extracting materially
less than it did last time - a parser version moves, a file is renamed, language
detection changes - where the stage succeeds, every count reconciles against the
input it was given, the layer is written, and the store loses a repository's worth
of structure quietly. An empty layer and a partial layer are both refused above;
**a smaller layer is neither of those**, and it is the common case.

So the per-repository counts go to a sidecar beside the layer, and the next run
reports what moved: decreased, absent, new. Those are three events needing three
responses, which a single delta cannot express.

**Reported, never refused on.** A decrease can be entirely legitimate - code was
deleted - so the judgement is a person's. Refusing would also make this stage
unusable in a chain, which is where it is most useful, and would train an operator
to pass whatever silences it. Non-zero stays reserved for a repository that did not
extract at all.

The sidecar is a working file beside the layer, deliberately not a committed
artefact: it is the same class of thing as `.graphify_ast.json` itself, so it needs
no decision from a store's owner about what the store commits. If the estate-wide
build stamp grows a per-repository series, this should move there rather than
persist as a second convention - but that artefact cannot hold a per-repository
series today, and a working file is the option that does not block on it.

## It does not carry an exclusion list, and that is the design

Every store that has driven extraction per repository has hand-written a
vendored-path exclusion regex to keep dependency bundles and build output away
from the parser. Those lists are wrong in shape, and only incidentally wrong in
fact - which is the order that matters, because the second invites the answer
"then maintain the list better" and the first does not.

**Wrong in shape: an exclusion list is a check with no failing case.** It is a
second, hand-maintained model of what the tool produces, and it drifts from the
first by construction. No test can catch the drift, because a test would have to
already know the artefact existed - which is the same thing the list would have
to know. It fails by omission, and omission is exactly what neither a list nor a
test of a list can see. That is the shape of a vacuous gate, and it cannot be
fixed by being more careful.

The extractor already computes the content set and writes it to
`.graphify_detect.json`. This stage consumes that, so there is one model.
Anything derived from what the pipeline actually wrote cannot drift, because the
pipeline knows what it wrote.

Wrong in fact, as the instance that made it visible: one store's list excluded
dependency bundles, build output and state files - and not the pipeline's own
output directory, so several hundred of the pipeline's own JSON artefacts were
handed back to the extractor as source, and nodes in that store's committed graph
describe the graph itself. Found by watching a run parse a graph file. Not by any
check, because there was no check that could have failed.

So this stage takes its file list and does not derive one. `--files` accepts an
explicit list for a caller that has computed its own; the default reads the
extractor's. Neither route grows a pattern here.

The one refusal it does carry is `pipeline_artefacts`, and it is not an
exclusion list: it takes the name of the directory this library *knows* it
writes, and refuses input from inside one whichever route supplied the list.
That is the defect above turned into a failing case rather than a longer regex.

It checks for that directory anywhere in the path rather than only at the store
root, because `sync` ends with `git clean -fd -e graphify-out` - so that
directory is the one thing in a clone sync deliberately preserves, and every
repository in the corpus can hold one.

**Which cleanliness this claims, precisely.** The content set is clean of *this
pipeline's own output*, by construction, and that is the whole of the claim. It
is not a claim about symlinks, which are the other way a repository walk reads
content that is not that repository's - whether the content set is symlink-aware
is a property of the extractor's detection, and this stage's default inherits
whatever that is rather than improving on it. A store relying on a per-repository
ignore file for symlinked trees should note that `git clean -fd -e graphify-out`
deletes it every sync, so it has to be re-applied before this stage runs and
nothing here fails if it was not.
"""

from __future__ import annotations

import argparse
import contextlib
import signal
import sys
import time
from pathlib import Path

from . import config, io

# The kinds `detect` classifies that hold parseable source. `paper`, `image` and
# `video` are content the semantic layer reads and the AST parser cannot, so
# handing them over costs a parse per file and yields nothing.
CODE_KINDS = ("code",)


def content_files(detect: dict, kinds: "tuple[str, ...]" = CODE_KINDS) -> "list[Path]":
    """The extractor's own content set, as paths, for the kinds that parse."""
    files = detect.get("files")
    if not isinstance(files, dict):
        return []
    found: list[Path] = []
    for kind in kinds:
        for entry in files.get(kind) or []:
            if isinstance(entry, str) and entry:
                found.append(Path(entry))
    return found


def read_file_list(path: Path) -> "list[Path]":
    """A newline-delimited path list, as written for `grep -f` and the like."""
    # Sonar S8707, on the same grounds as `build_community_summaries.merge`, which
    # reads a caller-supplied batch the same way: reading a path this stage's
    # operator named is the purpose of the flag, and this is a maintainer CLI run
    # offline against a local clone with no privilege boundary to cross. Recorded
    # once there and cross-referenced here rather than reasoned out twice, so the
    # two cannot drift into two different policies.
    lines = path.read_text(encoding="utf-8").splitlines()  # NOSONAR(S8707)
    return [Path(line.strip()) for line in lines if line.strip()]


def pipeline_artefacts(files: "list[Path]", graph_directory: Path) -> "list[Path]":
    """Input paths that lie in a directory this pipeline writes.

    Not an exclusion list: the name is taken from the configured output directory,
    so it cannot drift from what the pipeline emits the way a pattern list does.

    Both placements are checked, and the second is the one that bites. `sync` ends
    with `git clean -fd -e graphify-out`, so that directory is the one thing in a
    clone sync deliberately preserves - which means every repository in the corpus
    can hold one, not just the store root. A refusal anchored only at the root
    would pass a per-clone artefact straight to the parser, which is the defect
    itself rather than a near miss.
    """
    try:
        resolved = graph_directory.resolve()
    except OSError:  # pragma: no cover - an unresolvable root is caught upstream
        resolved = graph_directory
    name = graph_directory.name
    inside = []
    for path in files:
        try:
            candidate = path.resolve()
        except OSError:
            continue
        at_the_store_root = candidate == resolved or resolved in candidate.parents
        # `parts[:-1]`: a *directory* component, so a file that merely shares the
        # name is not swept up with the directories that hold artefacts.
        in_a_clone = bool(name) and name in candidate.parts[:-1]
        if at_the_store_root or in_a_clone:
            inside.append(path)
    return inside


class RepositoryTimeout(BaseException):
    """The per-repository bound, deriving from BaseException on purpose.

    Measured against the real extractor, not reasoned about: it wraps each file in
    its own `except Exception`, logs a warning and moves on. A `TimeoutError` - or
    anything else below `Exception` - is therefore **caught by the extractor and
    swallowed**, and the call returns *successfully* with the file skipped. The
    bound then does the opposite of its job: instead of naming a repository and
    failing, it silently converts a hang into a smaller layer, which is precisely
    the failure the movement check exists to catch a whole run later.

    Deriving from BaseException is what makes the bound survive a per-file handler
    that was written to be robust, for the same reason `KeyboardInterrupt` does.
    """


@contextlib.contextmanager
def repository_time_limit(seconds: int):
    """Bound one repository's parse, so a pathological one names itself and ends.

    Per-repository extraction gives you the *name* of the repository that is
    hanging. It does not end the run, and a name nobody is awake to read is not
    an improvement on no name. This is what makes "one bundle can dominate a
    parse" an observation rather than a night lost.

    Yields True when a bound is actually in force, so a caller can report which
    it got rather than assuming. It is not in force in two cases, and both are
    reported rather than silently downgraded: a platform with no `SIGALRM`, and
    a call from any thread but the main one, where installing a handler raises.

    **What this bounds, measured rather than reasoned about.** The concern worth
    having was that Python runs signal handlers between bytecode instructions, so
    a long call inside a C extension - which is where a minified bundle's parse
    goes - might not be interrupted. Run against a real multi-megabyte bundle whose
    unbounded parse takes several seconds, a one-second limit raised at 1.01s. The
    alarm lands, and it lands on the case that motivated it.

    What the measurement did find is the reason for `RepositoryTimeout` above: with
    an ordinary `TimeoutError`, the extractor's own per-file handler caught it,
    skipped the file and returned *successfully* with fewer nodes.

    A subprocess per repository would be a harder bound still, and is not taken:
    it forfeits running in one interpreter, which is what makes the parser's
    version recordable and stops it resolving to a different executable than the
    one the lock names.
    """
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield False
        return

    def _expire(signum, frame):
        raise RepositoryTimeout(f"exceeded the {seconds}s per-repository limit")

    try:
        previous = signal.signal(signal.SIGALRM, _expire)
    except ValueError:  # not the main thread, so no handler can be installed
        yield False
        return
    signal.alarm(seconds)
    try:
        yield True
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def counts_path(layer: Path) -> Path:
    """Where the per-repository node counts sit: beside the layer, not committed."""
    return layer.with_name(f"{layer.stem}_counts.json")


def previous_counts(path: Path) -> "dict[str, int]":
    """Last run's nodes-per-repository, or empty when there was no last run."""
    stored = io.read_json(path, default={}) or {}
    if not isinstance(stored, dict):
        return {}
    return {str(k): int(v) for k, v in stored.items() if isinstance(v, (int, float))}


def movement(before: "dict[str, int]", after: "dict[str, int]") -> "dict[str, list]":
    """Three things a changed count can mean, kept apart because responses differ.

    A repository that shrank, one that is gone, and one that is new are not the
    same event, and a single "changed" number cannot tell them apart. Reconciling
    a run against the content set it was handed is a different check and already
    here: it catches input dropped *within* a run. It cannot catch a repository
    extracting materially less than it did last time - a parser version moves, a
    file is renamed, language detection changes - where the stage succeeds, the
    count reconciles against its own input, and the store loses a repository's
    worth of structure quietly.
    """
    return {
        "decreased": sorted(
            (name, before[name], after[name])
            for name in set(before) & set(after)
            if after[name] < before[name]
        ),
        "absent": sorted(set(before) - set(after)),
        "new": sorted(set(after) - set(before)),
    }


def carry_forward(
    before: "dict[str, int]", counts: "dict[str, int]", failures: "list"
) -> "dict[str, int]":
    """Keep a failed repository's last known count rather than dropping it.

    Recording nothing for a repository that failed is not neutral. It reads as
    *absent from the content set* on the next run and as *new* on the one after -
    two false movement reports from one failure - and it discards the baseline
    that would have shown a decrease when the repository comes back.

    The degenerate case is the one that matters: if every repository fails, an
    unfiltered write replaces the whole baseline with nothing, so the next run
    reports that there is no previous run at all. A failed run would have erased
    the record that shows what the failure cost, which is the same shape as every
    other silent-empty defect this stage guards against.

    A carried count is deliberately stale: comparing the next successful run
    against the last one that actually extracted is what a decrease check is for.
    """
    failed = {label for label, _ in failures}
    carried = {name: value for name, value in before.items() if name in failed}
    return {**carried, **counts}


def by_repository(files: "list[Path]", corpus: Path) -> "dict[str, list[Path]]":
    """Group the content set by the repository directory each file sits under.

    A file outside the corpus is grouped under `""`, which reports as `(corpus
    root)` rather than being dropped: silently discarding input would make this
    stage's own count disagree with the content set it was handed.
    """
    try:
        corpus_resolved = corpus.resolve()
    except OSError:
        corpus_resolved = corpus
    groups: dict[str, list[Path]] = {}
    for path in files:
        try:
            relative = path.resolve().relative_to(corpus_resolved)
            name = relative.parts[0] if len(relative.parts) > 1 else ""
        except (OSError, ValueError):
            name = ""
        groups.setdefault(name, []).append(path)
    return groups


def extract_estate(
    groups: "dict[str, list[Path]]", extractor, root: Path, timeout: int = 0
) -> "tuple[dict, list, dict, list]":
    """Extract each repository in turn: the layer, the failures, the counts, the unread.

    `extractor` is `graphify.extract.extract`, injected rather than imported here
    so the loop, the reporting and the isolation are testable without the parser
    and without a corpus.

    The per-repository node counts are returned rather than derived from the layer
    afterwards, because a node need not carry a repository attribute - deriving
    them would make this measurement depend on a property of the extraction the
    rest of this stage does not require.
    """
    nodes: list = []
    edges: list = []
    failures: list[tuple[str, str]] = []
    counts: dict[str, int] = {}
    skipped: list[str] = []
    width = max((len(name) for name in groups), default=1)
    unbounded_reported = False
    for name, files in sorted(groups.items()):
        label = name or "(corpus root)"
        started = time.monotonic()
        try:
            with repository_time_limit(timeout) as bounded:
                if timeout > 0 and not bounded and not unbounded_reported:
                    # Once, not per repository: a limit that could not be installed
                    # is a fact about the platform, and repeating it per repository
                    # buries the failures it sits among.
                    print(
                        f"  (no per-repository time limit available here; --timeout "
                        f"{timeout} is not in force)",
                        file=sys.stderr,
                    )
                    unbounded_reported = True
                result = extractor(files, cache_root=root)
        except RepositoryTimeout as error:
            # A timeout is a failure, not a separate outcome: named, counted, the
            # partial layer kept, and the exit code carries it. The only reason to
            # tell them apart at all is that the operator's next action differs.
            failures.append((label, f"TIMED OUT: {error}"))
            print(f"  {label:<{width}}  TIMED OUT  {error}", file=sys.stderr)
            continue
        except Exception as error:  # a parser raises whatever the grammar raises
            # Named and counted, never fatal: on a large estate one unparseable
            # repository must not cost the other several hundred.
            failures.append((label, str(error)))
            print(f"  {label:<{width}}  FAILED  {error}", file=sys.stderr)
            continue
        found = result.get("nodes") or []
        relations = result.get("edges") or []
        # The extractor names every file it could not read, and until this line the
        # stage discarded that. A repository can return successfully having parsed
        # none of its files: the per-file handler catches, records, and carries on,
        # so "extracted" and "extracted anything" are different facts and only one
        # of them was being reported.
        unread = [str(f) for f in (result.get("failed_sources") or [])]
        skipped.extend(unread)
        nodes.extend(found)
        edges.extend(relations)
        counts[label] = len(found)
        elapsed = time.monotonic() - started
        note = f"  {len(unread):>4,} unread" if unread else ""
        print(
            f"  {label:<{width}}  {len(files):>6,} files  {len(found):>7,} nodes  "
            f"{len(relations):>7,} edges  {elapsed:6.1f}s{note}"
        )
    return {"nodes": nodes, "edges": edges}, failures, counts, skipped


def report_unread(skipped: "list[str]", total: int) -> str:
    """Files the extractor could not read. Reported, not refused on.

    The extractor has already decided to continue past them; this stage surfaces
    that decision rather than overriding it, for the same reason a decrease is
    reported and not refused. What it must not do is stay silent, because a
    repository that parsed none of its files still reports as extracted.
    """
    if not skipped:
        return ""
    shown = "\n".join(f"  {name}" for name in skipped[:5])
    more = f"\n  ... and {len(skipped) - 5:,} more" if len(skipped) > 5 else ""
    return (
        f"\n{len(skipped):,} of {total:,} file(s) were handed to the extractor and could "
        f"not be read:\n{shown}{more}"
    )


def report_movement(moved: "dict[str, list]") -> str:
    """Every decrease named. Never an exit code - the judgement is a person's.

    A decrease can be legitimate: code was deleted. Refusing on one would make
    this stage unusable in a chain, which is where it is most useful, and would
    train an operator to pass whatever silences it. So it reports and returns 0,
    and non-zero stays reserved for a repository that did not extract at all.
    """
    lines = []
    for name, before, after in moved["decreased"]:
        lines.append(f"  {name}: {before:,} -> {after:,} nodes  ({after - before:+,})")
    for name in moved["absent"]:
        lines.append(f"  {name}: extracted last run, absent from the content set now")
    for name in moved["new"]:
        lines.append(f"  {name}: new since the last run")
    if not lines:
        return ""
    return "\nper-repository movement since the last run:\n" + "\n".join(lines)


def parse_args(argv: "list[str] | None") -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore extract-ast",
        description="Extract the AST layer one repository at a time.",
    )
    parser.add_argument(
        "--detect",
        type=Path,
        help="the extractor's content set (default: graphify-out/.graphify_detect.json)",
    )
    parser.add_argument(
        "--files",
        type=Path,
        help="a newline-delimited path list to extract instead of the content set",
    )
    parser.add_argument("--out", type=Path, help="destination layer (default: .graphify_ast.json)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="seconds any one repository may take before it is timed out and named "
        "(0 disables; see repository_time_limit for what it does and does not bound)",
    )
    return parser.parse_args(argv)


class InputRefused(Exception):
    """A reason not to start, carrying the message an operator needs.

    Deciding *what* to extract and *extracting* it are different jobs with
    different failure modes: everything here refuses before the parser is even
    imported, and nothing here can leave a half-written layer behind. Separating
    them keeps every refusal in one place, which is what makes it answerable to
    ask whether they are all still reachable.
    """


def resolve_input(arguments, graph_directory: Path) -> "tuple[list[Path], Path]":
    """The files to extract and where the list came from, or a refusal.

    Both routes end here on purpose. `--files` is the route most likely to carry
    a hand-rolled list, so it is the one that least deserves to skip the checks.
    """
    if arguments.files:
        if not arguments.files.is_file():
            raise InputRefused(f"File list not found: {arguments.files}")
        files = read_file_list(arguments.files)
        source = arguments.files
    else:
        source = arguments.detect or config.DETECT_PATH
        if not source.is_file():
            raise InputRefused(
                f"Content set not found: {source}. The extractor writes it; run detection "
                "first, or pass an explicit list with --files."
            )
        files = content_files(io.read_json_dict(source))

    if not files:
        # An empty layer is an upstream failure, and writing one would look like a
        # successful run right up until `merge-layers` refused it.
        raise InputRefused(
            f"Refusing to extract: {source} names no parseable content. An empty layer is "
            "an upstream failure, and writing one would look like a success."
        )

    artefacts = pipeline_artefacts(files, graph_directory)
    if artefacts:
        # The real defect this refusal exists for: a store's own exclusion list
        # omitted this directory, so the pipeline's artefacts were parsed as source.
        raise InputRefused(
            f"Refusing to extract: {len(artefacts):,} of {len(files):,} input paths are this "
            f"pipeline's own output, under {graph_directory}. Parsing them feeds the graph "
            f"back to the extractor. First: {artefacts[0]}"
        )
    return files, source


def main(argv: "list[str] | None" = None) -> int:
    arguments = parse_args(argv)
    graph_directory = config.GRAPH_PATH.parent
    destination = arguments.out or (graph_directory / ".graphify_ast.json")

    try:
        files, _source = resolve_input(arguments, graph_directory)
    except InputRefused as refusal:
        print(str(refusal), file=sys.stderr)
        return 1

    try:
        from graphify.extract import extract
    except ImportError:
        print(
            "The `ast` extra is not installed, so there is no extractor to drive. Install "
            "with `pip install 'hmcts-knowledge-store-builder[ast]'` and re-run.",
            file=sys.stderr,
        )
        return 1

    groups = by_repository(files, config.REPOSITORIES_DIR)
    # Read before anything is written: the sidecar is the only record of the last
    # run, and writing the layer first would leave a half-updated pair if the
    # write failed between them.
    sidecar = counts_path(destination)
    before = previous_counts(sidecar)

    print(f"Extracting {len(files):,} file(s) across {len(groups):,} repository group(s):")
    layer, failures, counts, skipped = extract_estate(
        groups, extract, config.ROOT, arguments.timeout
    )

    # Written even when repositories failed: a layer covering the rest is worth
    # more than a traceback. The exit code carries the failure instead, because a
    # partial layer reporting success is how a store commits a hole.
    io.write_json(destination, layer)
    # Only repositories that actually extracted get a fresh count, so a failure does
    # not write a 0 that the next run would read as a legitimate decrease to nothing -
    # and a failed repository keeps its previous one rather than vanishing from the
    # baseline, which would erase the record that shows what the failure cost.
    io.write_json(sidecar, carry_forward(before, counts, failures))
    print(
        f"\nnodes {len(layer['nodes']):,}  edges {len(layer['edges']):,}  "
        f"repositories {len(groups) - len(failures):,} of {len(groups):,}"
    )
    print(f"-> {destination}")
    print(report_unread(skipped, len(files)), end="" if not skipped else "\n")
    if before:
        print(report_movement(movement(before, counts)) or "\nno per-repository movement.")
    else:
        print(f"\nno previous run recorded in {sidecar.name}; movement starts next run.")
    if failures:
        print(
            f"\n{len(failures)} repository group(s) failed to extract and are absent from the "
            "layer:",
            file=sys.stderr,
        )
        for label, error in failures:
            print(f"  {label}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
