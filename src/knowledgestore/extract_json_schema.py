"""The JSON Schema layer of the merged graph: nodes, edges, and what they replace.

The graph functions are pure: no filesystem, no configuration. The stage's `main`
reads the files, builds `Owned` values, and calls `add_layer`, which rebuilds the
layer from scratch each time (idempotence by reconstruction, as `packages` does).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from knowledgestore import config, extract_ast, graph_files, io, kinds, telemetry
from knowledgestore.json_schema import (
    Entity,
    SchemaReadError,
    load_with_lines,
    recognise,
    walk,
)
from knowledgestore.json_schema_refs import Index, Owned, Resolution, resolve

FORMAT = "json-schema"
_ORIGIN = FORMAT
_STORE_PREFIX = "repositories/"
_KIND_ORDER = (kinds.SCHEMA, kinds.DEFINITION, kinds.FIELD)
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
    report.nodes_by_kind = {k: kind_counts[k] for k in _KIND_ORDER if kind_counts[k]}
    report.edges_by_relation = dict(sorted(Counter(e["relation"] for e in edges).items()))
    report.resolved_by_how = {k: how_counts[k] for k in _RESOLVED_HOW if how_counts[k]}
    unresolved.sort(
        key=lambda u: (u["repo"], u["source_file"], u["pointer"], str(u["ref"]), u["reason"])
    )
    report.unresolved = unresolved
    return report


# --- the stage ------------------------------------------------------------

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
REPORT_NOTE_MARKER = "<!-- knowledgestore: json-schema layer -->"
UNRESOLVED_SHOWN = 20
_MIB = 1024 * 1024
_BASES = ("meta-schema", "schema-keywords", "declared-id")
_SKIP_REASONS = ("malformed", "too-large", "outside-repositories")
_REASONS = (*_SKIP_REASONS, "truncated", "not-a-schema")
_UNRESOLVED_REASONS = (
    "meta-schema",
    "external",
    "ambiguous",
    "no-such-pointer",
    "unrecognised-target",
    "malformed",
)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore json-schema",
        description="Read JSON Schema documents into schema, definition and field nodes "
        "with resolved $ref edges, replacing graphify's keyword nodes for them.",
    )
    parser.add_argument(
        "--detect",
        type=Path,
        help="the extractor's content set (default: graphify-out/.graphify_detect.json)",
    )
    parser.add_argument(
        "--files",
        type=Path,
        help="a newline-delimited path list to read instead of the content set",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help="skip, by name, any file larger than this (default: 8 MiB)",
    )
    parser.add_argument(
        "--unresolved",
        type=Path,
        help="write every unresolved $ref, sorted, to this JSON file",
    )
    return parser.parse_args(argv)


def _repo_relative(path: Path, repo: str, corpus: Path) -> str:
    """The POSIX path of `path` inside the clone `repo`."""
    try:
        base = corpus.resolve()
    except OSError:  # pragma: no cover - an unresolvable corpus is caught upstream
        base = corpus
    return "/".join(path.resolve().relative_to(base).parts[1:]) if repo else path.as_posix()


def read_owned(
    groups: dict[str, list[Path]], corpus: Path, max_bytes: int
) -> tuple[list[Owned], dict]:
    """Read every file the layer might own; say what was skipped, and why.

    The returned dict holds, per reason, `{"count": n, "names": [...]}` with the
    names sorted, plus `"basis"`, the count of owned files by recognition basis.
    Only `malformed`, `too-large` and `outside-repositories` are skips. A
    `truncated` file stays in the layer (its walk stopped at the depth limit) and
    a `not-a-schema` file is counted only: graphify keeps it.
    """
    names: dict[str, list[str]] = {reason: [] for reason in _REASONS}
    bases: Counter[str] = Counter()
    not_a_schema = 0
    owned: list[Owned] = []
    for repo in sorted(groups):
        entries = sorted(
            ((_repo_relative(p, repo, corpus), p) for p in groups[repo]),
            key=lambda entry: (entry[0], str(entry[1])),
        )
        for rel, path in entries:
            name = f"{repo}/{rel}" if repo else rel
            if not repo:
                names["outside-repositories"].append(name)
                continue
            try:
                # Size first: an oversized file is never read.
                if path.stat().st_size > max_bytes:
                    names["too-large"].append(name)
                    continue
                text = path.read_bytes().decode("utf-8")
                document, lines = load_with_lines(text)
            except (OSError, UnicodeDecodeError, SchemaReadError):
                names["malformed"].append(name)
                continue
            basis = recognise(document)
            if basis is None:
                not_a_schema += 1
                continue
            walked = walk(document, lines, rel)
            if walked.truncated:
                names["truncated"].append(name)
            bases[basis] += 1
            owned.append(Owned(repo, rel, basis, walked))
    info: dict = {
        reason: {"count": len(found), "names": sorted(found)} for reason, found in names.items()
    }
    info["not-a-schema"] = {"count": not_a_schema, "names": []}
    info["basis"] = {basis: bases[basis] for basis in _BASES if bases[basis]}
    return owned, info


def _size_wording(max_bytes: int) -> str:
    if max_bytes >= _MIB and max_bytes % _MIB == 0:
        return f"over {max_bytes // _MIB:,} MiB"
    return f"over {max_bytes:,} bytes"


def _counts(pairs: dict[str, int]) -> str:
    return ", ".join(f"{key} {value:,}" for key, value in pairs.items())


def _unresolved_line(item: dict) -> str:
    line = (
        f"{item['repo']}:{item['source_file']} #{item['pointer']} {item['ref']} -> {item['reason']}"
    )
    if item["candidates"]:
        line += f" [{', '.join(item['candidates'])}]"
    return line


def render_report(
    total: int,
    repositories: int,
    info: dict,
    owned: list[Owned],
    layer_report: LayerReport,
    max_bytes: int,
) -> list[str]:
    removed, _edges_removed, semantic_kept = layer_report.graphify_removed
    by_basis = info["basis"]
    skipped = {
        ("over" in label and _size_wording(max_bytes) or label): info[reason]["count"]
        for reason, label in (
            ("malformed", "malformed"),
            ("too-large", "over"),
            ("outside-repositories", "outside-repositories"),
        )
        if info[reason]["count"]
    }
    skipped_total = sum(info[reason]["count"] for reason in _SKIP_REASONS)
    lines = [
        f"json-schema: {total:,} .json files in the content set across {repositories:,} "
        "repositories",
        f"  recognised as JSON Schema {len(owned):>7,}   ({_counts(by_basis) or 'none'})",
        f"  not a schema              {info['not-a-schema']['count']:>7,}   (left to graphify)",
        f"  skipped                   {skipped_total:>7,}   "
        f"({_counts(skipped) or 'none'}){'  - named below' if skipped_total else ''}",
    ]
    if info["truncated"]["count"]:
        lines.append(
            f"  truncated at depth        {info['truncated']['count']:>7,}   "
            "(kept in the layer; named below)"
        )
    lines += [
        f"  graphify nodes removed    {removed:>7,} from {len(owned):,} owned files "
        f"(keyword scaffolding); {semantic_kept:,} semantic nodes kept",
        f"  added                     {sum(layer_report.nodes_by_kind.values()):>7,} nodes "
        f"({_counts(layer_report.nodes_by_kind) or 'none'})",
        f"                            {sum(layer_report.edges_by_relation.values()):>7,} edges "
        f"({_counts(layer_report.edges_by_relation) or 'none'})",
        f"  $ref resolved             {sum(layer_report.resolved_by_how.values()):>7,}   "
        f"({_counts(layer_report.resolved_by_how) or 'none'})",
        f"{'cross-repository':>45} {layer_report.cross_repository:,}",
    ]
    reasons = Counter(item["reason"] for item in layer_report.unresolved)
    ordered = {
        reason: reasons[reason]
        for reason in _UNRESOLVED_REASONS
        if reason == "meta-schema" or reasons[reason]
    }
    lines.append(
        f"  $ref unresolved           {len(layer_report.unresolved):>7,}   ({_counts(ordered)})"
    )
    for reason in _REASONS[:3] + ("truncated",):
        if info[reason]["names"]:
            lines.append("")
            heading = _size_wording(max_bytes) if reason == "too-large" else reason
            lines.append(f"{reason} ({heading}):" if reason == "too-large" else f"{reason}:")
            lines += [f"  {name}" for name in info[reason]["names"]]
    shown = layer_report.unresolved[:UNRESOLVED_SHOWN]
    if shown:
        lines += ["", f"first {len(shown):,} of {len(layer_report.unresolved):,} unresolved:"]
        lines += [f"  {_unresolved_line(item)}" for item in shown]
    return lines


def note_json_schema_layer(nodes: int, edges: int, layer_report: LayerReport) -> None:
    """Record in graphify's report that the graph changed after it was written.

    Bounded by a marker and replaced on every run, as the gherkin stage's note is.
    Nothing is written when graphify left no report.
    """
    if not config.GRAPH_REPORT_PATH.is_file():
        return
    existing = config.GRAPH_REPORT_PATH.read_text(encoding="utf-8")
    if REPORT_NOTE_MARKER in existing:
        existing = existing.split(REPORT_NOTE_MARKER)[0].rstrip() + "\n"
    removed = layer_report.graphify_removed[0]
    note = (
        f"\n{REPORT_NOTE_MARKER}\n"
        "## Added after this report was written\n\n"
        "The totals above describe the graph as `graphify` left it. The `json-schema` "
        "stage ran afterwards, replaced graphify's keyword nodes for JSON Schema "
        "documents with schema, definition and field nodes, and resolved their `$ref`s, "
        "so the committed graph no longer matches this report:\n\n"
        f"- graph now: **{nodes:,} nodes, {edges:,} edges**\n"
        f"- removed here: {removed:,} graphify keyword nodes\n"
        f"- added here: {sum(layer_report.nodes_by_kind.values()):,} nodes, "
        f"{sum(layer_report.edges_by_relation.values()):,} edges\n\n"
        "Later stages can move these figures again. Measure the graph itself when "
        "the number matters; read this report for the commit it was built from.\n"
    )
    # Sonar S2083: a module constant from configuration, offline build tooling on a local clone.
    config.GRAPH_REPORT_PATH.write_text(existing + note, encoding="utf-8")  # NOSONAR(S2083)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    if not config.GRAPH_PATH.exists():
        print(f"Graph not found: {config.GRAPH_PATH} (gunzip -k graph.json.gz first)")
        return 1
    refusal = graph_files.stale_refusal(config.GRAPH_PATH)
    if refusal:
        print(refusal, file=sys.stderr)
        return 1
    try:
        files, _source = extract_ast.resolve_input(arguments, config.GRAPH_PATH.parent)
    except extract_ast.InputRefused as refusal_message:
        print(str(refusal_message), file=sys.stderr)
        return 1

    json_files = [path for path in files if path.suffix.lower() == ".json"]
    groups = extract_ast.by_repository(json_files, config.REPOSITORIES_DIR)
    owned, info = read_owned(groups, config.REPOSITORIES_DIR, arguments.max_bytes)

    graph = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
    # #478 will supply the fork set; until it merges, no repository is treated as a fork.
    layer_report = add_layer(graph, owned, frozenset())

    serialised = json.dumps(graph, ensure_ascii=False)
    config.GRAPH_PATH.write_text(serialised, encoding="utf-8")  # NOSONAR(S2083)
    with io.gzip_text(config.GRAPH_PATH.with_suffix(".json.gz")) as out:
        out.write(serialised)

    if arguments.unresolved:
        io.checked_write_target(arguments.unresolved)
        arguments.unresolved.parent.mkdir(parents=True, exist_ok=True)
        arguments.unresolved.write_text(  # NOSONAR(S2083) - validated just above
            json.dumps(layer_report.unresolved, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    repositories = len([name for name in groups if name])
    print(
        "\n".join(
            render_report(
                len(json_files), repositories, info, owned, layer_report, arguments.max_bytes
            )
        )
    )
    if arguments.unresolved:
        print(f"-> {arguments.unresolved}")
    note_json_schema_layer(len(graph["nodes"]), len(graph["links"]), layer_report)

    skipped = sum(info[reason]["count"] for reason in _SKIP_REASONS)
    telemetry.record(
        {
            "json_schema.files_recognised": len(owned),
            "json_schema.files_skipped": skipped,
            "json_schema.nodes": sum(layer_report.nodes_by_kind.values()),
            "json_schema.edges": sum(layer_report.edges_by_relation.values()),
            "json_schema.refs_resolved": sum(layer_report.resolved_by_how.values()),
            "json_schema.refs_unresolved": len(layer_report.unresolved),
            "json_schema.refs_cross_repository": layer_report.cross_repository,
            "json_schema.graphify_nodes_removed": layer_report.graphify_removed[0],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
