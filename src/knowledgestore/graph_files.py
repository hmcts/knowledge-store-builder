"""Which of a store's two graph files a stage actually read, and whether it is stale.

Every store gitignores the uncompressed `graphify-out/graph.json` and commits the
compressed `graph.json.gz`. `config.GRAPH_PATH` names the uncompressed one, so on
any checkout the plain path is either absent or whatever a discarded verification
run left behind — and a stage that reads it without saying so reports confidently
on an artefact nobody else has.

That is not hypothetical. On one estate a leftover `graph.json` made
`record-clustering` describe 42,572 communities over 785,610 nodes while the
committed `.gz` held 42,627 over 785,493. It exited 0 and every count in it
reconciled. The operator had to diff the two files by hand.

This module holds that comparison once, because the same class then reached
`summaries snapshot`, where it is worse: a snapshot is the remap's baseline, so a
snapshot keyed to the wrong graph mis-keys the entire carry of committed prose.
Two stages needing the same warning is the reason it lives here rather than in
either of them.

**It reports; it never adjudicates and never refuses.** An earlier version of the
`record-clustering` warning refused when both files existed, and that was wrong
for a reason worth keeping written down: the `.gz` is *tracked*, so it is present
from checkout on every refresh after the first, and both files therefore exist at
the exact moment these stages are meant to run. The refusal fired on the normal
case and made the stage unreachable. The operator knows which of their two files
is real; these stages do not.
"""

from __future__ import annotations

from pathlib import Path

from . import graph_stream


def counterpart(path: Path) -> Path | None:
    """The store's *other* graph file: the `.gz` beside a `.json`, or vice versa.

    Both directions, because a `--graph` flag may name either. `with_suffix(".json.gz")`
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

    An earlier decision refused to compare the two files at all, because
    "comparing 1.4 GB of graph is what this refuses to do". That was right about
    what it refused - a node-by-node *content* diff - and over-broad: counting is a
    different question, and it turns out not to need the graph in memory at all.
    """
    members: set[str] = set()
    clustered = 0
    for node in graph_stream.iter_array(path):
        community = node.get("community") if isinstance(node, dict) else None
        if community is not None:
            members.add(str(community))
            clustered += 1
    return len(members), clustered


def disagreement(described: Path, counts: tuple[int, int], remedy: str) -> str:
    """A line naming both files and both counts when they disagree. Empty when they agree.

    An advisory note can tell an operator that the other file exists and that this
    one might be stale. It cannot tell them whether it *is*, so on that estate the
    stage recorded 42,572 communities over 785,610 nodes from a leftover
    uncompressed graph while the committed artefact held 42,627 over 785,493, and
    the operator diffed them by hand.

    So this measures it. Silent-but-wrong becomes named-and-wrong, which is the
    whole difference: every count in that record reconciled, and described the
    wrong graph.

    `remedy` is the caller's, because the way out is stage-specific: one stage
    offers `--graph`, another can only tell the operator which file to remove or
    decompress. A shared message naming a flag the calling stage does not have
    would send an operator to a dead end, which is worse than saying less.
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
        f"One of them is stale. {remedy}"
    )


def counts_from_nodes(nodes) -> tuple[int, int]:
    """(communities, clustered nodes) from an already-loaded node list.

    `graph_counts` streams, which is right when the file is not otherwise being
    read. A stage that has already loaded the graph would be paying twice, and on
    the largest estate available the streamed read is 2.2s - not free. Same
    quantity, different source.
    """
    members = set()
    clustered = 0
    for node in nodes:
        community = node.get("community") if isinstance(node, dict) else None
        if community is not None:
            members.add(str(community))
            clustered += 1
    return len(members), clustered


def stale_note(described, nodes, artefact: str) -> str:
    """The disagreement line for a stage that reads the graph and writes `artefact`.

    One phrasing for every artefact-writing stage, because the operator's way out
    is the same in all of them and only the thing at risk differs.
    """
    remedy = (
        f"{artefact} will be built from {described.name}. Decompress the committed "
        f"graph over it, or remove the stale file, and re-run."
    )
    note = disagreement(described, counts_from_nodes(nodes), remedy)
    return f"{note}\n" if note else ""


def most_connected(path: Path, top: int = 10) -> list[tuple[str, str, int]]:
    """The `top` most connected nodes as (id, label, degree), most connected first.

    The check #112 asked for, and the reason it is worth a stage: after the first
    build of one estate the most central entities were `c()`, `push()`, `s()` and
    `a()` - minified helpers from two committed dependency bundles, which supplied
    36% of the AST nodes and 60% of the AST edges and formed the two largest
    communities. Centrality and community detection are both degree-driven, so a
    dense blob of interlinked vendored helpers wins every ranking, and topics,
    summaries and the explorer are all generated downstream of clusters.

    Nothing upstream catches it: graphify's `detect` honours `.gitignore`, and a
    zero-install dependency bundle is *deliberately committed*, so it is not
    ignored anywhere.

    This does not decide what is vendored, because size is not the signal -
    `values.schema.json`, `variables.tf` and `package.json` are all high
    node-count and are real declarations of an estate's own surface. Provenance is
    the signal, and a person reading ten names can tell in seconds what a rule
    cannot: whether these are things you would name if asked what the estate is
    built from.

    Two streamed passes, never a load. The first counts endpoints and holds one
    integer per id that appears in an edge; the second reads labels for the `top`
    ids only. Measured against a loaded read on the largest estate available, the
    streamed form was 2.2s and 0.04 GB where loading was 5.3s and 3.75 GB, which is
    why `status` can afford this behind a flag and could not afford it otherwise.
    """
    degree: dict[str, int] = {}
    for edge in iter_edges(path):
        for end in ("source", "target"):
            value = edge.get(end)
            if value is not None:
                key = str(value)
                degree[key] = degree.get(key, 0) + 1
    if not degree:
        return []
    # Sort by degree then id: two nodes of equal degree must not swap between runs.
    ranked = sorted(degree.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    wanted = {node_id for node_id, _ in ranked}
    labels: dict[str, str] = {}
    for node in graph_stream.iter_array(path, key="nodes"):
        node_id = str(node.get("id"))
        if node_id in wanted:
            labels[node_id] = str(node.get("label") or "")
            if len(labels) == len(wanted):
                break
    return [(node_id, labels.get(node_id, ""), count) for node_id, count in ranked]


def iter_edges(path: Path):
    """Stream a graph's edges, from either key.

    graphify writes `links` in node-link JSON and `edges` in its extract files.
    Reading one and silently finding none is indistinguishable from a graph with no
    edges, which would make every caller of this report an empty ranking.
    """
    found = False
    for edge in graph_stream.iter_array(path, key="links"):
        found = True
        yield edge
    if not found:
        yield from graph_stream.iter_array(path, key="edges")
