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

Commit the record beside the graph. A consumer's `status` can only report a
partitioner mismatch if the record travelled with the store.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable
from io import StringIO

from . import config
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


def main() -> int:
    # The uncompressed graph, because this runs at build time beside the
    # clustering that just wrote it - the committed .gz is produced later.
    nodes = io.read_json_dict(config.GRAPH_PATH).get("nodes", [])
    communities, clustered = _clustering(nodes)
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
        {"partitioner": partitioner, "communities": communities, "clustered_nodes": clustered},
        indent=2,
    )
    print(
        f"Recorded {PARTITIONER_NAMES[partitioner]} as the partitioner of the "
        f"{communities:,} communities over {clustered:,} nodes now in "
        f"{config.GRAPH_PATH.name} -> {config.CLUSTERING_RECORD_PATH}"
    )
    print(f"Determined by import, not inferred: {how}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
