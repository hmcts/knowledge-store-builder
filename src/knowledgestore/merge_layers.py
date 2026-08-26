"""Merge the AST and semantic layers without fabricating relationships.

    knowledgestore merge-layers   -> graphify-out/.graphify_extract.json

The last of the three fan-out seams, after `chunk-plan` and `merge-chunks`.

## What this replaces, and why it is not a refactor

The documented route merges the two layers with a concatenation that keeps the
AST node on an id collision and **concatenates the semantic layer's edges
anyway**:

    seen = {n['id'] for n in ast['nodes']}
    merged_nodes = list(ast['nodes'])
    for n in sem['nodes']:
        if n['id'] not in seen:          # else the semantic node vanishes
            merged_nodes.append(n)
    merged_edges = ast['edges'] + sem['edges']   # ...but its edges are kept

Those edges still name the discarded id, which now resolves to the AST node. So
every relationship the semantic layer asserted about one entity is silently
re-pointed at an unrelated one. The result builds cleanly and nothing dangles,
because the id exists. The graph simply asserts relationships that were never in
the corpus - the failure a knowledge store can least afford, and the hardest to
detect afterwards.

Measured on a 500k-node AST layer against a 46k-node semantic layer: 98 colliding
ids, every one with disagreeing labels, describing different files, carrying 311
semantic edges. Reproduced to the unit across two different semantic id schemes,
because the mechanism is the id *format* and not the scheme: the extraction spec
drops the file extension, so a component and its template are assigned one id by
design.

## What this does instead

**Same label** - the two layers named the same thing. The semantic node is
dropped and its edges keep pointing at the surviving id, which is correct: there
is one entity and both layers found it.

**Different labels** - two distinct entities were assigned one id. The semantic
node is **kept under a disambiguated id** and its edges are re-pointed to it. No
evidence is discarded and no edge is left resolving against an entity it was never
about.

Renaming rather than dropping is deliberate. Dropping the edges would fix the
fabrication and lose 311 real relationships; keeping the node loses nothing. It
also changes no id that any store has committed, because on the current route
these semantic nodes are *absent* from the output altogether - so the ids this
introduces are new rather than moved.

**It does not change the stem basis.** A stem that identified a file uniquely
within the corpus - extension included - would remove this class rather than
resolve instances of it, and would also change `spec_stem` in `merge_chunks`,
which generates ids committed in more than one store. That is an output change
consumers see and a decision for the maintainer; this stage is written so it
remains available rather than foreclosed.

## Nothing is discarded without being counted

Every number this prints is a decision the stage made. `collisions_same_label`
and `collisions_different_label` are reported separately because the first is a
benign duplicate and the second is the dangerous case, and a single "collisions"
total would hide the ratio between them. `edges_dropped` is the residue: an edge
whose endpoint exists in neither layer cannot be re-pointed and is not guessed at.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config
from . import io
from . import telemetry


def _nodes_and_edges(payload: dict) -> tuple[list[dict], list[dict]]:
    """A layer's nodes and edges, tolerating either key for the edge list.

    graphify writes `links` in node-link JSON and `edges` in its extract files,
    and both forms reach this stage depending on which part produced the layer.
    Reading one and silently seeing zero edges is the failure this avoids.
    """
    nodes = [n for n in payload.get("nodes", []) if isinstance(n, dict)]
    edges = payload.get("edges")
    if not isinstance(edges, list):
        edges = payload.get("links")
    return nodes, [e for e in (edges or []) if isinstance(e, dict)]


def _label(node: dict) -> str:
    """A node's label, normalised for comparison.

    Structural nodes carry no label - newer graphify emits package-hierarchy nodes
    with neither `label` nor `source_file` - so this must tolerate absence rather
    than assume a string.
    """
    return str(node.get("label") or "").strip()


def _free_id(candidate: str, taken: set[str]) -> str:
    """`candidate` with the lowest numeric suffix that is not already used."""
    if candidate not in taken:
        return candidate
    suffix = 2
    while f"{candidate}_{suffix}" in taken:
        suffix += 1
    return f"{candidate}_{suffix}"


def repository_of(source_file: str) -> str:
    """The repository a `source_file` belongs to, or "" when it names none.

    Exact rather than inferred, which is what makes the rewrite below a rewrite
    and not a guess: the path either carries a `repositories/<name>/` segment or it
    does not.
    """
    parts = Path(str(source_file or "")).parts
    for index, part in enumerate(parts):
        if part == "repositories" and index + 1 < len(parts):
            return parts[index + 1]
    return ""


def namespace_by_repository(
    nodes: list[dict], edges: list[dict], counters: dict
) -> tuple[list[dict], list[dict]]:
    """Prefix AST ids with their repository, and rewrite edges through the remap.

    graphify derives AST ids from the file path but drops the leading
    `repositories/<repo>/` segment for declarations *inside* a file. So a variable
    declared at the same relative path in two repositories - say
    `<repo>/infra/variables.tf` - gets one id in **both**, and in every other
    repository that follows the same layout.

    Measured on one estate: of M distinct AST ids, several hundred were used more
    than once, covering thousands of node records, and most of those spanned more
    than one repository. The worst single id appeared once per repository across
    most of the estate.

    It is caused by the estate being *well run*. Where every service declares the
    same variables in the same place because that is the house convention, **the
    more consistent an organisation's conventions, the worse the collision** - and
    it scales with repository count rather than with corpus size.

    The consequence is not a duplicate. A build that dedupes by id keeps one record
    and re-points every edge at it, so one shared declaration becomes a single node
    adjacent to every service that declares it - immediately the highest-degree
    node in the graph. Centrality and community detection are both degree-driven,
    so community detection then reports those independent services as one
    tightly-coupled cluster, and topics, summaries and the explorer are all
    generated downstream of clusters.

    **The two conditions that make this exact**, stated because they are what
    licenses the rewrite and they do not hold for every layer:

    1. `extract()` runs once per repository, so an AST edge is always produced from
       a single file and both endpoints belong to that file's repository.
    2. Every node carries a `source_file`.

    Where the second does not hold - a structural node with no `source_file`, which
    newer graphify emits - the id is left alone and counted rather than guessed at.
    Where an edge's endpoints resolve to different repositories, condition 1 has
    been violated for that edge, so it is counted and left pointing at the
    un-namespaced ids rather than being attributed to one side.

    Never applied to a layer whose edges can legitimately span repositories, and
    never applied twice: an id already carrying `::` is skipped, because
    re-namespacing produces `repo::repo::id` and sets every repository attribute to
    the wrong value - a hazard this estate has already met from running a merge on
    an already-merged graph.
    """
    remap: dict[str, str] = {}
    renamed: list[dict] = []
    for node in nodes:
        node_id = str(node.get("id"))
        repository = repository_of(node.get("source_file") or "")
        if not repository or "::" in node_id:
            counters["ast_not_namespaced"] += 1
            renamed.append(node)
            continue
        new_id = f"{repository}::{node_id}"
        if new_id != node_id:
            remap[node_id] = new_id
            counters["ast_namespaced"] += 1
        moved = dict(node)
        moved["id"] = new_id
        renamed.append(moved)

    rewritten: list[dict] = []
    for edge in edges:
        source, target = str(edge.get("source")), str(edge.get("target"))
        new_source, new_target = remap.get(source, source), remap.get(target, target)
        source_repo = new_source.split("::", 1)[0] if "::" in new_source else ""
        target_repo = new_target.split("::", 1)[0] if "::" in new_target else ""
        if source_repo and target_repo and source_repo != target_repo:
            # Condition 1 does not hold for this edge. Attributing it to either
            # side would be the guess this function exists to avoid.
            counters["ast_edges_spanning_repositories"] += 1
            rewritten.append(edge)
            continue
        moved = dict(edge)
        moved["source"], moved["target"] = new_source, new_target
        rewritten.append(moved)
    return renamed, rewritten


def _merge_nodes(
    ast_nodes: list[dict], sem_nodes: list[dict], counters: dict
) -> tuple[list[dict], dict[str, dict], dict[str, str]]:
    """The merged node list, an id index, and the ids that moved.

    Split out of `merge` because Sonar measured its cognitive complexity at 16
    against a limit of 15. The node walk is the separable half: it decides what
    exists, and the edge walk then decides what resolves.

    `renamed` holds only ids that moved, so an unchanged endpoint costs no lookup.
    """
    by_id = {str(n.get("id")): n for n in ast_nodes if n.get("id") is not None}
    taken = set(by_id)
    merged_nodes = list(ast_nodes)
    renamed: dict[str, str] = {}

    for node in sem_nodes:
        node_id = str(node.get("id"))
        existing = by_id.get(node_id)
        if existing is None:
            by_id[node_id] = node
            taken.add(node_id)
            merged_nodes.append(node)
            continue
        if _label(existing) == _label(node):
            # One entity, found by both layers. Its edges already resolve here.
            counters["collisions_same_label"] += 1
            continue
        # Two entities under one id. Keep both; the semantic one moves.
        counters["collisions_different_label"] += 1
        new_id = _free_id(f"sem_{node_id}", taken)
        moved = dict(node)
        moved["id"] = new_id
        moved["original_id"] = node_id
        renamed[node_id] = new_id
        taken.add(new_id)
        by_id[new_id] = moved
        merged_nodes.append(moved)
    return merged_nodes, by_id, renamed


def merge(ast: dict, semantic: dict) -> tuple[dict, dict]:
    """The merged graph and the counters describing what was decided.

    Deterministic: layers are walked in the order they were written and renames
    take the lowest free suffix, so two runs on the same inputs produce identical
    bytes.
    """
    ast_nodes, ast_edges = _nodes_and_edges(ast)
    sem_nodes, sem_edges = _nodes_and_edges(semantic)

    counters = {
        "ast_nodes": len(ast_nodes),
        "semantic_nodes": len(sem_nodes),
        "ast_namespaced": 0,
        "ast_not_namespaced": 0,
        "ast_edges_spanning_repositories": 0,
        "collisions_same_label": 0,
        "collisions_different_label": 0,
        "edges_repointed": 0,
        "edges_dropped": 0,
    }

    # Before merging, because an un-namespaced AST id is what the semantic layer
    # then collides with, and because a fused hub node cannot be unfused afterwards.
    ast_nodes, ast_edges = namespace_by_repository(ast_nodes, ast_edges, counters)
    merged_nodes, by_id, renamed = _merge_nodes(ast_nodes, sem_nodes, counters)

    merged_edges = list(ast_edges)
    for edge in sem_edges:
        source, target = str(edge.get("source")), str(edge.get("target"))
        moved = dict(edge)
        if source in renamed:
            moved["source"] = renamed[source]
        if target in renamed:
            moved["target"] = renamed[target]
        if moved.get("source") != edge.get("source") or moved.get("target") != edge.get("target"):
            counters["edges_repointed"] += 1
        if str(moved["source"]) not in by_id or str(moved["target"]) not in by_id:
            # Neither layer holds this endpoint. Guessing at it is how a
            # concatenation invents relationships in the first place.
            counters["edges_dropped"] += 1
            continue
        merged_edges.append(moved)

    merged = dict(ast)
    merged["nodes"] = merged_nodes
    merged["edges"] = merged_edges
    merged.pop("links", None)
    counters["nodes"] = len(merged_nodes)
    counters["edges"] = len(merged_edges)
    return merged, counters


def report(counters: dict) -> str:
    """Every decision the stage made, as lines an operator reads rather than scans.

    The two collision counts are separate on purpose. Same-label is a duplicate
    both layers found; different-label is two entities that would have been fused.
    A single total would hide which one an estate has.
    """
    lines = [
        f"Merged {counters['ast_nodes']:,} AST and {counters['semantic_nodes']:,} semantic "
        f"nodes -> {counters['nodes']:,} nodes, {counters['edges']:,} edges",
        f"  {counters['collisions_same_label']:,} id collisions with the same label "
        "(one entity, both layers found it - semantic copy dropped)",
        f"  {counters['collisions_different_label']:,} id collisions with DIFFERENT labels "
        "(two entities under one id - semantic node kept under a new id)",
        f"  {counters['edges_repointed']:,} semantic edges re-pointed to a renamed node",
        f"  {counters['edges_dropped']:,} edges dropped: an endpoint exists in neither layer",
        f"  {counters.get('ast_namespaced', 0):,} AST ids namespaced by repository "
        "(graphify drops the repository segment for declarations inside a file)",
        f"  {counters.get('ast_not_namespaced', 0):,} AST ids left alone: no source_file to attribute, "
        "or already namespaced",
        f"  {counters.get('ast_edges_spanning_repositories', 0):,} AST edges span two repositories and "
        "were left un-namespaced rather than attributed to one side",
    ]
    if counters["collisions_different_label"]:
        lines.append(
            "  Without the rename those "
            f"{counters['collisions_different_label']:,} nodes would have been discarded and "
            f"their {counters['edges_repointed']:,} edges left pointing at unrelated entities."
        )
    return "\n".join(lines)


def layer_measurements(counters: dict) -> dict[str, int]:
    """The four counts a later build needs to see this build's shape move.

    The two input layers are recorded as counts and never as the ratio between
    them, even though the ratio is what an operator reads. Two estates measured
    that ratio at roughly 0.5:1 and 57:1 - a factor of a hundred - so it is only
    meaningful against a store's own previous build, and computing the previous
    ratio needs the previous build's numerator *and* denominator. A recorded
    `57.5` would leave a later reader unable to tell an AST layer that doubled
    from a semantic layer that halved.
    """
    return {
        "layers.ast_nodes": counters["ast_nodes"],
        "layers.semantic_nodes": counters["semantic_nodes"],
        "layers.merged_nodes": counters["nodes"],
        "layers.merged_edges": counters["edges"],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore merge-layers",
        description="Merge the AST and semantic layers without re-pointing edges.",
    )
    parser.add_argument("--ast", type=Path, help="default: graphify-out/.graphify_ast.json")
    parser.add_argument(
        "--semantic", type=Path, help="default: graphify-out/.graphify_semantic_new.json"
    )
    parser.add_argument("--out", type=Path, help="default: graphify-out/.graphify_extract.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    directory = config.GRAPH_PATH.parent
    ast_path = arguments.ast or (directory / ".graphify_ast.json")
    sem_path = arguments.semantic or (directory / ".graphify_semantic_new.json")
    destination = arguments.out or (directory / ".graphify_extract.json")

    for path in (ast_path, sem_path):
        if not path.is_file():
            print(f"Layer not found: {path}", file=sys.stderr)
            return 1

    ast = io.read_json_dict(ast_path)
    semantic = io.read_json_dict(sem_path)
    ast_nodes, _ = _nodes_and_edges(ast)
    sem_nodes, _ = _nodes_and_edges(semantic)
    if not ast_nodes or not sem_nodes:
        # A layer that read as empty is an upstream failure, and merging it would
        # produce a smaller graph that looks like a successful run.
        print(
            f"Refusing to merge: {ast_path.name} holds {len(ast_nodes):,} nodes and "
            f"{sem_path.name} holds {len(sem_nodes):,}. An empty layer is an upstream "
            "failure, and merging it would look like a success.",
            file=sys.stderr,
        )
        return 1

    merged, counters = merge(ast, semantic)
    # Through `io.write_json` rather than a raw write: it validates the target and
    # is the one place in this library that writes JSON, so a second raw write site
    # would need its own guard and would drift from that one.
    io.write_json(destination, merged)
    print(report(counters))
    print(f"-> {destination}")
    # The layer sizes are the numbers #116 has to be decided on, and they are
    # only interpretable against this store's own last build.
    telemetry.record(layer_measurements(counters))
    return 0
