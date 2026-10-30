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

## The shape it writes: the per-repository route's (#512)

Each repository is extracted with its clone as the extractor's root, which is
CLAUDE.md 6.2's rule, so ids and `source_file` come back repository-relative, exactly
as `graphify update .` inside the clone writes them. The stage then adds what
`graphify merge-graphs` adds: `<repo>::<id>` ids and edge endpoints, `repo`, and
`local_id` holding the un-prefixed id. It does not rewrite graphify's strings after
the fact; the one rewrite is that prefix, over ids graphify already made.

The consequence is that every consumer reads one shape whichever route built the
graph. The earlier form - store-relative `source_file`, ids derived from the store
path, `repo` unset - matched nothing in `intent[repo][source_file]`, so every node
lost its tickets. A store switching routes re-keys nothing.

The per-file record is unchanged on disk: its keys stay store-relative
(`repositories/<repo>/<path>`), so an existing baseline stays valid. The join from a
node to its content-set file is `(repo, source_file)` to that key, in `node_key`.

## Scope: the declared repositories, and nothing else (#525)

The detect scan covers the store root, so its content set also holds fetch-only
clones under `external/`, the store's own `.claude/`, `tests/` and `scripts/`, and its
generated `knowledge/` layers. Extracted, those arrive as nodes with no repository
(the layers feeding the store's own output back into its graph), and `json-schema`
skips the same files as `outside-repositories`. So this stage extracts the
repositories `config/repositories.txt` declares - what the per-repository route
hands `merge-graphs` - and reports every other file as `N file(s) outside scope,
skipped`, listing each, without extracting it. `by_repository` is the grouping both
stages share; `in_scope` is the cut this stage adds to it. A store declaring no
repositories has nothing to filter by, so every clone under `repositories/` stays in
scope.

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

graphify's detect scan already computes the content set, and the build redirects
it into `.graphify_detect.json` - the extractor does not write that file, and no
graphify subcommand does either; `content_set.DETECT_PRODUCER` carries the call.
This stage consumes the result, so there is one model.
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
deletes it every sync, so it has to be re-applied before this stage runs. A
store that declares its exclusions in `config/graphify-ignore.txt` gets that from
`apply-ignores`, and this stage refuses a clone whose ignore file lacks a declared
line - see `unapplied_exclusions`.

## Against the declared repositories, before anything runs

Grouping the content set and comparing with the last run cannot see a repository
that was never in the content set. On a first run there is no last run at all, so
a declared repository `sync` never cloned was simply absent: no error, no name,
and a layer that looked complete. So `config/repositories.txt` is read first. A
declared repository with no clone refuses the run, naming each; one whose clone
holds no parseable code is named as a warning, because a repository of documents
alone is legitimate. A store with no `config/repositories.txt` is not reconciled.

## Counts written as each repository finishes

The sidecar is rewritten, atomically, after every repository, so an interrupted
run keeps every count it measured: completed repositories carry this run's count,
and the rest keep last run's. That replaces a completed repository's baseline
before the end-of-run movement report prints, so its progress line names a
decrease as it is measured - otherwise an interrupt would leave that decrease
recorded nowhere.

## A file that went to zero

Beside the per-repository counts sits a per-file record, `<layer>_file_counts.json`:
store-relative path to node count, sorted. A file that produced nodes last run and
none now, while still in the content set, fails the run - unless `--allow-vanished`
names it, which says that one loss is intended. That is a different event from a decrease, which is
reported and never refused: the content is still there and the extractor stopped
reading it. A consuming store met exactly that - graphify extracting nothing from
JSON Schema files using `definitions` or `$defs`, with `failed_sources` empty - and
nothing in the library noticed.

graphify names files an extractor declined by design in `skipped_sources`, from
0.9.94, and files it could not read in `failed_sources`. Such a file is reported as
`declined by graphify` or `unread` rather than `VANISHED`, and fails all the same: a
lost file is lost whatever the reason. A vanished file `--allow-vanished` does not
name keeps its previous count in the record, so the next run fails again rather than
taking the zero as the new baseline.

## What the layer is made of

Beside the per-file record sits `<layer>_extension_counts.json`: nodes per source-file
extension. The run prints the largest and warns when one extension's share of the layer
has grown by `EXTENSION_GROWTH_POINTS` since the last run, as when graphify 0.9.95 began
extracting YAML as one node per key. The warning never refuses; the remedy is to list the
content in `config/graphify-ignore.txt`. A run with a failed repository leaves the record
alone, because a layer missing a repository is not a baseline.

Node ids that repeat are handled the way `graphify update` handles them: copies of one
node differing only in where they were seen collapse to the last (`merge_duplicate_ids`),
and an id shared by distinct nodes keeps every copy and is named. The per-file record
counts the nodes before that merge, so a file is not sent to zero by it.

The record is only as good as the join between a node's `source_file` and a
content-set path, and a join that misses returns a clean 0 for every file - which
would record all zeros and leave the guard unable ever to fire. So when fewer than
`MATCHED_SHARE` of the nodes carrying a `source_file` match a content-set file, the
stage refuses with an instrument mismatch and writes no record, rather than measure
something other than what it claims (CLAUDE.md 2.7).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import posixpath
import signal
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import apply_ignores, config, content_set, export_git_history, io, store_paths
from .build_store_manifest import write_atomically

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


def distinct_paths(files: "list[Path]") -> "tuple[list[Path], int]":
    """`files` with each file once, and how many repeats were dropped.

    Two spellings of one file are one file: an absolute path and the
    store-relative one name the same thing, and a list generator that emits both
    makes every count the stages report twice what the corpus holds. The kept
    spelling is the first, in the list's order - except that an absolute one
    replaces a relative one, because a relative path is read against the working
    directory and would be grouped as outside every repository.
    """
    position: dict[str, int] = {}
    kept: list[Path] = []
    for path in files:
        key = os.path.normpath(store_paths.relative(path))
        if key not in position:
            position[key] = len(kept)
            kept.append(path)
        elif path.is_absolute() and not kept[position[key]].is_absolute():
            kept[position[key]] = path
    return kept, len(files) - len(kept)


def duplicate_notice(dropped: int) -> str:
    """The line that says repeats were dropped, or an empty string when none were."""
    return f"{dropped:,} duplicate path(s) in the input ignored" if dropped else ""


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


class RepositoryTimeout(BaseException):  # NOSONAR(S5709) - deliberate, see docstring
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
    before: "dict[str, int]",
    counts: "dict[str, int]",
    failures: "list",
    pending: "Iterable[str]" = (),
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

    `pending` names the repositories a run has not reached yet, which keep their
    count for the same reason: a checkpoint written mid-run that dropped them would
    read as all of them absent if the run were interrupted there.
    """
    kept = {label for label, _ in failures} | set(pending)
    carried = {name: value for name, value in before.items() if name in kept}
    return {**carried, **counts}


def by_repository(files: "list[Path]", corpus: Path) -> "dict[str, list[Path]]":
    """Group the content set by the repository directory each file sits under.

    A file outside `repositories/` is grouped under `""` rather than being dropped, so
    the caller decides what it is; `in_scope` is what both stages use to decide.
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


def in_scope(
    files: "list[Path]", declared: "list[str] | None"
) -> "tuple[dict[str, list[Path]], list[Path]]":
    """Split the content set into what this stage extracts and what is outside its scope.

    In scope is the declared repositories (`config/repositories.txt`), which is what the
    per-repository route hands `merge-graphs`. Everything else the detect scan found - a
    fetch-only clone under `external/`, the store's own `scripts/` or `knowledge/` - is
    returned in the second half, to be named and never extracted. A store declaring
    nothing has no declaration to filter by, so every clone under `repositories/` stays
    in scope, as it did.

    `json-schema` calls this too, so the two stages cannot disagree about which files
    are in scope.
    """
    groups = by_repository(files, config.REPOSITORIES_DIR)
    outside: list[Path] = list(groups.pop("", []))
    if declared is not None:
        for name in [name for name in groups if name not in declared]:
            outside.extend(groups.pop(name))
    return groups, sorted(outside, key=str)


def outside_scope_report(outside: "list[Path]") -> "list[str]":
    """The count line and one line per path left out, store-relative; empty when none."""
    if not outside:
        return []
    shown = [f"  {store_paths.relative(path)}" for path in outside[:5]]
    more = [f"  ... and {len(outside) - 5:,} more"] if len(outside) > 5 else []
    return [
        f"{len(outside):,} file(s) outside scope, skipped (not in a declared repository):",
        *shown,
        *more,
    ]


def write_counts(path: Path, counts: "dict[str, int]") -> None:
    """The sidecar, through a temporary file and a rename, in the form it always had.

    Written after every repository, so a write interrupted part-way must leave the
    previous whole file rather than a truncated one the next run cannot read.
    """
    write_atomically(path, json.dumps(counts, ensure_ascii=False))


def declined_path(name: str, entry: str) -> str:
    """A `skipped_sources` entry as a store-relative path, whichever form it arrived in.

    Observed from graphify 0.9.94: the entry is the absolute path the extractor was
    handed, whether `cache_root` is the store or the clone - unlike `source_file`,
    which is relative to `cache_root`. The record is keyed store-relative, so an
    unconverted entry would never match and every declined file would read as
    vanished. A store- or repository-relative entry is accepted too, so a later
    change of spelling upstream cannot silently undo the distinction.
    """
    relative = store_paths.relative(entry)
    if relative.startswith(f"{config.REPOSITORIES_DIR.name}/"):
        return relative
    return f"{config.REPOSITORIES_DIR.name}/{name}/{relative}"


def unread_path(name: str, entry: str) -> str:
    """A `failed_sources` entry as the store-relative path the record is keyed by.

    graphify spells it relative to its root, which is now the clone, so a relative entry
    is placed under `repositories/<repo>/`; an absolute one already names its place.
    """
    if Path(entry).is_absolute():
        return entry
    return posixpath.normpath(f"{config.REPOSITORIES_DIR.name}/{name}/{entry}")


@dataclass
class _EstateRun:
    """What `extract_estate` accumulates across repositories, in the order it returns it."""

    nodes: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    failures: "list[tuple[str, str]]" = field(default_factory=list)
    counts: "dict[str, int]" = field(default_factory=dict)
    skipped: "list[str]" = field(default_factory=list)
    declined: "set[str]" = field(default_factory=set)
    unbounded_reported: bool = False


def namespace_repository(result: dict, name: str) -> dict:
    """One repository's extraction in the shape `graphify merge-graphs` gives it (#512).

    The extractor ran with the clone as its root, so ids and `source_file` are already
    repository-relative - what `graphify update .` inside the clone writes. What remains
    is what `merge-graphs` adds: the `<repo>::` prefix on every node id and both edge
    endpoints, the `repo` attribute, and `local_id` holding the un-prefixed id. Without
    them a node names `repositories/<repo>/...` and no repository, and the file-to-ticket
    join (`intent[repo][source_file]`) matches nothing.
    """
    prefix = f"{name}::"
    nodes = []
    for node in result.get("nodes") or []:
        moved = dict(node)
        moved["id"] = f"{prefix}{node.get('id')}"
        moved["local_id"] = node.get("id")
        moved["repo"] = name
        nodes.append(moved)
    edges = []
    for edge in result.get("edges") or []:
        moved = dict(edge)
        moved["source"] = f"{prefix}{edge.get('source')}"
        moved["target"] = f"{prefix}{edge.get('target')}"
        edges.append(moved)
    return {**result, "nodes": nodes, "edges": edges}


def clone_root(name: str) -> Path:
    """The directory the extractor treats as its root for one repository: its own clone.

    So that ids and `source_file` come out repository-relative (CLAUDE.md 6.2).
    """
    return config.REPOSITORIES_DIR / name


def under_clone(path: Path, clone: Path) -> Path:
    """`path` spelled under `clone`, lexically, whichever spelling of the store it arrived in.

    The content set can name a file through a symlink to the store, while the extractor
    resolves its root: a file it cannot place under that root is spelled `../<link>/...`,
    which is no repository-relative path at all. Re-rooting the path onto the clone, and
    never resolving the file itself, keeps a symlinked file as the file it is.
    """
    for base in (clone, clone.resolve()):
        for candidate in (path, path.parent.resolve() / path.name):
            try:
                return clone / candidate.relative_to(base)
            except ValueError:
                continue
    return path


def file_key(name: str, path: Path) -> str:
    """A content-set file's store-relative key, built from its repository and its place in it.

    Not `store_paths.relative(path)` alone, which cuts a path at the first `repositories/`
    segment and so mis-keys a store kept under a directory of that name, or reached
    through a link. Built as `node_key` builds it - `repositories/<repo>/<path in the
    clone>` - so a file and the nodes extracted from it meet on one spelling.
    """
    clone = config.REPOSITORIES_DIR / name
    placed = under_clone(path, clone)
    try:
        inside = placed.relative_to(clone).as_posix()
    except ValueError:
        return store_paths.relative(path)
    return f"{config.REPOSITORIES_DIR.name}/{name}/{inside}"


def _bounded_extract(extractor, files: "list[Path]", root: Path, timeout: int, run: _EstateRun):
    """One repository's extract call, under the per-repository time limit."""
    with repository_time_limit(timeout) as bounded:
        if timeout > 0 and not bounded and not run.unbounded_reported:
            # Once, not per repository: a limit that could not be installed
            # is a fact about the platform, and repeating it per repository
            # buries the failures it sits among.
            print(
                f"  (no per-repository time limit available here; --timeout "
                f"{timeout} is not in force)",
                file=sys.stderr,
            )
            run.unbounded_reported = True
        return extractor(files, cache_root=root)


def _progress_note(unread: "list[str]", found: int, previous: "int | None") -> str:
    """The tail of a repository's progress line: files unread, and a decrease as measured."""
    note = f"  {len(unread):>4,} unread" if unread else ""
    if previous is not None and found < previous:
        note += f"  down from {previous:,}"
    return note


def _absorb(result: dict, name: str, label: str, run: _EstateRun) -> "tuple[int, int, list[str]]":
    """Fold one repository's result into the run: (nodes, edges, unread) for its line."""
    found = result.get("nodes") or []
    relations = result.get("edges") or []
    # The extractor names every file it could not read, and until this line the
    # stage discarded that. A repository can return successfully having parsed
    # none of its files: the per-file handler catches, records, and carries on,
    # so "extracted" and "extracted anything" are different facts and only one
    # of them was being reported.
    unread = [str(f) for f in (result.get("failed_sources") or [])]
    run.skipped.extend(unread_path(name, entry) for entry in unread)
    run.declined.update(declined_path(name, str(f)) for f in result.get("skipped_sources") or [])
    run.nodes.extend(found)
    run.edges.extend(relations)
    run.counts[label] = len(found)
    return len(found), len(relations), unread


def extract_estate(
    groups: "dict[str, list[Path]]",
    extractor,
    timeout: int = 0,
    before: "dict[str, int] | None" = None,
    checkpoint: "Callable[[dict, list, list], None] | None" = None,
) -> "tuple[dict, list, dict, list, set]":
    """Extract each repository in turn: the layer, the failures, the counts, the unread,
    and the files graphify declined by design.

    `extractor` is `graphify.extract.extract`, injected rather than imported here
    so the loop, the reporting and the isolation are testable without the parser
    and without a corpus.

    The per-repository node counts are returned rather than derived from the layer
    afterwards, because a node need not carry a repository attribute - deriving
    them would make this measurement depend on a property of the extraction the
    rest of this stage does not require.

    `checkpoint` is called after every repository, extracted or failed, with the
    counts and failures so far and the labels not yet reached - which is what lets
    an interrupted run keep what it measured.
    """
    run = _EstateRun()
    width = max((len(name) for name in groups), default=1)
    ordered = sorted(groups.items())
    labels = [name for name, _ in ordered]
    for position, (name, files) in enumerate(ordered):
        label = name
        started = time.monotonic()
        try:
            clone = clone_root(name)
            handed = [under_clone(path, clone) for path in files]
            result = namespace_repository(
                _bounded_extract(extractor, handed, clone, timeout, run), name
            )
        except RepositoryTimeout as error:
            # A timeout is a failure, not a separate outcome: named, counted, the
            # partial layer kept, and the exit code carries it. The only reason to
            # tell them apart at all is that the operator's next action differs.
            run.failures.append((label, f"TIMED OUT: {error}"))
            print(f"  {label:<{width}}  TIMED OUT  {error}", file=sys.stderr)
        except Exception as error:  # a parser raises whatever the grammar raises
            # Named and counted, never fatal: on a large estate one unparseable
            # repository must not cost the other several hundred.
            run.failures.append((label, str(error)))
            print(f"  {label:<{width}}  FAILED  {error}", file=sys.stderr)
        else:
            found, relations, unread = _absorb(result, name, label, run)
            elapsed = time.monotonic() - started
            note = _progress_note(unread, found, (before or {}).get(label))
            print(
                f"  {label:<{width}}  {len(files):>6,} files  {found:>7,} nodes  "
                f"{relations:>7,} edges  {elapsed:6.1f}s{note}"
            )
        if checkpoint:
            checkpoint(run.counts, run.failures, labels[position + 1 :])
    layer = {"nodes": run.nodes, "edges": run.edges}
    return layer, run.failures, run.counts, run.skipped, run.declined


# The share of `source_file`-bearing nodes that must match a content-set file before
# the per-file record is trusted. A spelling mismatch is all or nothing - every node
# in a run is spelled by the same rule - so a working join sits near 1 and a broken
# one near 0, and a half is far from both. A few legitimate strays (a node for a file
# outside the content set) cannot reach it; a broken join cannot get past it.
MATCHED_SHARE = 0.5


def source_key(source: str) -> str:
    """A node's `source_file` (or a `failed_sources` entry) as a content-set key.

    graphify writes `source_file` relative to its *resolved* `cache_root`. Observed in
    0.9.80 and 0.9.94: with the store root reached through a symlink, a file spelled
    through the link comes back as `../<link>/repositories/...`, which matches no
    content-set path. So a relative entry is rebuilt against the resolved root -
    lexically, never resolving the file itself - and then keyed the way the content
    set is, which falls back to the `repositories/` marker for a foreign spelling.
    """
    if Path(source).is_absolute():
        return store_paths.relative(source)
    rebuilt = os.path.normpath(os.path.join(config.ROOT.resolve(), source))
    return store_paths.relative(rebuilt)


def file_counts_path(layer: Path) -> Path:
    """Where the per-file node counts sit: beside the layer and its per-repository counts."""
    return layer.with_name(f"{layer.stem}_file_counts.json")


def node_key(node: dict) -> str:
    """A node's content-set key, or "" when it names no file.

    A node with `repo` set has a repository-relative `source_file`, so the store-relative
    key the record uses is that path under `repositories/<repo>/`. One without names no
    repository, so its `source_file` is read as spelled relative to the store.
    """
    source = node.get("source_file")
    if not isinstance(source, str) or not source:
        return ""
    repo = node.get("repo")
    if isinstance(repo, str) and repo and not Path(source).is_absolute():
        return posixpath.normpath(f"{config.REPOSITORIES_DIR.name}/{repo}/{source}")
    return source_key(source)


def _nodes_by_source(layer: dict) -> "dict[str, int]":
    """Nodes per content-set key, from each node's `source_file`; nodes without one skipped."""
    found: dict[str, int] = {}
    for node in layer["nodes"]:
        key = node_key(node) if isinstance(node, dict) else ""
        if key:
            found[key] = found.get(key, 0) + 1
    return found


def nodes_per_file(
    layer: dict, groups: "dict[str, list[Path]]", failures: list
) -> "tuple[dict[str, int], int, int, str]":
    """Each content-set file's node count, and how well the join that produced it held.

    Keyed by store-relative path, from each node's `source_file`. A file with no node
    is recorded as 0, which is what lets the next run tell "went to zero" from "was
    never there". A failed repository's files are left out: they did not extract,
    and a 0 for them would read as every file vanishing.

    Also returns how many `source_file`-bearing nodes there were, how many of them
    matched a counted file, and the first that did not - the instrument check.
    """
    found = _nodes_by_source(layer)
    failed = {label for label, _ in failures}
    counts: dict[str, int] = {}
    for name, files in groups.items():
        if name in failed:
            continue
        for path in files:
            key = file_key(name, path)
            counts[key] = found.get(key, 0)
    sourced = sum(found.values())
    matched = sum(n for key, n in found.items() if key in counts)
    stray = next((key for key in sorted(found) if key not in counts), "")
    return counts, sourced, matched, stray


def vanished_files(before: "dict[str, int]", now: "dict[str, int]") -> "list[tuple[str, int]]":
    """(path, previous count) for each file that had nodes last run and has none now.

    Only files in this run's content set are compared, so a file removed from it is
    not a loss.
    """
    return sorted(
        (path, before[path])
        for path, count in now.items()
        if count == 0 and before.get(path, 0) > 0
    )


def file_record(
    before: "dict[str, int]",
    now: "dict[str, int]",
    groups: "dict[str, list[Path]]",
    failures: list,
    keep: "list[tuple[str, int]]",
) -> "dict[str, int]":
    """The record to write: this run's counts, a failed repository's last ones, sorted.

    `keep` are vanished files whose previous count stands, so an unaccepted loss
    fails every run until someone passes `--allow-vanished`.
    """
    failed = {label for label, _ in failures}
    carried = {
        key: before[key]
        for name, files in groups.items()
        if name in failed
        for key in (file_key(name, path) for path in files)
        if key in before
    }
    merged = {**carried, **now, **dict(keep)}
    return dict(sorted(merged.items()))


def loss_kind(path: str, declined: "set[str]", unread: "set[str]") -> str:
    """Why a file went to zero, when graphify said: declined, unread, or neither."""
    if path in declined:
        return "declined by graphify"
    if path in unread:
        return "unread"
    return "VANISHED"


def report_vanished(
    vanished: "list[tuple[str, int]]",
    declined: "set[str]",
    unread: "set[str]",
    accepted: "set[str]",
) -> "list[str]":
    """One line per lost file, naming why when graphify said and whether it was accepted."""
    lines = []
    for path, previous in vanished:
        kind = loss_kind(path, declined, unread)
        state = "accepted" if path in accepted else "refused"
        lines.append(f"  {kind}  {path}  ({previous:,} node(s) last run, 0 now; {state})")
    lost = sum(previous for _, previous in vanished)
    lines.append(f"{len(vanished)} file(s) vanished, {lost:,} node(s) lost.")
    return lines


def extension_breakdown(paths: "list[str]") -> str:
    """`198 .yaml, 2 .tpl`: how many of `paths` carry each extension, most first.

    Ties break on the extension, so two runs on the same input print the same line
    (CLAUDE.md 1.3). A path with no extension is counted as `(none)`.
    """
    tally: dict[str, int] = {}
    for name in paths:
        extension = posixpath.splitext(name)[1].lower() or "(none)"
        tally[extension] = tally.get(extension, 0) + 1
    ordered = sorted(tally.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{count:,} {extension}" for extension, count in ordered)


def _fails_to_read(name: str) -> bool:
    """True when the operating system will not hand over the file's first byte.

    graphify lists a file in `failed_sources` for a parse failure and for a read failure
    alike, and gives no reason. Reading the file ourselves is what tells them apart: a
    file we can read was one the parser rejected.
    """
    path = Path(name)
    try:
        with open(path if path.is_absolute() else config.ROOT / path, "rb") as handle:
            handle.read(1)
    except OSError:
        return True
    return False


def _unread_paragraph(names: "list[str]", total: int, outcome: str) -> str:
    shown = "\n".join(f"  {name}" for name in names[:5])
    more = f"\n  ... and {len(names) - 5:,} more" if len(names) > 5 else ""
    return (
        f"\n{len(names):,} of {total:,} file(s) were handed to the extractor and could "
        f"not be {outcome} ({extension_breakdown(names)}):\n{shown}{more}"
    )


def report_unread(skipped: "list[str]", total: int) -> str:
    """Files the extractor could not read. Reported, not refused on.

    The extractor has already decided to continue past them; this stage surfaces
    that decision rather than overriding it, for the same reason a decrease is
    reported and not refused. What it must not do is stay silent, because a
    repository that parsed none of its files still reports as extracted.

    A file the parser rejected is "could not be parsed" and a file the system will not
    open is "could not be read". graphify files both under `failed_sources`, and a
    template with Go templating in a `.yaml` file is not an I/O fault - the old wording
    sent operators looking for a permissions problem that was not there.
    """
    if not skipped:
        return ""
    unopenable = [name for name in skipped if _fails_to_read(name)]
    refused = set(unopenable)
    unparsed = [name for name in skipped if name not in refused]
    paragraphs = []
    if unparsed:
        paragraphs.append(_unread_paragraph(unparsed, total, "parsed"))
    if unopenable:
        paragraphs.append(_unread_paragraph(unopenable, total, "read"))
    return "".join(paragraphs)


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


# What `graphify` stamps on a node to say where it saw it, and which two copies of the
# same node therefore differ in. `graphify update` builds its graph with `add_node`, so
# nodes sharing an id collapse to the last one written; observed on a Swift
# `import Foundation` in two files, which emits one `foundation` module node per
# importing file, differing only in these two fields. Any other difference between two
# nodes of one id means they are not the same node.
PROVENANCE_FIELDS = frozenset({"source_file", "source_location"})


def merge_duplicate_ids(nodes: list) -> "tuple[list, int, list[str]]":
    """The nodes with each repeated id collapsed to its last copy, as `graphify update` does.

    Returns the nodes, how many copies were dropped, and the ids that were *not*
    collapsed. An id whose copies differ in anything beyond where they were seen is a
    collision of distinct nodes, not one node emitted twice, and dropping one of them
    would lose data without a word: every copy is kept and the id is named instead.
    Order follows each id's first appearance, so the output is deterministic.
    """
    groups: dict[object, list[dict]] = {}
    for node in nodes:
        groups.setdefault(node.get("id") if isinstance(node, dict) else None, []).append(node)
    kept: list = []
    merged = 0
    collisions: list[str] = []
    for node_id, copies in groups.items():
        identities = [
            {k: v for k, v in copy.items() if k not in PROVENANCE_FIELDS}
            if isinstance(copy, dict)
            else copy
            for copy in copies
        ]
        if node_id is None or any(identity != identities[0] for identity in identities):
            if node_id is not None:
                collisions.append(str(node_id))
            kept.extend(copies)
        else:
            kept.append(copies[-1])
            merged += len(copies) - 1
    return kept, merged, sorted(collisions)


def report_duplicate_ids(merged: int, collisions: "list[str]") -> "tuple[str, str]":
    """(stdout, stderr) text for the duplicate-id handling; each empty when it has nothing to say."""
    said = ""
    if merged:
        said = (
            f"\n{merged:,} duplicate node id(s) merged: the same node emitted once per file "
            "that references it, kept once with the last copy's source_file, as "
            "`graphify update` does."
        )
    warned = ""
    if collisions:
        shown = "\n".join(f"  {node_id}" for node_id in collisions[:5])
        more = f"\n  ... and {len(collisions) - 5:,} more" if len(collisions) > 5 else ""
        warned = (
            f"\nwarning: {len(collisions):,} node id(s) are shared by distinct nodes (they "
            f"differ in more than where they were seen), so every copy was kept:\n{shown}{more}"
        )
    return said, warned


# An extension is flagged when its share of the layer has grown by this many points
# (0.10 is ten percentage points) since the last run. graphify 0.9.95 took `.yaml` from
# no nodes to a large share of a layer, so the real event clears this by a wide
# margin. A routine run - one repository added or refactored among hundreds - moves an
# extension's share by a point or two, and a threshold that fires on that is read once
# and then ignored. Only growth is flagged: shrinkage is for the movement and
# vanished-file reports.
EXTENSION_GROWTH_POINTS = 0.10


def extension_counts_path(layer: Path) -> Path:
    """Where the per-extension node counts sit: beside the layer and its per-file record."""
    return layer.with_name(f"{layer.stem}_extension_counts.json")


def nodes_per_extension(nodes: list) -> "dict[str, int]":
    """Nodes per source-file extension, lower-cased and sorted; `(none)` and `(no source_file)`.

    Counted from `source_file` as the nodes carry it, so what is counted is the layer as
    written, not the content set that went in.
    """
    tally: dict[str, int] = {}
    for node in nodes:
        source = node.get("source_file") if isinstance(node, dict) else None
        if not isinstance(source, str) or not source:
            key = "(no source_file)"
        else:
            key = posixpath.splitext(source)[1].lower() or "(none)"
        tally[key] = tally.get(key, 0) + 1
    return dict(sorted(tally.items()))


def _share(counts: "dict[str, int]", extension: str) -> float:
    total = sum(counts.values())
    return counts.get(extension, 0) / total if total else 0.0


def grown_extensions(
    before: "dict[str, int]", now: "dict[str, int]"
) -> "list[tuple[str, int, int, float]]":
    """(extension, nodes before, nodes now, share now) for each that crossed the threshold."""
    return [
        (extension, before.get(extension, 0), count, _share(now, extension))
        for extension, count in sorted(now.items())
        if count > before.get(extension, 0)
        and _share(now, extension) - _share(before, extension) >= EXTENSION_GROWTH_POINTS
    ]


def report_extensions(
    now: "dict[str, int]", grown: "list[tuple[str, int, int, float]]"
) -> "tuple[str, str]":
    """(counts line, warning): the largest extensions, and a warning when any grew past the threshold."""
    ordered = sorted(now.items(), key=lambda item: (-item[1], item[0]))
    line = "\nnodes per extension: " + ", ".join(f"{e} {n:,}" for e, n in ordered[:10])
    if len(ordered) > 10:
        line += f", ... and {len(ordered) - 10:,} more"
    if not grown:
        return line, ""
    lines = [
        f"warning: {len(grown)} extension(s) grew by {EXTENSION_GROWTH_POINTS:.0%} of the "
        "layer or more since the last run:"
    ]
    for extension, was, count, share in grown:
        lines.append(f"  {extension}: {was:,} -> {count:,} nodes ({share:.0%} of the layer)")
    lines.append(
        "If that content is modelled elsewhere or is not wanted, list it in "
        "config/graphify-ignore.txt (e.g. `*.yaml` and `*.yml`). Nothing was refused."
    )
    return line, "\n".join(lines)


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
        "--allow-vanished",
        nargs="+",
        default=[],
        metavar="PATH",
        help="accept these store-relative files producing nodes last run and none now "
        "(an intended change); any other file that did still fails the run",
    )
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
                f"Content set not found: {source}. {content_set.DETECT_PRODUCER} Or pass an "
                "explicit list with --files."
            )
        files = content_files(io.read_json_dict(source))

    files, dropped = distinct_paths(files)
    if dropped:
        print(duplicate_notice(dropped))

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


def declared_repositories() -> "list[str] | None":
    """Every repository `config/repositories.txt` names, or None when the store has none.

    Read with the library's own parser. An unreadable declaration refuses rather than
    reading as "declares nothing", which would switch the reconciliation off.
    """
    try:
        return export_git_history.declared_names()
    except (OSError, ValueError) as error:
        raise InputRefused(f"Cannot read the declared repositories: {error}") from None


def reconcile(declared: "list[str]", groups: "dict[str, list[Path]]") -> "list[str]":
    """Refuse a declared repository with no clone; return those with nothing to parse.

    Both are invisible to the comparison with the last run, and on a first run there
    is no last run: a repository that is not in the content set is not in anything
    this stage would otherwise read.
    """
    where = store_paths.relative(config.REPOSITORIES_CONFIG)
    missing = [name for name in declared if not (config.REPOSITORIES_DIR / name).is_dir()]
    if missing:
        named = "\n".join(f"  {name}" for name in missing)
        raise InputRefused(
            f"Refusing to extract: {len(missing)} repositor{'y' if len(missing) == 1 else 'ies'} "
            f"declared in {where} {'has' if len(missing) == 1 else 'have'} no clone under "
            f"{store_paths.relative(config.REPOSITORIES_DIR)}/:\n{named}\n"
            "Run `knowledgestore sync`, or remove them from the declaration. A layer "
            "without them would read as the whole estate."
        )
    return [name for name in declared if not groups.get(name)]


def unapplied_exclusions(names: "list[str]") -> "list[tuple[str, str]]":
    """(repository, pattern) for each declared exclusion a clone's ignore file lacks.

    Empty when the store declares none, which is what keeps a store that never
    opted in unchanged.
    """
    try:
        declared = apply_ignores.read_declarations(config.GRAPHIFY_IGNORE_PATH)
    except (OSError, ValueError) as error:
        raise InputRefused(f"Cannot read the declared exclusions: {error}") from None
    if not declared:
        return []
    found = []
    for name in names:
        clone = config.REPOSITORIES_DIR / name
        if clone.is_dir():
            patterns = apply_ignores.patterns_for(declared, name)
            found += [(name, pattern) for pattern in apply_ignores.absent(clone, patterns)]
    return found


def check_declared(groups: "dict[str, list[Path]]", declared: "list[str] | None") -> "list[str]":
    """Every refusal the store's declarations raise, and the warnings they leave."""
    empty = reconcile(declared, groups) if declared is not None else []
    names: "list[str]" = sorted({*(declared or []), *(name for name in groups if name)})
    unapplied = unapplied_exclusions(names)
    if unapplied:
        listed = "\n".join(f"  {name}: {pattern}" for name, pattern in unapplied)
        raise InputRefused(
            f"Refusing to extract: {len(unapplied)} exclusion(s) declared in "
            f"{store_paths.relative(config.GRAPHIFY_IGNORE_PATH)} are absent from the "
            f"clone's .graphifyignore:\n{listed}\n"
            "`sync` deletes those files. Run `knowledgestore apply-ignores`, then the "
            "detect scan, then this stage."
        )
    return [
        f"warning: {name} is declared in {store_paths.relative(config.REPOSITORIES_CONFIG)} "
        "and has no parseable code in the content set, so it is absent from the layer."
        for name in empty
    ]


def _read_file_record(path: Path) -> "dict[str, int] | None":
    """Last run's per-file counts, or None when there was no last run."""
    stored = io.read_json(path, default=None)
    if not isinstance(stored, dict):
        return None
    return {str(k): int(v) for k, v in stored.items() if isinstance(v, (int, float))}


def instrument_refusal(sourced: int, matched: int, stray: str) -> str:
    """The refusal when the `source_file` join does not hold, or "" when it does."""
    if matched >= sourced * MATCHED_SHARE:
        return ""
    return (
        f"\nRefusing to write the per-file record: instrument mismatch. {sourced - matched:,} "
        f"of {sourced:,} node(s) name a source_file matching no content-set file (first: "
        f"{stray}). Counting through that join would record files that have nodes as 0, "
        "and a guard holding only zeros can never fire, so no record was written."
    )


def vanished_report(
    destination: Path,
    layer: dict,
    groups: "dict[str, list[Path]]",
    failures: list,
    lost_to: "tuple[set[str], set[str]]",
    allow: "list[str]",
) -> bool:
    """Compare and rewrite the per-file record; True when the run must fail on it.

    `lost_to` is the files graphify declined and the files it could not read, both
    as content-set keys; `allow` the paths `--allow-vanished` named.
    """
    path = file_counts_path(destination)
    now, sourced, matched, stray = nodes_per_file(layer, groups, failures)
    previous = _read_file_record(path)
    # No node at all is every file going to zero, so it takes the vanished path when
    # the record had files with nodes; with nothing to lose there is nothing to say.
    if not sourced and not vanished_files(previous or {}, now):
        print(
            f"\nno node carries a source_file, so no file can be counted; {path.name} "
            "was not written."
        )
        return False
    refusal = instrument_refusal(sourced, matched, stray)
    if refusal:
        print(refusal, file=sys.stderr)
        return True
    vanished = vanished_files(previous or {}, now)
    accepted = {store_paths.relative(p) for p in allow}
    refused = [(file, count) for file, count in vanished if file not in accepted]
    write_counts(path, file_record(previous or {}, now, groups, failures, refused))
    for name in sorted(accepted - {file for file, _ in vanished}):
        print(
            f"--allow-vanished names {name}, which did not vanish; nothing to accept.",
            file=sys.stderr,
        )
    if previous is None:
        print(
            f"\nno per-file record in {path.name}; wrote one, so a file going to zero "
            "is caught from the next run."
        )
        return False
    if not vanished:
        print("\n0 file(s) vanished, 0 node(s) lost.")
        return False
    print(
        f"\n{len(vanished)} file(s) produced nodes last run and none now; "
        f"{len(vanished) - len(refused)} accepted by --allow-vanished, {len(refused)} refused:",
        file=sys.stderr,
    )
    for line in report_vanished(vanished, *lost_to, accepted):
        print(line, file=sys.stderr)
    if refused:
        print(
            "Pass --allow-vanished PATH for each loss that is intended; the rest fail "
            "every run until then.",
            file=sys.stderr,
        )
    return bool(refused)


def scoped_input(
    arguments, graph_directory: Path
) -> "tuple[dict[str, list[Path]], list[Path], list[str]]":
    """The groups to extract, the files left out of scope, and the declaration's warnings.

    Every refusal that can precede extraction is raised from here, so `main` has one
    place to catch them.
    """
    files, source = resolve_input(arguments, graph_directory)
    declared = declared_repositories()
    groups, outside = in_scope(files, declared)
    if not groups:
        raise InputRefused(
            f"Refusing to extract: none of the {len(files):,} file(s) in {source} is in "
            f"scope, which is a declared repository. {len(outside):,} file(s) outside "
            f"scope, first: {store_paths.relative(outside[0]) if outside else ''}."
        )
    return groups, outside, check_declared(groups, declared)


def prepare_run(
    arguments, graph_directory: Path
) -> "tuple[dict[str, list[Path]], Callable] | None":
    """The groups to extract and the extractor to drive, or None after printing why not.

    Everything that can refuse before a parser runs, in the order an operator meets it:
    the scope and the declarations, the `ast` extra, then the grammars.
    """
    try:
        groups, outside, warnings = scoped_input(arguments, graph_directory)
    except InputRefused as refusal:
        print(str(refusal), file=sys.stderr)
        return None
    for line in outside_scope_report(outside):
        print(line)
    for line in warnings:
        print(line, file=sys.stderr)

    try:
        from graphify.extract import extract
    except ImportError:
        print(
            "The `ast` extra is not installed, so there is no extractor to drive. Install "
            "with `pip install 'hmcts-knowledge-store-builder[ast]'` and re-run.",
            file=sys.stderr,
        )
        return None

    # The extractor is imported in this process, so this interpreter is the one that
    # must hold the grammars - passed explicitly, not left to the stage's fallback.
    # Imported here because `check_grammars` reuses this module's input handling.
    from . import check_grammars

    _report, missing = check_grammars.check(
        groups, lambda _name: (sys.executable, "extract-ast's own interpreter")
    )
    for line in missing:
        print(f"Refusing to extract: {line}", file=sys.stderr)
    return None if missing else (groups, extract)


def report_failures(failures: list) -> None:
    """Name each repository group that failed to extract, on stderr."""
    print(
        f"\n{len(failures)} repository group(s) failed to extract and are absent from the layer:",
        file=sys.stderr,
    )
    for label, error in failures:
        print(f"  {label}: {error}", file=sys.stderr)


def report_extension_growth(destination: Path, nodes: list, failures: list) -> None:
    """Print per-extension counts and any growth since last run; then record them.

    Not recorded when a repository failed: a layer missing one is not a baseline, and
    the next complete run would then read the missing repository as growth everywhere.
    """
    path = extension_counts_path(destination)
    now = nodes_per_extension(nodes)
    stored = io.read_json(path, default=None)
    before = (
        {str(k): int(v) for k, v in stored.items() if isinstance(v, (int, float))}
        if isinstance(stored, dict)
        else None
    )
    grown = grown_extensions(before, now) if before is not None else []
    counts_line, warning = report_extensions(now, grown)
    print(counts_line)
    if warning:
        print(warning, file=sys.stderr)
    if before is None:
        print(
            f"no per-extension record in {path.name}; wrote one, so growth is caught from "
            "the next run."
        )
    if failures:
        print(
            f"{path.name} was left as it was: a repository failed, so this layer is not a baseline."
        )
        return
    write_counts(path, now)


def main(argv: "list[str] | None" = None) -> int:
    arguments = parse_args(argv)
    graph_directory = config.GRAPH_PATH.parent
    destination = arguments.out or (graph_directory / ".graphify_ast.json")

    prepared = prepare_run(arguments, graph_directory)
    if prepared is None:
        return 1
    groups, extract = prepared
    # Read before anything is written: the sidecar is the only record of the last
    # run, and it is rewritten as each repository finishes.
    sidecar = counts_path(destination)
    before = previous_counts(sidecar)

    def record(counts: "dict[str, int]", failures: list, pending: list) -> None:
        # Mid-run, the sidecar and the layer are deliberately out of step: an
        # interrupted run keeps its counts and leaves the previous layer standing.
        write_counts(sidecar, carry_forward(before, counts, failures, pending))

    in_scope_files = sum(len(group) for group in groups.values())
    print(f"Extracting {in_scope_files:,} file(s) across {len(groups):,} repository group(s):")
    layer, failures, counts, skipped, declined = extract_estate(
        groups, extract, arguments.timeout, before, record
    )

    # Written even when repositories failed: a layer covering the rest is worth
    # more than a traceback. The exit code carries the failure instead, because a
    # partial layer reporting success is how a store commits a hole.
    # Counted and recorded from the raw layer, below, so a file whose only node is one
    # shared with others still has it in its count; merging first would send such a file
    # to zero and have the guard fail a file that extracted fine.
    nodes, merged, collisions = merge_duplicate_ids(layer["nodes"])
    written = {**layer, "nodes": nodes}
    io.write_json(destination, written)
    # Only repositories that actually extracted get a fresh count, so a failure does
    # not write a 0 that the next run would read as a legitimate decrease to nothing -
    # and a failed repository keeps its previous one rather than vanishing from the
    # baseline, which would erase the record that shows what the failure cost.
    write_counts(sidecar, carry_forward(before, counts, failures))
    print(
        f"\nnodes {len(nodes):,}  edges {len(layer['edges']):,}  "
        f"repositories {len(groups) - len(failures):,} of {len(groups):,}"
    )
    print(f"-> {destination}")
    print(report_unread(skipped, in_scope_files), end="" if not skipped else "\n")
    if before:
        print(report_movement(movement(before, counts)) or "\nno per-repository movement.")
    else:
        print(f"\nno previous run recorded in {sidecar.name}; movement starts next run.")
    said, warned = report_duplicate_ids(merged, collisions)
    print(said, end="\n" if said else "")
    if warned:
        print(warned, file=sys.stderr)
    report_extension_growth(destination, nodes, failures)
    unread = {source_key(entry) for entry in skipped}
    refused = vanished_report(
        destination, layer, groups, failures, (declined, unread), arguments.allow_vanished
    )
    if failures:
        report_failures(failures)
        return 1
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
