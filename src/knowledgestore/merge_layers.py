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
import json
import sys
from pathlib import Path

from . import config
from . import io


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
        "collisions_same_label": 0,
        "collisions_different_label": 0,
        "edges_repointed": 0,
        "edges_dropped": 0,
    }

    by_id = {str(n.get("id")): n for n in ast_nodes if n.get("id") is not None}
    taken = set(by_id)
    merged_nodes = list(ast_nodes)
    # Only ids that moved appear here, so an unchanged endpoint costs no lookup.
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
    ]
    if counters["collisions_different_label"]:
        lines.append(
            "  Without the rename those "
            f"{counters['collisions_different_label']:,} nodes would have been discarded and "
            f"their {counters['edges_repointed']:,} edges left pointing at unrelated entities."
        )
    return "\n".join(lines)


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
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    print(report(counters))
    print(f"-> {destination}")
    return 0
