"""How many of a store's dangling edge endpoints name a node that exists.

    knowledgestore dangling-endpoints

`merge-graphs` creates a node for any id an edge mentions, so an edge endpoint
missing from its own graph's node list survives the merge as a node carrying
identity and nothing else. `status` already counts those *after* the merge
(`contentless_nodes`). This stage asks the question that has to be asked
before it: of the endpoints that dangle, how many name an entity the graph
already holds under another id, and how many name nothing at all?

**Why this is a measurement and not a repair.** Three estates measured the
same predicate and got rates three orders of magnitude apart - one near zero,
one around a third, one over four fifths - and by *different mechanisms*: on
one, normalisation failed to match a node that was present; on another, a
chunk named an id defined in a different chunk. Same defect class, different
cause, so no single repair can be chosen centrally and a store that has not
measured its own rate cannot choose one either. This stage exists so that each
store can answer the question for itself. It reads; it never writes to the
graph.

**Measured before the merge, deliberately.** One estate's first measurement
came back at zero dangling endpoints and it was a tautology: its own merge and
layer-combine steps drop dangling endpoints before anything downstream sees the
file, so by the time a consuming tool reads it there is nothing left to fail on.
A rate measured downstream of a store's own fix is not a rate. So the default
walk is the per-repository graphs under `repositories/<name>/graphify-out/`,
the estate graph at the store root is refused by name if that walk finds
nothing, and every file read is printed.

**Rate is not size.** The highest of the three rates seen was also negligible
in absolute terms - a few dozen endpoints against tens of thousands of edges -
so the report leads with counts and gives the rate beside them. A store
deciding whether a repair is worth building needs both.

**Ambiguity is its own bucket and is never guessed.** An endpoint whose name
matches more than one node is counted as ambiguous, not as recovered: folding
it into the recovered total would report as recoverable exactly the cases where
a repair would have to pick one node arbitrarily, which is how a merge invents
relationships. The report prints the ambiguous count even when it is zero,
because that zero is the load-bearing part of the number beside it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from . import config, graph_files, graph_stream, io

# The per-repository graph, in the order preferred when both exist. graphify
# writes the uncompressed file; a store that has compressed one keeps the other
# beside it. Whichever is read is named in the report, and the counterpart is
# named too - a measurement that silently picked one of two files would be the
# stale-graph class this repository has already paid for twice.
GRAPH_NAMES = ("graph.json", "graph.json.gz")

# The classes a dangling endpoint falls into. Ordered as the report prints them.
RECOVERABLE, AMBIGUOUS, ABSENT = "recoverable", "ambiguous", "absent"


def entity_name(value: object) -> str:
    """The entity a graph id names: its final `::` segment, casefolded.

    This is the whole matching predicate, kept deliberately narrow and stated
    here so a reader can judge what the rate means. graphify's ids are
    path-and-scope qualified with `::` and its dangling endpoints arrive as bare
    casefolded names, so the final segment is the part the two forms have in
    common - and a `local_id` is that segment already, which is what makes the
    endpoint recoverable at all.

    Nothing wider. Stripping punctuation, splitting camel case or matching on a
    stem would each recover more endpoints and guess at more of them, and this
    number's only value is that it can be trusted to be a floor.
    """
    if value is None:
        return ""
    return str(value).strip().rsplit("::", 1)[-1].casefold()


def node_keys(node: dict) -> set[str]:
    """The entity names one node answers to: its id's, and its `local_id`'s.

    A set, so a node whose `local_id` already equals its id's final segment
    contributes one key and not two. Counting it twice would make every such
    node ambiguous with itself, and an ambiguous count inflated by the
    instrument is worse than no ambiguous count at all - it reads as caution.
    """
    keys = {entity_name(node.get("id")), entity_name(node.get("local_id"))}
    keys.discard("")
    return keys


@dataclass
class Measurement:
    """One graph's answer, and enough to say what was read to get it."""

    path: Path
    counterpart: Path | None = None
    nodes: int = 0
    edges: int = 0
    endpoints: int = 0
    dangling_edges: int = 0
    classified: dict[str, list[str]] = field(default_factory=dict)
    error: str = ""

    @property
    def dangling(self) -> int:
        return sum(len(ids) for ids in self.classified.values())

    def count(self, kind: str) -> int:
        return len(self.classified.get(kind, ()))

    @property
    def measurable(self) -> bool:
        """False when this file cannot answer the question at all.

        A graph with no nodes makes every endpoint dangle and a graph with no
        edges has no endpoints to dangle, so both would report a rate computed
        from nothing. Naming them as unmeasurable is the difference between
        'this store has no problem' and 'this file was never read'.
        """
        return not self.error and self.nodes > 0 and self.edges > 0


def _index(path: Path) -> tuple[set[str], dict[str, int]]:
    """(every node id, how many nodes answer to each entity name).

    Counts rather than id lists: the ambiguity question is 'how many', the
    per-repository graph can hold hundreds of thousands of nodes, and holding
    the ids as well would double what this pass costs for nothing.
    """
    ids: set[str] = set()
    names: dict[str, int] = {}
    for node in graph_stream.iter_array(path, key="nodes"):
        node_id = node.get("id")
        if node_id is not None:
            ids.add(str(node_id))
        for key in node_keys(node):
            names[key] = names.get(key, 0) + 1
    return ids, names


def classify(endpoint: str, names: dict[str, int]) -> str:
    """Which bucket one dangling endpoint belongs in.

    `> 1` is `AMBIGUOUS` and never `RECOVERABLE`. That is the constraint the
    whole measurement rests on: a repair built on this number would resolve only
    where the name is unambiguous, so a rate that quietly counted the ambiguous
    ones would promise recoveries the repair must refuse to make.
    """
    found = names.get(entity_name(endpoint), 0)
    if found == 1:
        return RECOVERABLE
    return AMBIGUOUS if found > 1 else ABSENT


def measure_graph(path: Path) -> Measurement:
    """Measure one graph, streaming it twice: nodes, then edges.

    Streamed rather than loaded because these files reach gigabytes decompressed
    and this stage may be pointed at every repository in an estate in one run.
    """
    result = Measurement(path=path, counterpart=_counterpart(path))
    try:
        ids, names = _index(path)
        result.nodes = len(ids)
        _scan_edges(path, ids, names, result)
    except (OSError, ValueError, EOFError) as error:
        # ValueError covers graph_stream.TruncatedJson, which subclasses it; EOFError
        # is listed separately because a truncated `.gz` raises that instead and is
        # neither an OSError nor a ValueError.
        result.error = str(error)
    return result


def _scan_edges(path: Path, ids: set[str], names: dict[str, int], result: Measurement) -> None:
    endpoints: set[str] = set()
    buckets: dict[str, set[str]] = {RECOVERABLE: set(), AMBIGUOUS: set(), ABSENT: set()}
    for edge in graph_files.iter_edges(path):
        result.edges += 1
        dangling = False
        for end in ("source", "target"):
            value = edge.get(end)
            if value is None:
                continue
            endpoint = str(value)
            endpoints.add(endpoint)
            if endpoint not in ids:
                buckets[classify(endpoint, names)].add(endpoint)
                dangling = True
        result.dangling_edges += 1 if dangling else 0
    result.endpoints = len(endpoints)
    # Sorted here rather than at print time: two runs on the same graph must
    # produce byte-identical output, and a set's order is not stable across
    # processes.
    result.classified = {kind: sorted(found) for kind, found in buckets.items()}


def _counterpart(path: Path) -> Path | None:
    other = graph_files.counterpart(path)
    return other if other is not None and other.is_file() else None


def discover_graphs(repositories: Path) -> list[Path]:
    """Every per-repository graph under `repositories/`, one per clone, sorted.

    One file per repository even when a clone holds both forms, because
    measuring the same graph twice would double every count in the total while
    leaving the rate unchanged - the shape of error that reconciles internally
    and is wrong.
    """
    if not repositories.is_dir():
        return []
    found = []
    for clone in sorted(p for p in repositories.iterdir() if p.is_dir()):
        for name in GRAPH_NAMES:
            candidate = clone / "graphify-out" / name
            if candidate.is_file():
                found.append(candidate)
                break
    return found


def _percentage(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def _relative(path: Path) -> str:
    return str(path.relative_to(config.ROOT)) if path.is_relative_to(config.ROOT) else str(path)


def _read_lines(measurements: list[Measurement]) -> list[str]:
    """One line per file read, naming it and what it held.

    Every input file named, because a total is not a measurement until you can
    say which artefacts it came from - and because the counterpart line is what
    tells an operator that the number describes one of their two graph files.
    """
    lines = [f"Read {len(measurements)} per-repository graph(s):"]
    for found in measurements:
        if found.error:
            lines.append(f"  {_relative(found.path)}  UNREADABLE: {found.error}")
            continue
        state = "" if found.measurable else "  (not measurable: no nodes or no edges)"
        lines.append(
            f"  {_relative(found.path)}  {found.nodes:,} nodes  {found.edges:,} edges{state}"
        )
        if found.counterpart is not None:
            lines.append(
                f"    a counterpart exists and was NOT read: {_relative(found.counterpart)}"
            )
    return lines


def _sample_lines(measurements: list[Measurement], sample: int) -> list[str]:
    if sample <= 0:
        return []
    lines = []
    for kind in (RECOVERABLE, AMBIGUOUS):
        ids = sorted({e for m in measurements for e in m.classified.get(kind, ())})
        if ids:
            shown = ", ".join(ids[:sample])
            more = f" (+{len(ids) - sample:,} more)" if len(ids) > sample else ""
            lines.append(f"  {kind}, first {min(sample, len(ids))} by id: {shown}{more}")
    return lines


def report(measurements: list[Measurement], sample: int = 5) -> list[str]:
    """The whole report, as lines. Deterministic: every list is sorted."""
    lines = [
        "Dangling-endpoint recovery, measured on per-repository graphs before merging.",
        "",
        *_read_lines(measurements),
        "",
    ]
    usable = [m for m in measurements if m.measurable]
    edges = sum(m.edges for m in usable)
    endpoints = sum(m.endpoints for m in usable)
    dangling = sum(m.dangling for m in usable)
    dangling_edges = sum(m.dangling_edges for m in usable)
    lines.append(
        f"Dangling endpoints: {dangling:,} distinct, of {endpoints:,} endpoints referenced by "
        f"{edges:,} edges, on {dangling_edges:,} edge(s) ({_percentage(dangling_edges, edges)} "
        "of edges)."
    )
    for kind, meaning in (
        (RECOVERABLE, "the id names exactly one node in the same graph"),
        (AMBIGUOUS, "the id names more than one - never guessed, never recovered"),
        (ABSENT, "no node of that name in the graph; external or standard-library symbols"),
    ):
        total = sum(m.count(kind) for m in usable)
        lines.append(f"  {kind:<12} {total:>9,}  ({_percentage(total, dangling)})  - {meaning}")
    recoverable = sum(m.count(RECOVERABLE) for m in usable)
    lines.append("")
    if dangling:
        lines.append(
            f"Recovery rate: {_percentage(recoverable, dangling)} "
            f"({recoverable:,} of {dangling:,} dangling endpoints). "
            "Read the count beside the rate: a high rate over a handful of endpoints "
            "sizes nothing."
        )
        lines.extend(_sample_lines(measurements, sample))
    else:
        lines.append(
            "Recovery rate: not defined - no endpoint dangles in the graphs named above, "
            "so there is nothing here to recover."
        )
    return lines


def as_json(measurements: list[Measurement]) -> dict:
    """The same numbers, machine-readable, with the artefacts they came from."""
    usable = [m for m in measurements if m.measurable]
    return {
        "measured": "per-repository graphs, before merge-graphs",
        "predicate": "the id's final '::' segment, casefolded, against node id and local_id",
        "graphs": [
            {
                "path": _relative(m.path),
                "measurable": m.measurable,
                "error": m.error,
                "nodes": m.nodes,
                "edges": m.edges,
                "endpoints": m.endpoints,
                "dangling_edges": m.dangling_edges,
                **{kind: m.count(kind) for kind in (RECOVERABLE, AMBIGUOUS, ABSENT)},
            }
            for m in measurements
        ],
        "totals": {
            "graphs_measured": len(usable),
            "edges": sum(m.edges for m in usable),
            "endpoints": sum(m.endpoints for m in usable),
            "dangling_edges": sum(m.dangling_edges for m in usable),
            "dangling": sum(m.dangling for m in usable),
            **{
                kind: sum(m.count(kind) for m in usable)
                for kind in (RECOVERABLE, AMBIGUOUS, ABSENT)
            },
        },
    }


def _no_graphs_message(repositories: Path) -> str:
    """Why nothing was measured, and why the estate graph is not a substitute.

    Naming the merged graph explicitly rather than saying 'no graphs found':
    the store root almost always holds one, it is the file an operator reaches
    for next, and reading it is precisely the substitution that produced a clean
    zero on an estate that had thousands.
    """
    estate = ""
    for name in GRAPH_NAMES:
        candidate = config.ROOT / "graphify-out" / name
        if candidate.is_file():
            estate = (
                f" {_relative(candidate)} exists, and is not a substitute: it is post-merge, "
                "where `merge-graphs` has already turned every dangling endpoint into a node "
                "and a store's own layer merge has already dropped the rest. Measured there, "
                "this rate is zero by construction."
            )
            break
    return (
        f"No per-repository graph found under {_relative(repositories)} - nothing was "
        f"measured, and no rate is reported.{estate} Extract per repository first, from "
        "inside each clone."
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowledgestore dangling-endpoints",
        description=(
            "Measure what share of dangling edge endpoints name a node the graph already "
            "holds. Reads per-repository graphs before they are merged; writes nothing."
        ),
    )
    parser.add_argument(
        "--graph",
        type=Path,
        action="append",
        default=None,
        help="a graph to measure (repeatable); default: every repositories/*/graphify-out/graph.json",
    )
    parser.add_argument(
        "--repositories",
        type=Path,
        default=None,
        help="directory of clones to walk (default: the store's repositories/)",
    )
    parser.add_argument("--json", type=Path, default=None, help="also write the numbers here")
    parser.add_argument(
        "--sample", type=int, default=5, help="how many endpoint ids to name per class (default 5)"
    )
    return parser


def _resolve_inputs(arguments) -> tuple[list[Path], str]:
    """(graphs to measure, refusal). Exactly one of the two is non-empty."""
    if arguments.graph:
        missing = [p for p in arguments.graph if not p.is_file()]
        if missing:
            named = ", ".join(str(p) for p in sorted(missing, key=str))
            return [], f"No such graph file: {named} - nothing was measured."
        return list(arguments.graph), ""
    repositories = arguments.repositories or config.REPOSITORIES_DIR
    graphs = discover_graphs(repositories)
    return (graphs, "") if graphs else ([], _no_graphs_message(repositories))


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    graphs, refusal = _resolve_inputs(arguments)
    if refusal:
        print(refusal)
        return 1

    measurements = [measure_graph(path) for path in graphs]
    if not any(m.measurable for m in measurements):
        print("\n".join(_read_lines(measurements)))
        print(
            "\nNone of those graphs could be measured - every one is unreadable, has no "
            "nodes, or has no edges. No rate is reported: a rate computed from nothing "
            "reads exactly like a clean one."
        )
        return 1

    print("\n".join(report(measurements, arguments.sample)))
    if arguments.json:
        io.write_json(arguments.json, as_json(measurements), indent=2)
        print(f"\nWritten to {arguments.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
