"""Record which partitioner produced the communities in the graph.

Clustering is graphify's, and graphify picks its partitioner from the
environment: graspologic's Leiden where that library imports, networkx's Louvain
where it does not. Both are seeded and each is deterministic on its own, so the
choice is invisible in every artefact - and it is not a detail. Community ids key
every authored summary, so one corpus clustered in two environments yields two
partitions, and `summaries remap` then reports a retention collapse for a reason
that has nothing to do with the corpus. A store that pins its library version and
its hash seed still cannot reproduce its own clustering unless the partitioner
matches, and until now nothing said so.

    knowledgestore record-clustering    -> graphify-out/clustering-inputs.json

**Run it in the environment that clustered, right after clustering.** This stage
records what *this* environment offers; it cannot know what a clustering it did
not run was built with. `status` then compares the record against the environment
it is run in, reading only that small file - it must never load the graph.

That makes `hash_randomised` a proxy, and it can be wrong in both directions. Only
one of them is dangerous:

    recorded in a shell WITHOUT the seed, clustered WITH it
        -> hash_randomised: true, understating reproducibility. Alarming, and an
           alarm gets investigated.

    recorded in a shell WITH the seed, clustered WITHOUT it
        -> hash_randomised: false, claiming a clustering is reproducible when it
           is not. Silent, reassuring and wrong, which is the direction that
           costs someone a re-cluster they cannot repeat.

So invoke this from the build script that clusters, not as a later manual step -
an operator reported doing exactly that after being caught by the first direction,
and moving it into the build closes both. A record made in a different shell from
the clustering describes that shell, however carefully it is read.

Commit the record beside the graph. A consumer's `status` can only report a
partitioner mismatch if the record travelled with the store.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Callable
from io import StringIO
from pathlib import Path

from . import config
from . import graph_stream
from . import io


LEIDEN = "leiden"
LOUVAIN = "louvain"

# Named with the library that supplies each, because "Leiden" alone sends an
# operator looking for a setting rather than for a missing package.
PARTITIONER_NAMES = {
    LEIDEN: "Leiden (graspologic)",
    LOUVAIN: "Louvain (networkx)",
}


def _import_leiden() -> None:
    """Exactly the import graphify's `_partition` attempts, and nothing else.

    Kept to one line of real work so it cannot drift from the thing it stands in
    for. stdout is swallowed because graspologic writes ANSI escape sequences
    that corrupt the PowerShell 5.1 scroll buffer - graphify suppresses them
    around the same call for the same reason.
    """
    with contextlib.redirect_stdout(StringIO()):
        from graspologic.partition import leiden  # noqa: F401


def available_partitioner(probe: Callable[[], None] = _import_leiden) -> tuple[str | None, str]:
    """The partitioner graphify would use here, and how that was determined.

    Decided by graphify's own test - whether `graspologic.partition.leiden`
    imports - because that import inside a try/except ImportError *is* the whole
    choice. Anything inferred instead (a requirements file, a pinned extra, an
    installed version) can disagree with what actually ran, which is how a
    machine-dependent input stays invisible.

    Returns `(None, reason)` when the import failed in a way graphify does not
    catch: graphify would raise there rather than fall back, so that environment
    clusters with neither partitioner. Reporting Louvain for it would be a guess
    presented as a measurement.
    """
    try:
        probe()
    except ImportError as absent:
        return LOUVAIN, f"graspologic did not import ({absent})"
    except Exception as broken:  # noqa: BLE001 - see the docstring: not a fallback
        return None, f"importing graspologic raised {type(broken).__name__}: {broken}"
    return LEIDEN, "graspologic.partition.leiden imported"


def recorded_partitioner() -> str | None:
    """The partitioner the record names, or None when nothing recorded one.

    An absent record and a record naming something this library does not
    recognise are the same state - unknown - and neither may fall through to
    "agrees with this environment". An absent measurement printed as a clean
    result is the failure mode this library has shipped twice.
    """
    named = io.read_json_dict(config.CLUSTERING_RECORD_PATH).get("partitioner")
    return named if named in PARTITIONER_NAMES else None


def _clustering(nodes: list) -> tuple[int, int]:
    """How many communities the graph holds, and how many nodes carry one."""
    members = {str(node["community"]) for node in nodes if node.get("community") is not None}
    return len(members), sum(1 for node in nodes if node.get("community") is not None)


def other_graph_note() -> str:
    """A line naming the other graph file, when one exists. Never a refusal.

    `graph.json` is gitignored in every store and the compressed `.gz` is the
    committed artefact, so a record taken from the uncompressed file can describe
    a graph nobody will have. On one estate exactly that happened: a discarded
    verification run had left `graph.json` in the tree, and the record described
    42,572 communities over 785,610 nodes while the committed `.gz` held 42,627
    over 785,493, exiting 0 with real-looking counts.

    An earlier attempt at this **refused** when both files existed. That was
    wrong, and the estate that reported the original defect showed why: the `.gz`
    is *tracked*, so it is present from checkout on every refresh after the first,
    and both files therefore exist at the exact moment this stage is meant to run.
    The refusal fired on the normal case and made the stage unreachable.

    So the source is stated rather than adjudicated, and `--graph` lets an
    operator say which file they mean. That follows from the reasoning the
    refusal was built on: the operator knows which of their two files is real,
    and this stage does not.
    """
    described = config.GRAPH_PATH
    other = counterpart(described)
    if other is None or not other.is_file():
        return ""
    if described.suffix == ".gz":
        # The other direction, which this note used to get wrong: it called the
        # uncompressed file "normally the committed artefact", which is the exact
        # opposite of true, whenever an operator described the `.gz` explicitly.
        return (
            f"  {other.name} also exists and is what clustering writes. This record describes "
            f"{described.name}, the committed artefact - right if you mean what ships, and "
            "wrong if a newer clustering has not been published yet."
        )
    return (
        f"  {other.name} also exists and is normally the committed artefact. This record "
        f"describes {described.name}, which is correct immediately after clustering "
        "and wrong if that file is left from an earlier run - pass --graph to be explicit."
    )


def counterpart_disagreement(described: Path, counts: tuple[int, int]) -> str:
    """A line naming both files and both counts when they disagree. Empty when they agree.

    The advisory note above tells an operator that the other file exists and that
    this one might be stale. It cannot tell them whether it *is*, so on one estate
    the stage recorded 42,572 communities over 785,610 nodes from a leftover
    uncompressed graph while the committed artefact held 42,627 over 785,493 - and
    the operator had to diff the two by hand to find out.

    So this measures it. Silent-but-wrong becomes named-and-wrong, which is the
    whole difference: every count in that record reconciled, and described the
    wrong graph.

    Only when `--graph` was not passed. An operator who named a file has already
    said which one they mean, and re-reading the other would be cost for nothing.
    """
    other = counterpart(described)
    if other is None or not other.is_file():
        return ""
    try:
        other_counts = graph_counts(other)
    except (OSError, ValueError, EOFError) as error:
        # EOFError explicitly: a truncated `.gz` raises it and it is neither an
        # OSError nor a ValueError, so it escaped and took the stage down with it -
        # over a file the stage was not even asked to describe.
        # A counterpart that cannot be read is not this stage's business to fail
        # over - it is describing the file it was pointed at. Say so and move on.
        return f"  {other.name} exists but could not be read for comparison: {error}"
    if other_counts == counts:
        return ""
    return (
        f"  MISMATCH: {described.name} has {counts[0]:,} communities over {counts[1]:,} "
        f"clustered nodes; {other.name} has {other_counts[0]:,} over {other_counts[1]:,}. "
        f"One of them is stale, and this record describes {described.name}. If the "
        f"committed artefact is the real one, re-run with --graph {other.name}."
    )


def counterpart(path: Path) -> Path | None:
    """The store's *other* graph file: the `.gz` beside a `.json`, or vice versa.

    Both directions, because `--graph` may name either. `with_suffix(".json.gz")`
    handled only one: given `graph.json.gz` it produced `graph.json.json.gz`, a
    path that never exists, so the note about a stale counterpart went silent in
    exactly the case where the operator had been explicit about the compressed one.
    """
    if path.suffix == ".gz":
        return path.with_suffix("")
    if path.suffix == ".json":
        return path.with_name(path.name + ".gz")
    return None


def graph_counts(path: Path) -> tuple[int, int]:
    """(communities, clustered nodes) for one graph file.

    Reads and discards inside this function on purpose. The caller compares two
    graphs, and holding both node lists at once would double peak memory on an
    estate where one of them is already the largest thing in the process.

    Measured on the largest estate available - 785,493 nodes, 42,627 communities
    out of a 40 MB `graph.json.gz`:

        streamed   2.2s    0.04 GB peak RSS
        loaded     5.3s    3.75 GB peak RSS

    An operator measured the loading form at **9.2s and 5.17-6.07 GB** on the box
    that actually runs their build - twice my time and well over my memory - and
    that box was already swapping hard enough for one clustering run to take 77
    minutes against a normal 2.2. On a machine under that pressure a 5 GB peak can
    cost far more than the seconds it saves, and it would present as "the build is
    mysteriously slow" rather than as this check being expensive. They asked for
    streaming rather than a flag to switch the check off, which was the right ask.

    An earlier decision in this module refused to compare the two files at all,
    because "comparing 1.4 GB of graph is what this refuses to do". That was right
    about what it refused - a node-by-node *content* diff - and over-broad: counting
    is a different question, and it turns out not to need the graph in memory at all.
    """
    members: set[str] = set()
    clustered = 0
    for node in graph_stream.iter_array(path):
        community = node.get("community") if isinstance(node, dict) else None
        if community is not None:
            members.add(str(community))
            clustered += 1
    return len(members), clustered


def hash_randomisation() -> bool:
    """Whether this interpreter randomises hashes - i.e. the seed is NOT pinned.

    Read from `sys.flags` rather than the environment, deliberately.
    `PYTHONHASHSEED=random` is a legal value that reads like an instruction and
    leaves randomisation **on**; the environment variable would record it as set
    while the flag correctly reports unpinned.

    A proxy, and labelled as one: this is the process that recorded, which is the
    process that clustered only if the operator ran them together, as this
    stage's module docstring instructs.
    """
    return bool(sys.flags.hash_randomization)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knowledgestore record-clustering",
        description="Record which partitioner clustered the graph, and whether hashes were pinned.",
    )
    parser.add_argument(
        "--graph",
        type=Path,
        default=None,
        help="the graph to describe, when a store holds more than one candidate",
    )
    arguments = parser.parse_args(argv)
    explicit_graph = arguments.graph
    if explicit_graph is not None:
        config.configure(GRAPH_PATH=explicit_graph)

    # The uncompressed graph by default, because this runs at build time beside
    # the clustering that just wrote it. The committed .gz is last release's until
    # the build finishes, so preferring it would describe the previous graph.
    # Streamed, not loaded. This stage only ever counted the graph, so holding it
    # was pure cost: 2.2s at 0.04 GB against 5.3s at 3.75 GB on a 785,493-node
    # graph, and an operator measured the loading form at 9.2s and 5-6 GB on the
    # machine that actually runs their build - a box already swapping hard enough
    # that one clustering run took 77 minutes against a normal 2.2.
    communities, clustered = graph_counts(config.GRAPH_PATH)
    if not communities:
        print(
            f"No communities in {config.GRAPH_PATH}. Cluster the graph before recording - "
            "a record written over an unclustered graph names a partitioner for a "
            "clustering that does not exist, and `graphify cluster-only` can report "
            "success without persisting one.",
            file=sys.stderr,
        )
        return 1

    partitioner, how = available_partitioner()
    if partitioner is None:
        print(
            f"Cannot say which partitioner this environment uses: {how}. graphify catches "
            "ImportError and falls back to Louvain; it does not catch this, so it would "
            "fail here rather than cluster. Nothing recorded.",
            file=sys.stderr,
        )
        return 1

    # Counts as corroboration for a human, never for an automatic comparison:
    # `deployments` and `gherkin` legitimately add nodes and communities after
    # clustering, so a later disagreement is normal and a check built on it
    # would cry wolf. What this file is *for* is the partitioner.
    io.write_json(
        config.CLUSTERING_RECORD_PATH,
        {
            "partitioner": partitioner,
            "communities": communities,
            "clustered_nodes": clustered,
            # Which file these counts came from, so a reader never has to guess
            # which of a store's two graph files a record describes.
            "described": config.GRAPH_PATH.name,
            # Whether the recording interpreter randomised hashes. Without this,
            # a store that clustered unseeded is indistinguishable from one that
            # did not - and the community count cannot stand in for it, because
            # a re-cluster can return an identical count with different
            # membership. Measured: 4 of 12 unstable communities did exactly that.
            "hash_randomised": hash_randomisation(),
        },
        indent=2,
    )
    print(
        f"Recorded {PARTITIONER_NAMES[partitioner]} as the partitioner of the "
        f"{communities:,} communities over {clustered:,} nodes now in "
        f"{config.GRAPH_PATH.name} -> {config.CLUSTERING_RECORD_PATH}"
    )
    print(f"Determined by import, not inferred: {how}.")
    # Both of the counterpart hints below are answers to "which file did you mean",
    # so an operator who passed --graph has already answered and gets neither. That
    # is also what keeps the comparison's cost off the explicit path.
    if explicit_graph is None:
        disagreement = counterpart_disagreement(config.GRAPH_PATH, (communities, clustered))
        if disagreement:
            # Flushed first: stdout buffers when piped, so without this the finding
            # arrives above the lines it qualifies and reads as being about nothing.
            sys.stdout.flush()
            print(disagreement, file=sys.stderr)
    if hash_randomisation():
        print(
            "  Hash randomisation was ON in this process, so these communities are not "
            "reproducible even with the same partitioner. Cluster with PYTHONHASHSEED=0."
        )
    note = other_graph_note()
    if note:
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
