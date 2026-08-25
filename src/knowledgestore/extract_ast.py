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

**A pathological repository is identifiable by name.** One multi-megabyte
minified bundle can dominate a whole-corpus parse, and in a single call there is
nothing to attribute it to.

**One repository blowing up does not lose the run.** Failures are caught, named,
and counted; the remaining repositories still extract.

**The partial result is usable.** A layer covering every repository but the
failing ones, with those named, is worth more than no layer and a traceback, so
it is written even when repositories failed - and the exit code is still
non-zero, because a partial layer that reports success is how a store commits a
hole.

## It does not carry an exclusion list, and that is the design

Every store that has driven extraction per repository has hand-written a
vendored-path exclusion regex to keep dependency bundles and build output away
from the parser. Those lists are the wrong shape twice over.

They are wrong in fact. One store's list excluded dependency bundles, build
output and state files - and not the pipeline's own output directory, so several
hundred of the pipeline's own JSON artefacts were being handed back to the
extractor as though they were source. That was found by watching a run parse a
graph file, not by any check, because an exclusion list has no failing case: it
is correct on the day it is written and silently wrong the next time the
pipeline emits a new kind of artefact. It fails by omission, which is invisible.

They are wrong in shape. An exclusion list is a second, hand-maintained model of
what the tool produces, and it drifts from the first one by construction. The
extractor already computes the content set and writes it to
`.graphify_detect.json`. This stage consumes that, so there is one model.
Anything derived from what the pipeline actually wrote cannot drift, because the
pipeline knows what it wrote.

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
"""

from __future__ import annotations

import argparse
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


def extract_estate(groups: "dict[str, list[Path]]", extractor, root: Path) -> "tuple[dict, list]":
    """Extract each repository in turn. Returns the merged layer and the failures.

    `extractor` is `graphify.extract.extract`, injected rather than imported here
    so the loop, the reporting and the isolation are testable without the parser
    and without a corpus.
    """
    nodes: list = []
    edges: list = []
    failures: list[tuple[str, str]] = []
    width = max((len(name) for name in groups), default=1)
    for name, files in sorted(groups.items()):
        label = name or "(corpus root)"
        started = time.monotonic()
        try:
            result = extractor(files, cache_root=root)
        except Exception as error:  # a parser raises whatever the grammar raises
            # Named and counted, never fatal: on a large estate one unparseable
            # repository must not cost the other several hundred.
            failures.append((label, str(error)))
            print(f"  {label:<{width}}  FAILED  {error}", file=sys.stderr)
            continue
        found = result.get("nodes") or []
        relations = result.get("edges") or []
        nodes.extend(found)
        edges.extend(relations)
        elapsed = time.monotonic() - started
        print(
            f"  {label:<{width}}  {len(files):>6,} files  {len(found):>7,} nodes  "
            f"{len(relations):>7,} edges  {elapsed:6.1f}s"
        )
    return {"nodes": nodes, "edges": edges}, failures


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
    return parser.parse_args(argv)


def main(argv: "list[str] | None" = None) -> int:
    arguments = parse_args(argv)
    graph_directory = config.GRAPH_PATH.parent
    destination = arguments.out or (graph_directory / ".graphify_ast.json")

    if arguments.files:
        if not arguments.files.is_file():
            print(f"File list not found: {arguments.files}", file=sys.stderr)
            return 1
        files = read_file_list(arguments.files)
        source = arguments.files
    else:
        source = arguments.detect or config.DETECT_PATH
        if not source.is_file():
            print(
                f"Content set not found: {source}. The extractor writes it; run detection "
                "first, or pass an explicit list with --files.",
                file=sys.stderr,
            )
            return 1
        files = content_files(io.read_json_dict(source))

    if not files:
        # An empty layer is an upstream failure, and writing one would look like a
        # successful run right up until `merge-layers` refused it.
        print(
            f"Refusing to extract: {source} names no parseable content. An empty layer is "
            "an upstream failure, and writing one would look like a success.",
            file=sys.stderr,
        )
        return 1

    artefacts = pipeline_artefacts(files, graph_directory)
    if artefacts:
        # The real defect this refusal exists for: a store's own exclusion list
        # omitted this directory, so the pipeline's artefacts were parsed as source.
        print(
            f"Refusing to extract: {len(artefacts):,} of {len(files):,} input paths are this "
            f"pipeline's own output, under {graph_directory}. Parsing them feeds the graph "
            f"back to the extractor. First: {artefacts[0]}",
            file=sys.stderr,
        )
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
    print(f"Extracting {len(files):,} file(s) across {len(groups):,} repository group(s):")
    layer, failures = extract_estate(groups, extract, config.ROOT)

    # Written even when repositories failed: a layer covering the rest is worth
    # more than a traceback. The exit code carries the failure instead, because a
    # partial layer reporting success is how a store commits a hole.
    io.write_json(destination, layer)
    print(
        f"\nnodes {len(layer['nodes']):,}  edges {len(layer['edges']):,}  "
        f"repositories {len(groups) - len(failures):,} of {len(groups):,}"
    )
    print(f"-> {destination}")
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
