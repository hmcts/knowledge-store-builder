"""The JSON Schema layer of the merged graph: nodes, edges, and what they replace.

Pure graph work: no filesystem, no configuration. The stage's `main` reads the
files, builds `Owned` values, and calls `add_layer`, which rebuilds the layer
from scratch each time (idempotence by reconstruction, as `packages` does).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from knowledgestore import kinds
from knowledgestore.json_schema import Entity
from knowledgestore.json_schema_refs import Index, Owned, Resolution, resolve

FORMAT = "json-schema"
_ORIGIN = FORMAT
_STORE_PREFIX = "repositories/"
_RESOLVED_HOW = ("local", "anchor", "by-id", "relative-id", "relative-path")


def node_id(repo: str, rel: str, pointer: str) -> str:
    return f"{repo}::jsonschema:{rel}#{pointer}"


def node_record(owned: Owned, entity: Entity) -> dict:
    # Key order is part of the byte-identical output (CLAUDE.md 1.3).
    metadata: dict = {"kind": entity.kind, "format": FORMAT, "pointer": entity.pointer}
    for key, value in entity.metadata.items():
        # A property's own `format` keyword (such as `date-time`) must not replace the
        # stage's `format`, which consumers read to know which parser wrote the node.
        metadata["schema_format" if key == "format" else key] = value
    if entity.kind == kinds.SCHEMA:
        metadata["basis"] = owned.basis
    local_id = f"jsonschema:{owned.rel}#{entity.pointer}"
    return {
        "id": f"{owned.repo}::{local_id}",
        "label": entity.label,
        "norm_label": entity.label.lower(),
        "file_type": "code",
        "source_file": owned.rel,
        "source_location": f"L{entity.line}",
        "repo": owned.repo,
        "_origin": _ORIGIN,
        "local_id": local_id,
        "metadata": metadata,
    }


def edge_record(
    source: str,
    target: str,
    relation: str,
    rel: str,
    line: int,
    context: str | None,
    target_pointer: str | None,
) -> dict:
    edge: dict = {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": "EXTRACTED",
        "confidence_score": 1.0,
        "source_file": rel,
        "source_location": f"L{line}",
        "weight": 1.0,
    }
    if context is not None:
        edge["context"] = context
    if target_pointer is not None:
        edge["target_pointer"] = target_pointer
    return edge


def owned_key(node: dict) -> tuple[str, str] | None:
    """The (repo, repository-relative path) a graphify node belongs to, if it names one."""
    source_file = node.get("source_file")
    if not isinstance(source_file, str) or not source_file:
        return None
    repo = node.get("repo")
    has_repo = isinstance(repo, str) and bool(repo)
    if source_file.startswith(_STORE_PREFIX):
        name, _, rest = source_file[len(_STORE_PREFIX) :].partition("/")
        # A node that names its repository is store-relative only when the path names the
        # same one: otherwise `repositories/foo/...` is a directory inside that repository.
        if name and rest and (not has_repo or name == repo):
            return (name, rest)
    if has_repo:
        return (str(repo), source_file)
    return None


def strip_layer(graph: dict) -> None:
    """Idempotence by reconstruction: drop everything this stage added before."""
    ours = {n["id"] for n in graph["nodes"] if n.get("_origin") == _ORIGIN}
    graph["nodes"] = [n for n in graph["nodes"] if n.get("_origin") != _ORIGIN]
    graph["links"] = [
        e for e in graph["links"] if e["source"] not in ours and e["target"] not in ours
    ]


def remove_graphify_nodes(graph: dict, owned: set[tuple[str, str]]) -> tuple[int, int, int]:
    """Drop graphify's keyword nodes for owned files; return (nodes, edges, semantic kept)."""
    removed: set[str] = set()
    kept = 0
    survivors = []
    for node in graph["nodes"]:
        if node.get("_origin") == "ast" and owned_key(node) in owned:
            removed.add(node["id"])
            continue
        if owned_key(node) in owned and node.get("_origin") != _ORIGIN:
            kept += 1
        survivors.append(node)
    links = [e for e in graph["links"] if e["source"] not in removed and e["target"] not in removed]
    edges_removed = len(graph["links"]) - len(links)
    _prune_hyperedges(graph, removed)
    graph["nodes"] = survivors
    graph["links"] = links
    return len(removed), edges_removed, kept


def _prune_hyperedges(graph: dict, removed: set[str]) -> None:
    """Drop removed ids from hyperedges, and a hyperedge left with no member.

    graphify keeps hyperedges in two slots, `graph.hyperedges` and a top-level
    `hyperedges`, and drops a hyperedge none of whose members survive.
    """
    if not removed:
        return
    holders = [graph, graph.get("graph")]
    for holder in holders:
        if not isinstance(holder, dict) or not isinstance(holder.get("hyperedges"), list):
            continue
        kept = []
        for hyperedge in holder["hyperedges"]:
            members = hyperedge.get("nodes") if isinstance(hyperedge, dict) else None
            if isinstance(members, list):
                survivors = [m for m in members if not (isinstance(m, str) and m in removed)]
                if not survivors:
                    continue
                hyperedge = {**hyperedge, "nodes": survivors}
            kept.append(hyperedge)
        holder["hyperedges"] = kept


@dataclass
class LayerReport:
    nodes_by_kind: dict[str, int] = field(default_factory=dict)
    edges_by_relation: dict[str, int] = field(default_factory=dict)
    resolved_by_how: dict[str, int] = field(default_factory=dict)
    unresolved: list[dict] = field(default_factory=list)
    cross_repository: int = 0
    self_references: int = 0
    graphify_removed: tuple[int, int, int] = (0, 0, 0)


def _unresolved(item: Owned, pointer: str, ref: object, found: Resolution, reason: str) -> dict:
    return {
        "repo": item.repo,
        "source_file": item.rel,
        "pointer": pointer,
        "ref": ref,
        "reason": reason,
        "candidates": list(found.candidates),
    }


def add_layer(graph: dict, owned: list[Owned], forks: frozenset[str]) -> LayerReport:
    """Replace graphify's keyword nodes for `owned` files with the layer; report what it did."""
    report = LayerReport()
    ordered = sorted(owned, key=lambda o: (o.repo, o.rel))
    strip_layer(graph)
    report.graphify_removed = remove_graphify_nodes(graph, {(o.repo, o.rel) for o in ordered})

    index = Index.build(ordered)
    nodes: list[dict] = []
    pointers: set[str] = set()
    kind_counts: Counter[str] = Counter()
    for item in ordered:
        for entity in item.walked.entities:
            nodes.append(node_record(item, entity))
            pointers.add(node_id(item.repo, item.rel, entity.pointer))
            kind_counts[entity.kind] += 1

    added: list[dict] = []
    how_counts: Counter[str] = Counter()
    unresolved: list[dict] = []
    for item in ordered:
        for entity in item.walked.entities:
            if entity.parent is None:
                continue
            added.append(
                edge_record(
                    node_id(item.repo, item.rel, entity.parent),
                    node_id(item.repo, item.rel, entity.pointer),
                    "contains",
                    item.rel,
                    entity.line,
                    None,
                    None,
                )
            )
        for ref in item.walked.refs:
            found = resolve(index, item, ref, forks)
            target = found.target
            if target is None:
                unresolved.append(_unresolved(item, ref.owner, ref.ref, found, found.how))
                continue
            target_id = node_id(target.repo, target.rel, target.pointer)
            if target_id not in pointers:
                unresolved.append(_unresolved(item, ref.owner, ref.ref, found, "no-such-pointer"))
                continue
            source_id = node_id(item.repo, item.rel, ref.owner)
            if source_id == target_id:
                report.self_references += 1
                continue
            how_counts[found.how] += 1
            added.append(
                edge_record(
                    source_id,
                    target_id,
                    ref.relation,
                    item.rel,
                    ref.line,
                    ref.context,
                    target.target_pointer if target.target_pointer != target.pointer else None,
                )
            )

    added.sort(key=lambda e: (e["source"], e["target"], e["relation"], e.get("context") or ""))
    edges: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for edge in added:
        key = (edge["source"], edge["target"], edge["relation"], edge.get("context") or "")
        if key not in seen:
            seen.add(key)
            edges.append(edge)

    graph["nodes"].extend(nodes)
    graph["links"].extend(edges)
    report.cross_repository = sum(
        1 for e in edges if e["source"].split("::", 1)[0] != e["target"].split("::", 1)[0]
    )
    report.nodes_by_kind = dict(sorted(kind_counts.items()))
    report.edges_by_relation = dict(sorted(Counter(e["relation"] for e in edges).items()))
    report.resolved_by_how = {k: how_counts[k] for k in _RESOLVED_HOW if how_counts[k]}
    unresolved.sort(
        key=lambda u: (u["repo"], u["source_file"], u["pointer"], str(u["ref"]), u["reason"])
    )
    report.unresolved = unresolved
    return report
