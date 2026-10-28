"""The JSON Schema layer of the merged graph: nodes, edges, and what they replace.

The graph functions are pure: no filesystem, no configuration. The stage's `main`
reads the files, builds `Owned` values, and calls `add_layer`, which rebuilds the
layer from scratch each time (idempotence by reconstruction, as `packages` does).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from knowledgestore import config, extract_ast, graph_files, io, kinds, telemetry
from knowledgestore.json_schema import (
    Entity,
    SchemaReadError,
    load,
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
        if isinstance(holder, dict) and isinstance(holder.get("hyperedges"), list):
            holder["hyperedges"] = _surviving_hyperedges(holder["hyperedges"], removed)


def _surviving_hyperedges(hyperedges: list, removed: set[str]) -> list:
    """`hyperedges` without the removed members, and without any left with none."""
    kept = []
    for hyperedge in hyperedges:
        members = hyperedge.get("nodes") if isinstance(hyperedge, dict) else None
        if isinstance(members, list):
            survivors = [m for m in members if not (isinstance(m, str) and m in removed)]
            if not survivors:
                continue
            hyperedge = {**hyperedge, "nodes": survivors}
        kept.append(hyperedge)
    return kept


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


@dataclass
class _RefTally:
    """What resolving the references found, beyond the edges it wrote."""

    how: Counter[str] = field(default_factory=Counter)
    unresolved: list[dict] = field(default_factory=list)
    self_references: int = 0


def add_layer(
    graph: dict,
    owned: list[Owned],
    forks: frozenset[str],
    aliases: frozenset[tuple[str, str]] = frozenset(),
) -> LayerReport:
    """Replace graphify's keyword nodes for `owned` files with the layer; report what it did.

    `aliases` are (repo, path) pairs naming a symlink to an owned file in the same
    repository: graphify read the link as a file of its own, so its keyword nodes
    there are removed too, while the layer's nodes belong to the target alone.
    """
    report = LayerReport()
    unique = _unique(owned)
    ordered = list(unique.values())
    strip_layer(graph)
    report.graphify_removed = remove_graphify_nodes(graph, set(unique) | aliases)

    index = Index.build(ordered)
    nodes, pointers, kind_counts = _layer_nodes(ordered)
    tally = _RefTally()
    added: list[dict] = []
    for item in ordered:
        added += _contains_edges(item)
        added += _reference_edges(item, index, forks, pointers, tally)
    edges = _deduplicated(added)

    graph["nodes"].extend(nodes)
    graph["links"].extend(edges)
    report.cross_repository = sum(
        1 for e in edges if e["source"].split("::", 1)[0] != e["target"].split("::", 1)[0]
    )
    report.self_references = tally.self_references
    report.nodes_by_kind = {k: kind_counts[k] for k in _KIND_ORDER if kind_counts[k]}
    report.edges_by_relation = dict(sorted(Counter(e["relation"] for e in edges).items()))
    report.resolved_by_how = {k: tally.how[k] for k in _RESOLVED_HOW if tally.how[k]}
    report.unresolved = sorted(
        tally.unresolved,
        key=lambda u: (u["repo"], u["source_file"], u["pointer"], str(u["ref"]), u["reason"]),
    )
    return report


def _unique(owned: list[Owned]) -> dict[tuple[str, str], Owned]:
    """One entry per file, whatever the input repeats, in (repo, path) order.

    A second entry would write the file's node ids twice and make it its own
    rival for the id it declares.
    """
    unique: dict[tuple[str, str], Owned] = {}
    for item in sorted(owned, key=lambda o: (o.repo, o.rel)):
        unique.setdefault((item.repo, item.rel), item)
    return unique


def _layer_nodes(ordered: list[Owned]) -> tuple[list[dict], set[str], Counter[str]]:
    """Every entity's node, the set of their ids, and the count of each kind."""
    nodes: list[dict] = []
    pointers: set[str] = set()
    kind_counts: Counter[str] = Counter()
    for item in ordered:
        for entity in item.walked.entities:
            nodes.append(node_record(item, entity))
            pointers.add(node_id(item.repo, item.rel, entity.pointer))
            kind_counts[entity.kind] += 1
    return nodes, pointers, kind_counts


def _contains_edges(item: Owned) -> list[dict]:
    """An edge from each entity's parent to it, within one file."""
    return [
        edge_record(
            node_id(item.repo, item.rel, entity.parent),
            node_id(item.repo, item.rel, entity.pointer),
            "contains",
            item.rel,
            entity.line,
            None,
            None,
        )
        for entity in item.walked.entities
        if entity.parent is not None
    ]


def _reference_edges(
    item: Owned, index: Index, forks: frozenset[str], pointers: set[str], tally: _RefTally
) -> list[dict]:
    """Resolve each reference in one file into an edge, or record why there is none."""
    edges: list[dict] = []
    for ref in item.walked.refs:
        found = resolve(index, item, ref, forks)
        target = found.target
        if target is None:
            tally.unresolved.append(_unresolved(item, ref.owner, ref.value, found, found.how))
            continue
        target_id = node_id(target.repo, target.rel, target.pointer)
        if target_id not in pointers:
            tally.unresolved.append(
                _unresolved(item, ref.owner, ref.value, found, "no-such-pointer")
            )
            continue
        source_id = node_id(item.repo, item.rel, ref.owner)
        if source_id == target_id:
            tally.self_references += 1
            continue
        tally.how[found.how] += 1
        edges.append(
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
    return edges


def _deduplicated(added: list[dict]) -> list[dict]:
    """`added` in key order, keeping the first edge of each key (CLAUDE.md 1.3)."""
    added.sort(key=lambda e: (e["source"], e["target"], e["relation"], e.get("context") or ""))
    edges: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for edge in added:
        key = (edge["source"], edge["target"], edge["relation"], edge.get("context") or "")
        if key not in seen:
            seen.add(key)
            edges.append(edge)
    return edges


# --- the stage ------------------------------------------------------------

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
REPORT_NOTE_MARKER = "<!-- knowledgestore: json-schema layer -->"
UNRESOLVED_SHOWN = 20
_MIB = 1024 * 1024
_BASES = ("meta-schema", "schema-keywords", "declared-id")
_SKIP_REASONS = ("malformed", "too-large", "outside-repositories")
_REASONS = (*_SKIP_REASONS, "truncated", "symlinked", "not-a-schema")
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


Location = tuple[str, str]  # (repo, POSIX repository-relative path)


def _location(parts: tuple[str, ...]) -> Location | None:
    return (parts[0], "/".join(parts[1:])) if len(parts) > 1 else None


def _as_written(path: Path, corpus: Path, resolved: dict[Path, Path]) -> Location | None:
    """Where `path` sits in the corpus as spelled, without following symlinks.

    graphify names a file by the path it was handed, so a link keeps its own name
    and this is the key that matches graphify's `source_file`. The corpus itself
    may be spelled differently from the content set's paths (a temporary directory
    reached through a symlink, say), so the part above the corpus is matched by
    the directory it resolves to; only the part beneath it is kept as written.
    `resolved` memoises that per directory, which every file in a clone shares.
    """
    spelled = Path(os.path.abspath(path))
    try:
        corpus_real = corpus.resolve()
    except OSError:  # pragma: no cover - an unresolvable corpus is caught upstream
        return None
    for base in (Path(os.path.abspath(corpus)), corpus_real):
        if spelled.is_relative_to(base):
            return _location(spelled.relative_to(base).parts)
    for ancestor in spelled.parents:
        try:
            if ancestor not in resolved:
                resolved[ancestor] = ancestor.resolve()
        except OSError:  # pragma: no cover - an unreadable ancestor
            return None
        if resolved[ancestor] == corpus_real:
            return _location(spelled.relative_to(ancestor).parts)
    return None


def _resolved(path: Path, corpus: Path) -> Location | None:
    """Where the file `path` names really is in the corpus, following symlinks."""
    try:
        return _location(path.resolve().relative_to(corpus.resolve()).parts)
    except (OSError, ValueError):
        return None


def _read_one(path: Path, max_bytes: int) -> tuple[str, object, dict[int, int]]:
    """Read one file: its recognition basis or the reason it is not owned, and its parse.

    The reason is `too-large`, `malformed` or `not-a-schema`. A file failing either
    parse is `malformed`.
    """
    try:
        # Size first: an oversized file is never read.
        if path.stat().st_size > max_bytes:
            return "too-large", None, {}
        text = path.read_bytes().decode("utf-8")
        # Recognise on the fast parse: most `.json` in a content set is data, and
        # only a schema needs the slower parse that records line numbers.
        basis = recognise(load(text))
        if basis is None:
            return "not-a-schema", None, {}
        document, lines = load_with_lines(text)
    except (OSError, UnicodeDecodeError, SchemaReadError):
        return "malformed", None, {}
    return basis, document, lines


def _entries(
    groups: dict[str, list[Path]], corpus: Path, outside: list[str]
) -> tuple[list[tuple[Location, Path]], list[tuple[Location, Location]]]:
    """Split the content set into files to read and links that their target owns.

    A path is keyed as written. One whose real file is another content-set path
    in the same repository is a link: the target is read once, under its own name,
    and the link takes the target's outcome. Anything `by_repository` placed
    outside every repository, a link out of the store included, is named in
    `outside` and never read.
    """
    keyed: list[tuple[Location, Location, Path]] = []
    resolved: dict[Path, Path] = {}
    for repo in sorted(groups):
        for path in groups[repo]:
            real = _resolved(path, corpus) if repo else None
            if real is None:
                outside.append(path.as_posix())
                continue
            keyed.append((_as_written(path, corpus, resolved) or real, real, path))
    keyed.sort(key=lambda entry: (entry[0], str(entry[2])))
    as_spelled = {written for written, real, _ in keyed if written == real}
    files: list[tuple[Location, Path]] = []
    links: list[tuple[Location, Location]] = []
    for written, real, path in keyed:
        if written != real and written[0] == real[0] and real in as_spelled:
            links.append((written, real))
        else:
            files.append((written, path))
    return files, links


def read_owned(
    groups: dict[str, list[Path]], corpus: Path, max_bytes: int
) -> tuple[list[Owned], dict]:
    """Read every file the layer might own; say what was skipped, and why.

    The returned dict holds, per reason, `{"count": n, "names": [...]}` with the
    names sorted, plus `"basis"`, the count of owned files by recognition basis,
    and `"aliases"`, the sorted (repo, path) of each `symlinked` link. Only
    `malformed`, `too-large` and `outside-repositories` are skips. A `truncated`
    file stays in the layer (its walk stopped at the depth limit), a `symlinked`
    link is represented by the owned file it points to, and a `not-a-schema` file
    is counted only: graphify keeps it.
    """
    names: dict[str, list[str]] = {reason: [] for reason in _REASONS}
    bases: Counter[str] = Counter()
    not_a_schema = 0
    owned: list[Owned] = []
    outcomes: dict[Location, str] = {}
    files, links = _entries(groups, corpus, names["outside-repositories"])
    for (repo, rel), path in files:
        outcome, document, lines = _read_one(path, max_bytes)
        outcomes[(repo, rel)] = outcome
        if outcome == "not-a-schema":
            not_a_schema += 1
        elif outcome not in _BASES:
            names[outcome].append(f"{repo}/{rel}")
        else:
            walked = walk(document, lines, rel)
            if walked.truncated:
                names["truncated"].append(f"{repo}/{rel}")
            bases[outcome] += 1
            owned.append(Owned(repo, rel, outcome, walked))
    aliases: list[Location] = []
    for (repo, rel), real in links:
        outcome = outcomes[real]
        if outcome == "not-a-schema":
            not_a_schema += 1
        elif outcome not in _BASES:
            names[outcome].append(f"{repo}/{rel}")
        else:
            names["symlinked"].append(f"{repo}/{rel}")
            aliases.append((repo, rel))
    info: dict = {
        reason: {"count": len(found), "names": sorted(found)} for reason, found in names.items()
    }
    info["not-a-schema"] = {"count": not_a_schema, "names": []}
    info["basis"] = {basis: bases[basis] for basis in _BASES if bases[basis]}
    info["aliases"] = sorted(aliases)
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
    skipped = {
        _skip_label(reason, max_bytes): info[reason]["count"]
        for reason in _SKIP_REASONS
        if info[reason]["count"]
    }
    skipped_total = sum(info[reason]["count"] for reason in _SKIP_REASONS)
    lines = [
        f"json-schema: {total:,} .json files in the content set across {repositories:,} "
        "repositories",
        f"  recognised as JSON Schema {len(owned):>7,}   ({_counts(info['basis']) or 'none'})",
        f"  not a schema              {info['not-a-schema']['count']:>7,}   (left to graphify)",
        f"  skipped                   {skipped_total:>7,}   "
        f"({_counts(skipped) or 'none'}){'  - named below' if skipped_total else ''}",
    ]
    lines += _kept_aside_lines(info)
    lines += [
        f"  graphify nodes removed    {removed:>7,} from {len(owned):,} owned files "
        f"(keyword scaffolding); {semantic_kept:,} semantic nodes kept",
        f"  added                     {sum(layer_report.nodes_by_kind.values()):>7,} nodes "
        f"({_counts(layer_report.nodes_by_kind) or 'none'})",
        f"                            {sum(layer_report.edges_by_relation.values()):>7,} edges "
        f"({_counts(layer_report.edges_by_relation) or 'none'})",
        f"  $ref resolved             {sum(layer_report.resolved_by_how.values()):>7,}   "
        f"({_counts(layer_report.resolved_by_how) or 'none'}); "
        f"self-references {layer_report.self_references:,} (no edge)",
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
    lines += _named_lines(info, max_bytes)
    shown = layer_report.unresolved[:UNRESOLVED_SHOWN]
    if shown:
        lines += ["", f"first {len(shown):,} of {len(layer_report.unresolved):,} unresolved:"]
        lines += [f"  {_unresolved_line(item)}" for item in shown]
    return lines


def _skip_label(reason: str, max_bytes: int) -> str:
    """A skip reason as the summary counts it: `too-large` says the limit instead."""
    return _size_wording(max_bytes) if reason == "too-large" else reason


def _kept_aside_lines(info: dict) -> list[str]:
    """The summary rows for files kept in the layer though not read in full, or not as named."""
    lines = []
    if info["truncated"]["count"]:
        lines.append(
            f"  truncated at depth        {info['truncated']['count']:>7,}   "
            "(kept in the layer; named below)"
        )
    if info["symlinked"]["count"]:
        lines.append(
            f"  symlinked to an owned file {info['symlinked']['count']:>6,}   "
            "(read once, as the file they point to; named below)"
        )
    return lines


def _named_lines(info: dict, max_bytes: int) -> list[str]:
    """One block per reason that names its files, under the reason as a heading."""
    lines: list[str] = []
    for reason in _REASONS[:3] + ("truncated", "symlinked"):
        if info[reason]["names"]:
            lines.append("")
            if reason == "too-large":
                lines.append(f"{reason} ({_size_wording(max_bytes)}):")
            else:
                lines.append(f"{reason}:")
            lines += [f"  {name}" for name in info[reason]["names"]]
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
        print(
            f"Graph not found: {config.GRAPH_PATH} (gunzip -k graph.json.gz first)",
            file=sys.stderr,
        )
        return 1
    if arguments.unresolved:
        # Before any write: a refused target found after the graph is rewritten
        # fails the run with the graph changed and no report printed.
        try:
            io.checked_write_target(arguments.unresolved)
        except ValueError as error:
            print(f"Not writing --unresolved: {error}", file=sys.stderr)
            return 2
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
    # The scope `extract-ast` extracts: the declared repositories. What falls outside is
    # handed to `read_owned` as the group it already names `outside-repositories`.
    try:
        groups, outside = extract_ast.in_scope(json_files, extract_ast.declared_repositories())
    except extract_ast.InputRefused as refusal_message:
        print(str(refusal_message), file=sys.stderr)
        return 1
    if outside:
        groups[""] = outside
    owned, info = read_owned(groups, config.REPOSITORIES_DIR, arguments.max_bytes)

    graph = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
    aliases = frozenset(info["aliases"])
    layer_report = add_layer(graph, owned, frozenset(config.FORK_REPOS), aliases)

    serialised = json.dumps(graph, ensure_ascii=False)
    config.GRAPH_PATH.write_text(serialised, encoding="utf-8")  # NOSONAR(S2083)
    with io.gzip_text(config.GRAPH_PATH.with_suffix(".json.gz")) as out:
        out.write(serialised)

    if arguments.unresolved:
        arguments.unresolved.parent.mkdir(parents=True, exist_ok=True)
        # Sonar S8707 (and S2083): validated by checked_write_target before the graph write.
        arguments.unresolved.write_text(  # NOSONAR(S2083, S8707) - validated by checked_write_target
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
