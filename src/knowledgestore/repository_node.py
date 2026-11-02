"""The node that stands for a repository, shared by every stage that needs one.

An edge whose meaning is "this repository" - a service deploys it, a repository
uses a module - must end on something that *is* the repository. Both stages used
to stand for it with the lowest node id the repository held, which is whatever
sorts first: a pipeline file, a symbol, a sentence of a README. "Service X
deploys <that sentence>" reads as a fact and is not one.

Ownership and idempotence. No enrichment stage owns this node. Each of them strips
its own layer by `_origin` before it rebuilds, so a node tagged with a stage's
origin would be deleted by that stage while the other stage's edges still pointed
at it. The node carries its own origin, `ORIGIN`, which no stage strips, and
`ensure_repository_node` creates it only when its id is absent. Two stages, either
order, or a second run of either one therefore leave exactly one node, and the
graph byte-identical - with one more step, `settle_repository_nodes`: a node
created mid-run sits wherever the stage happened to need it first, and on the
next run, already present, it would sit somewhere else. Each stage ends by moving
every repository node to the tail of the node list, sorted by id, so the position
does not depend on which run created the node.

Neither stage strips the node, so a stage that stops linking a repository would
leave it behind at degree 0. Each stage therefore also ends with
`prune_unlinked_repository_nodes`: what survives is what some stage's edges still
use. A fresh merge rebuilds the graph without the node, and with it the edges that
used it, so nothing outlives the repository it described.

The id is one graphify cannot produce (`SUFFIX`), and a node at it that this module
did not create is refused, never adopted (#549). A graph written under the old id is
migrated by `migrate_repository_nodes` when either stage next runs.
"""

from __future__ import annotations

from . import kinds

ORIGIN = "repository"

# What follows the `<repo>::` prefix. graphify builds node ids from word characters
# and underscores only (`graphify.ids.normalize_id` replaces every run of
# `[^\w]` with `_`), so a colon after the prefix is a shape its extraction cannot
# emit. The id used to be `<repo>::repository`, which a class named `Repository`
# produces exactly, and the stages then adopted that class as the repository (#549).
SUFFIX = "repository:self"
LEGACY_SUFFIX = "repository"


class ForeignNodeError(ValueError):
    """A node that is not a repository node holds a repository node's id."""


def repository_id(repo: str) -> str:
    return f"{repo}::{SUFFIX}"


def _legacy_id(repo: str) -> str:
    return f"{repo}::{LEGACY_SUFFIX}"


def _node(repo: str) -> dict:
    return {
        "id": repository_id(repo),
        "label": repo,
        "norm_label": repo.lower(),
        "file_type": "concept",
        "source_file": None,
        "source_location": None,
        "repo": repo,
        "_origin": ORIGIN,
        "local_id": "repository",
        "metadata": {"kind": kinds.REPOSITORY},
    }


def refuse_foreign_nodes(graph: dict) -> None:
    """Raise `ForeignNodeError` if a node that is not ours holds a repository id.

    The stages call this once, when they load the graph, because `ensure_repository_node`
    is called in a loop with a `known` set and cannot afford a scan per call.
    """
    tail = f"::{SUFFIX}"
    for node in graph["nodes"]:
        if node["id"].endswith(tail) and node.get("_origin") != ORIGIN:
            raise ForeignNodeError(
                f"node {node['id']!r} (_origin {node.get('_origin')!r}) holds the id of a "
                "repository node, which only this library creates; refusing to adopt it. "
                "Rebuild the graph, and report this if it recurs."
            )


def migrate_repository_nodes(graph: dict) -> None:
    """Rewrite repository nodes written under the old id to the current one.

    Called by each stage on load, before it strips its own layer: the old node is
    ours (`_origin` is `ORIGIN`), and every edge that ends on it is ours too, whichever
    stage wrote it, so they are retargeted together. A node that merely shares the old
    id - a class named `Repository` - has another `_origin`, is not touched, and its
    edges stay where they are. A foreign node already at the current id is refused.
    """
    refuse_foreign_nodes(graph)
    moved: dict[str, str] = {}
    present = {n["id"] for n in graph["nodes"]}
    keep = []
    for node in graph["nodes"]:
        repo = node.get("repo")
        if (
            node.get("_origin") == ORIGIN
            and isinstance(repo, str)
            and node["id"] == _legacy_id(repo)
        ):
            moved[node["id"]] = repository_id(repo)
            if repository_id(repo) in present:
                continue  # already migrated once; this is the stale twin
            node["id"] = repository_id(repo)
            present.add(node["id"])
        keep.append(node)
    if not moved:
        return
    graph["nodes"] = keep
    for edge in graph["links"]:
        edge["source"] = moved.get(edge["source"], edge["source"])
        edge["target"] = moved.get(edge["target"], edge["target"])


def ensure_repository_node(graph: dict, repo: str, known: set[str] | None = None) -> str:
    """Make sure `repo` has its repository node and return the node's id.

    `known` is the set of ids a caller that calls this in a loop already holds, so
    the graph is not rescanned per call; it is updated when a node is added. Left
    out, the graph is scanned once for this call, and a node at the id that is not
    ours raises `ForeignNodeError`. A caller that passes `known` has called
    `refuse_foreign_nodes` on load.
    """
    node_id = repository_id(repo)
    if known is None:
        known = {n["id"] for n in graph["nodes"]}
        refuse_foreign_nodes(graph)
    if node_id not in known:
        graph["nodes"].append(_node(repo))
        known.add(node_id)
    return node_id


def settle_repository_nodes(graph: dict) -> None:
    """Move every repository node to the end of the node list, sorted by id.

    Called by each stage once its layer is written. Where a node sits is an
    accident of when it was first needed, and the graph is committed output that
    two runs must reproduce byte for byte.
    """
    nodes = graph["nodes"]
    ours = sorted((n for n in nodes if n.get("_origin") == ORIGIN), key=lambda n: n["id"])
    if ours:
        graph["nodes"] = [n for n in nodes if n.get("_origin") != ORIGIN] + ours


def prune_unlinked_repository_nodes(graph: dict) -> None:
    """Drop repository nodes no edge reaches.

    Neither stage strips the node, so when a stage re-runs and no longer links a
    repository (its deployment repository dropped, its modules removed) the node
    would stay at degree 0 and be listed by the explorer as a repository nothing
    relates to. Called after `settle_repository_nodes`, so what is left is what
    some stage's edges still use.
    """
    linked = {e["source"] for e in graph["links"]} | {e["target"] for e in graph["links"]}
    graph["nodes"] = [n for n in graph["nodes"] if n.get("_origin") != ORIGIN or n["id"] in linked]
