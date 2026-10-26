"""Which repositories depend on which others, as a layer an agent reads whole.

    knowledgestore relationships                   # build and write the layer
    knowledgestore relationships --strict          # the same, failing on an unresolved chart
    knowledgestore relationships --compare PATH    # compare against a layer file

Every kind in `relationships.BUILTIN` - Terraform module sources, Helm charts,
Maven artifacts and npm packages - and every kind the store registers in
`config/relationship-kinds.json` is run over every clone under `repositories/`.
Each reference resolves to a repository in the estate, to itself (counted and
dropped), or to `unresolved_targets_outside_estate`: a dependency the store does
not hold is a finding, never an absence. Three outputs, under
`knowledge/relationships/`:

- `repository-dependencies.json`, every edge with both ends, the unresolved
  targets by kind, the conflicts, and the charts, Maven coordinates and npm
  packages several clones define that no rule chooses between
  (`unresolved_conflicts`);
- `kinds.json`, the counts per kind, small enough for a manifest to read;
- `by-repo/<repo>.json`, one per clone: what it depends on and what depends on
  it, plus `extra` copied from `extra/<repo>.json` when the store writes one.

The stage reads clones, `config/repositories.txt` (for links, and each clone's
organisation) and `knowledge/provenance.json` (for links), and never the graph. Two runs over the same clones write the same
bytes, and a repository no longer cloned loses its `by-repo` file in the same run.

A chart several clones define that no rule chooses between leaves every consumer
of it out of `dependents`, so the run ends with one line counting them -
`N charts unresolved: declare KSB_UMBRELLA_REPOS or KSB_CHART_REPO_PATTERN` - and
a Maven coordinate or npm package likewise, with
`N coordinates unresolved: declare KSB_FORK_REPOS`. `--strict` turns either into
exit 1, after the layer is written, for a store that wants the build to stop there.

`--compare PATH` builds the layer, writes nothing, and prints every
(consumer, provider, chart, kind) whose references or pins differ from PATH's,
and every unresolved target that differs. It exits 1 on any difference. PATH must
be a `repository-dependencies.json` in this stage's own layer format: nothing
else is read. A file of another shape, or with no format stamp or another major, exits 1 with "Cannot compare against
PATH: ..." and compares nothing. Parity with another tool is reached by
converting that tool's output to this format first, which is the store's job.

Exit codes: 0 on success; 1 on a parser failure, a kind declaration that cannot be used, an unreadable configuration or
`extra` input, a missing `repositories/`, a Flux `HelmRelease` with PyYAML not
installed (the `deploy` extra), a `--compare` difference, or under `--strict` an
unresolved chart; 2 when the layer cannot be written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, relationships, telemetry
from .source_urls import SourceUrls


def _plural(count: int, word: str, plural: str = "") -> str:
    return f"{count} {word}" if count == 1 else f"{count} {plural or word + 's'}"


def report(layer: relationships.Layer) -> list[str]:
    """One line per kind - edges, references, unresolved and skipped - then the
    conflicts, the unresolved conflicts and the unreadable files, each named."""
    lines = [
        f"Relationships: {_plural(len(layer.dependencies['edges']), 'edge')} between "
        f"{_plural(len(layer.by_repo), 'repository', 'repositories')}"
    ]
    for name, counts in layer.kinds.items():
        skipped = counts["skipped"]
        reasons = ", ".join(f"{reason} {count}" for reason, count in skipped.items())
        line = (
            f"  {name}: {_plural(counts['edges'], 'edge')}, "
            f"{_plural(counts['references'], 'reference')}, {counts['unresolved']} unresolved, "
            f"{sum(skipped.values())} skipped"
        )
        line += f" ({reasons})" if reasons else ""
        lines.append(line)
    for kind, targets in layer.conflicts.items():
        for target, repos in targets.items():
            lines.append(
                f"  conflict: {kind} {target} is published by {', '.join(repos)}; "
                f"resolved to {repos[0]}, the first by name"
            )
    for kind, targets in layer.unresolved_conflicts.items():
        settle = (
            "declare a wrapper in KSB_UMBRELLA_REPOS or name chart repositories with "
            "KSB_CHART_REPO_PATTERN"
            if kind == "helm_chart"
            else "declare the copies in KSB_FORK_REPOS"
        )
        for target, repos in targets.items():
            lines.append(
                f"  conflict: {kind} {target} is defined by {', '.join(repos)}; left unresolved - "
                + settle
            )
    lines.extend(f"  unreadable: {path}" for path in layer.unreadable)
    charts = len(layer.unresolved_conflicts.get("helm_chart", {}))
    if charts:
        lines.append(f"{_plural(charts, 'chart')} unresolved: {DECLARE}")
    coordinates = unresolved_charts(layer) - charts
    if coordinates:
        lines.append(f"{_plural(coordinates, 'coordinate')} unresolved: {DECLARE_FORKS}")
    return lines


# What settles an unresolved chart, and an unresolved Maven coordinate or npm
# package, named wherever one is reported.
DECLARE = "declare KSB_UMBRELLA_REPOS or KSB_CHART_REPO_PATTERN"
DECLARE_FORKS = "declare KSB_FORK_REPOS"


def unresolved_charts(layer: relationships.Layer) -> int:
    """How many targets `unresolved_conflicts` holds, across every kind it names:
    charts, Maven coordinates and npm packages alike."""
    return sum(len(targets) for targets in layer.unresolved_conflicts.values())


def shape_error(body: object) -> str:
    """Why `body` is not a `repository-dependencies.json` that can be compared, or "".

    The format stamp first, then the shape: a file with no stamp is not this stage's
    format, and which of its fields happens to be malformed says nothing useful.
    """
    if not isinstance(body, dict):
        return "it is not a JSON object"
    problem = relationships.format_problem(body)
    if problem:
        return problem
    edges = body.get("edges", [])
    if not isinstance(edges, list):
        return "`edges` is not a list"
    for number, edge in enumerate(edges):
        problem = _edge_shape_error(number, edge)
        if problem:
            return problem
    unresolved = body.get("unresolved_targets_outside_estate", {})
    if not isinstance(unresolved, dict):
        return "`unresolved_targets_outside_estate` is not an object"
    for kind, targets in unresolved.items():
        if not isinstance(targets, dict) or not all(isinstance(t, dict) for t in targets.values()):
            return f"`unresolved_targets_outside_estate.{kind}` is not an object of objects"
    return ""


def _edge_shape_error(number: int, edge: object) -> str:
    """Why edge `number` cannot be compared, or ""."""
    if not isinstance(edge, dict):
        return f"edge {number} is not an object"
    for name in ("kinds", "refs_pinned"):
        if not isinstance(edge.get(name, {}), dict):
            return f"edge {number}'s `{name}` is not an object"
    unpinned = edge.get("refs_unpinned", 0)
    if not isinstance(unpinned, int) or isinstance(unpinned, bool):
        return f"edge {number}'s `refs_unpinned` is not an integer"
    return ""


def _edge_index(body: dict) -> dict[tuple[str, str, str], tuple[dict, dict, int]]:
    index = {}
    for edge in body.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        key = (str(edge.get("consumer")), str(edge.get("provider")), str(edge.get("chart", "")))
        index[key] = (
            edge.get("kinds") or {},
            edge.get("refs_pinned") or {},
            edge.get("refs_unpinned", 0),
        )
    return index


def differences(here: dict, there: dict) -> list[str]:
    """Every (consumer, provider, chart, kind) whose references, pins or unpinned
    count differ, then every unresolved target that differs. Empty when the two
    agree."""
    found = []
    ours, theirs = _edge_index(here), _edge_index(there)
    for key in sorted(set(ours) | set(theirs)):
        found.extend(
            _edge_differences(key, ours.get(key, ({}, {}, 0)), theirs.get(key, ({}, {}, 0)))
        )
    unresolved_a = here.get("unresolved_targets_outside_estate") or {}
    unresolved_b = there.get("unresolved_targets_outside_estate") or {}
    for kind in sorted(set(unresolved_a) | set(unresolved_b)):
        found.extend(
            _unresolved_differences(
                kind, unresolved_a.get(kind) or {}, unresolved_b.get(kind) or {}
            )
        )
    return found


def _edge_differences(
    key: tuple[str, str, str], ours: tuple[dict, dict, int], theirs: tuple[dict, dict, int]
) -> list[str]:
    """How one (consumer, provider, chart) edge differs between here and there."""
    found = []
    consumer, provider, chart = key
    name = f"{consumer} -> {provider}" + (f" [{chart}]" if chart else "")
    kinds_a, pins_a, unpinned_a = ours
    kinds_b, pins_b, unpinned_b = theirs
    for kind in sorted(set(kinds_a) | set(kinds_b)):
        if kinds_a.get(kind, 0) != kinds_b.get(kind, 0):
            found.append(
                f"{name} {kind}: {kinds_a.get(kind, 0)} references here, "
                f"{kinds_b.get(kind, 0)} there"
            )
    kinds = ",".join(sorted(set(kinds_a) | set(kinds_b)))
    if pins_a != pins_b:
        found.append(f"{name} {kinds}: refs_pinned {pins_a} here, {pins_b} there")
    if unpinned_a != unpinned_b:
        found.append(f"{name} {kinds}: refs_unpinned {unpinned_a} here, {unpinned_b} there")
    return found


def _unresolved_differences(kind: str, targets_a: dict, targets_b: dict) -> list[str]:
    """Each unresolved target of one kind that differs between here and there."""
    return [
        f"unresolved {kind} {target}: {targets_a.get(target)} here, {targets_b.get(target)} there"
        for target in sorted(set(targets_a) | set(targets_b))
        if targets_a.get(target) != targets_b.get(target)
    ]


def _compare(layer: relationships.Layer, path: Path) -> int:
    try:
        # NOSONAR(S8707) - reading the path the operator named with `--compare` is the
        # purpose of the flag, and nothing is written there. Grounds are stated once
        # in `build_community_summaries.merge`; this site cites them rather than
        # restating them, so the two cannot drift.
        there = json.loads(path.read_text(encoding="utf-8"))  # NOSONAR(S8707)
    except (OSError, ValueError) as error:
        print(f"Cannot read {path} to compare against: {error}", file=sys.stderr)
        return 1
    problem = shape_error(there)
    if problem:
        print(f"Cannot compare against {path}: {problem}", file=sys.stderr)
        return 1
    found = differences(layer.dependencies, there)
    if not found:
        print(f"Relationships: this build and {path} agree")
        return 0
    print(f"Relationships: {_plural(len(found), 'difference')} from {path}:")
    for line in found:
        print(f"  {line}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knowledgestore relationships",
        description="Write knowledge/relationships/: which repositories reference which.",
    )
    parser.add_argument(
        "--compare",
        metavar="PATH",
        help=(
            "build without writing, and compare against PATH, a repository-dependencies.json "
            "in this stage's own layer format only; convert any other tool's output to "
            "this format first"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "exit 1 when any chart, Maven coordinate or npm package is left unresolved; "
            "the layer is written first"
        ),
    )
    arguments = parser.parse_args(argv)

    if not config.REPOSITORIES_DIR.is_dir():
        print(
            f"No clones under {config.REPOSITORIES_DIR} - run `knowledgestore sync` first",
            file=sys.stderr,
        )
        return 1
    problem = relationships.chart_pattern_problem(config.CHART_REPO_PATTERN)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    try:
        urls = SourceUrls.from_store()
    except ValueError as error:
        print(f"Cannot read the store's repository list or provenance: {error}", file=sys.stderr)
        return 1

    clones = sorted(d for d in config.REPOSITORIES_DIR.iterdir() if (d / ".git").is_dir())
    try:
        store_kinds = relationships.load_store_kinds(
            config.ROOT, config.RELATIONSHIP_KINDS_CONFIG_PATH
        )
        layer = relationships.build(
            config.ROOT,
            clones,
            (*relationships.BUILTIN, *store_kinds),
            config.GITHUB_ORG,
            config.CHART_REPO_PATTERN,
            set(config.UMBRELLA_REPOS),
            config.RELATIONSHIP_EXAMPLES,
            urls,
            config.RELATIONSHIPS_EXTRA_DIR,
            forks=set(config.FORK_REPOS),
        )
    except (relationships.KindError, ValueError) as error:
        print(f"Nothing written: {error}", file=sys.stderr)
        return 1

    if arguments.compare:
        return _compare(layer, Path(arguments.compare))

    try:
        relationships.write(layer, config)
    except (OSError, ValueError) as error:
        print(f"Cannot write {config.RELATIONSHIPS_DIR}: {error}", file=sys.stderr)
        return 2
    for line in report(layer):
        print(line)
    telemetry.record(
        {
            "relationships.edges": len(layer.dependencies["edges"]),
            "relationships.references": sum(k["references"] for k in layer.kinds.values()),
            "relationships.unresolved": sum(k["unresolved"] for k in layer.kinds.values()),
        }
    )
    if arguments.strict and unresolved_charts(layer):
        print(
            f"--strict: {_plural(unresolved_charts(layer), 'conflict')} unresolved, so the "
            "layer is written and the run fails",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
