"""Which repositories depend on which others, as a layer an agent reads whole.

    knowledgestore relationships                   # build and write the layer
    knowledgestore relationships --compare PATH    # compare against another build

Every kind in `relationships.BUILTIN` - Terraform module sources, Helm charts,
Maven artifacts and npm packages - is run over every clone under `repositories/`.
Each reference resolves to a repository in the estate, to itself (counted and
dropped), or to `unresolved_targets_outside_estate`: a dependency the store does
not hold is a finding, never an absence. Three outputs, under
`knowledge/relationships/`:

- `repository-dependencies.json`, every edge with both ends, the unresolved
  targets by kind, and the conflicts;
- `kinds.json`, the counts per kind, small enough for a manifest to read;
- `by-repo/<repo>.json`, one per clone: what it depends on and what depends on
  it, plus `extra` copied from `extra/<repo>.json` when the store writes one.

The stage reads clones, `config/repositories.txt` and `knowledge/provenance.json`
(for links), and never the graph. Two runs over the same clones write the same
bytes, and a repository no longer cloned loses its `by-repo` file in the same run.

`--compare PATH` builds the layer, writes nothing, and prints every
(consumer, provider, chart, kind) whose references or pins differ from PATH's,
and every unresolved target that differs. It exits 1 on any difference: it is the
tool a store uses to retire a script that wrote the same file.

Exit codes: 0 on success; 1 on a parser failure, an unreadable configuration or
`extra` input, a missing `repositories/`, or a `--compare` difference; 2 when the
layer cannot be written.
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
    conflicts and unreadable files, each named."""
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
        for note, text in counts["notes"].items():
            line += f"; {note}: {text}"
        lines.append(line)
    for kind, targets in layer.conflicts.items():
        for target, repos in targets.items():
            lines.append(
                f"  conflict: {kind} {target} is published by {', '.join(repos)}; "
                f"resolved to {repos[0]}, the first by name"
            )
    lines.extend(f"  unreadable: {path}" for path in layer.unreadable)
    return lines


def _edge_index(body: dict) -> dict[tuple[str, str, str], tuple[dict, dict]]:
    index = {}
    for edge in body.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        key = (str(edge.get("consumer")), str(edge.get("provider")), str(edge.get("chart", "")))
        index[key] = (edge.get("kinds") or {}, edge.get("refs_pinned") or {})
    return index


def differences(here: dict, there: dict) -> list[str]:
    """Every (consumer, provider, chart, kind) whose references or pins differ, then
    every unresolved target that differs. Empty when the two agree."""
    found = []
    ours, theirs = _edge_index(here), _edge_index(there)
    for key in sorted(set(ours) | set(theirs)):
        consumer, provider, chart = key
        name = f"{consumer} -> {provider}" + (f" [{chart}]" if chart else "")
        kinds_a, pins_a = ours.get(key, ({}, {}))
        kinds_b, pins_b = theirs.get(key, ({}, {}))
        for kind in sorted(set(kinds_a) | set(kinds_b)):
            if kinds_a.get(kind, 0) != kinds_b.get(kind, 0):
                found.append(
                    f"{name} {kind}: {kinds_a.get(kind, 0)} references here, "
                    f"{kinds_b.get(kind, 0)} there"
                )
        if pins_a != pins_b:
            kinds = ",".join(sorted(set(kinds_a) | set(kinds_b)))
            found.append(f"{name} {kinds}: refs_pinned {pins_a} here, {pins_b} there")
    unresolved_a = here.get("unresolved_targets_outside_estate") or {}
    unresolved_b = there.get("unresolved_targets_outside_estate") or {}
    for kind in sorted(set(unresolved_a) | set(unresolved_b)):
        targets_a, targets_b = unresolved_a.get(kind) or {}, unresolved_b.get(kind) or {}
        for target in sorted(set(targets_a) | set(targets_b)):
            if targets_a.get(target) != targets_b.get(target):
                found.append(
                    f"unresolved {kind} {target}: {targets_a.get(target)} here, "
                    f"{targets_b.get(target)} there"
                )
    return found


def _compare(layer: relationships.Layer, path: Path) -> int:
    try:
        there = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"Cannot read {path} to compare against: {error}", file=sys.stderr)
        return 1
    if not isinstance(there, dict):
        print(f"Cannot compare against {path}: it is not a JSON object", file=sys.stderr)
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
        help="build without writing, and compare against another repository-dependencies.json",
    )
    arguments = parser.parse_args(argv)

    if not config.REPOSITORIES_DIR.is_dir():
        print(
            f"No clones under {config.REPOSITORIES_DIR} - run `knowledgestore sync` first",
            file=sys.stderr,
        )
        return 1
    try:
        config.CHART_REPO_PATTERN.format(name="chart")
    except (KeyError, IndexError, ValueError) as error:
        print(
            f"KSB_CHART_REPO_PATTERN={config.CHART_REPO_PATTERN!r} is not a pattern with "
            f"only {{name}} in it: {error!r}",
            file=sys.stderr,
        )
        return 1
    try:
        urls = SourceUrls.from_store()
    except ValueError as error:
        print(f"Cannot read the store's repository list or provenance: {error}", file=sys.stderr)
        return 1

    clones = sorted(d for d in config.REPOSITORIES_DIR.iterdir() if (d / ".git").is_dir())
    try:
        layer = relationships.build(
            config.ROOT,
            clones,
            relationships.BUILTIN,
            config.GITHUB_ORG,
            config.CHART_REPO_PATTERN,
            set(config.UMBRELLA_REPOS),
            config.RELATIONSHIP_EXAMPLES,
            urls,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
