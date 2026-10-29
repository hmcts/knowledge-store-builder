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
"""

from __future__ import annotations

from . import kinds

ORIGIN = "repository"


def repository_id(repo: str) -> str:
    return f"{repo}::repository"


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


def ensure_repository_node(graph: dict, repo: str, known: set[str] | None = None) -> str:
    """Make sure `repo` has its repository node and return the node's id.

    `known` is the set of ids a caller that calls this in a loop already holds, so
    the graph is not rescanned per call; it is updated when a node is added. Left
    out, the graph is scanned once for this call.
    """
    node_id = repository_id(repo)
    if known is None:
        known = {n["id"] for n in graph["nodes"]}
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
