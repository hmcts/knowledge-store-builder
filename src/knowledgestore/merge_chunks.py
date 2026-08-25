"""Merge the semantic fan-out's per-chunk extractions without fabricating or losing.

graphify's skill concatenates the chunk files. On a 1,556-chunk estate that is
wrong in two opposite directions at once, and both are silent.

    knowledgestore merge-chunks   -> graphify-out/.graphify_semantic_new.json

## Three treatments, and id collision cannot be the condition

Chunks are extracted by independent agents, so ids are unique *within* a chunk and
emphatically not across chunks. **The label decides which of two opposite actions
is correct**, and a plain concatenation cannot tell them apart:

- **same id, same label** - genuinely one entity seen from two chunks: a shared
  registry, a vault alias. This is the cross-file linking the layer exists to
  produce. Merge, and union the source files. Measured: 135 on that estate.
- **same id, different label** - unrelated entities colliding on a short slug
  (`sops_key`, `kube_system`). Concatenation merges them and re-points every edge
  at whichever won, asserting relationships that were never in the corpus and are
  indistinguishable from extracted ones. Namespace, keep both. Measured: 187.

A merger that namespaces everything destroys the linking; one that merges
everything fabricates. Neither is a threshold.

## The namespace comes from content, never from the chunk

The obvious namespace is the chunk number - `sops_key_c0042`. It is forbidden and
unstable, and both matter:

**Forbidden.** The extraction spec is explicit: *"never append chunk numbers,
sequence numbers, or any suffix to an ID (no `_c1`, `_c2`, `_chunk2`)."* One
estate's merger does exactly that, and that estate's own chunk-file gate would
reject all 187 ids its merger produced - the gate runs on the input, so it never
sees them.

**Unstable.** Chunk numbering is not reproducible. Across two plans of the same
corpus, 5 of 762 text chunks had identical membership, so a chunk-derived id
changes on any re-plan and every downstream reference to it breaks silently.

So the namespace is the spec's own: `{stem}_{entity}`, where stem is the source
file's repo-relative path with separators collapsed. That is not an invention - it
is the id format the spec already mandates, applied to ids that did not follow it.

## Fragmentation is the mirror image, and the spec contradicts itself here

Agents that *do* follow `{stem}_{entity}` produce ids unique by construction, so a
genuinely shared entity gets a different id in every chunk that sees it - one
container image reappearing as sixteen unrelated nodes. **Collision fabricates;
fragmentation silently omits.**

The spec cannot settle it: it requires `{stem}_{entity}` *and* that "the same entity
must always produce the same ID regardless of which chunk processes it", and an
entity referenced from sixteen files has sixteen stems. Both cannot hold. So this
stage chooses, and says so: labels that are unambiguously *global* identifiers - an
image ref, a vault URL, a chart path - are consolidated to one id; everything else
is left fragmented. It **deliberately under-merges**, because consolidating
`Kustomization` would invent relationships between 109 unrelated resources, and a
fabricated edge is worse than a missing one. The residue is counted and reported.

## Nothing is discarded without being recorded

`original_id` is mandatory on every node whose id changed. One estate's merger
overwrites the id and drops its remap table when the process exits, so a consumer
resolving the extractor's id finds nothing and a consumer holding the synthesised
id cannot recover what it was: not retained, not recoverable, not recorded as lost.
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

from . import config, io

# Forbidden by the extraction spec, and asserted on this stage's own OUTPUT rather
# than only on its input - a gate positioned where the violation cannot occur reads
# as compliance for the artefact nobody asked it about.
CHUNK_SUFFIX = re.compile(r"_(?:c|chunk)(\d+)(?:_\d+)?$")

# A label is treated as a global identifier when it carries a separator, holds no
# whitespace, and is long enough to be an address rather than a word. Generic
# Kubernetes and infrastructure kinds are excluded by name: `Kustomization` alone
# carried 109 distinct ids on one estate, and those are 109 different resources.
GENERIC_LABELS = frozenset(
    {
        "kustomization",
        "helmrelease",
        "helmrepository",
        "namespace",
        "configmap",
        "secret",
        "deployment",
        "service",
        "ingress",
        "serviceaccount",
        "role",
        "rolebinding",
        "clusterrole",
        "clusterrolebinding",
        "networkpolicy",
        "gitrepository",
        "provider",
        "variable",
        "output",
        "module",
        "resource",
        "data",
    }
)
MIN_GLOBAL_LENGTH = 8


def is_global_identifier(label: str) -> bool:
    """Whether a label names one thing addressable from anywhere.

    Deliberately narrow. The cost of a false positive is a fabricated relationship
    between unrelated entities, which nothing downstream can detect; the cost of a
    false negative is a fragmented entity, which the residue count makes visible.
    """
    text = (label or "").strip()
    if len(text) < MIN_GLOBAL_LENGTH or any(c.isspace() for c in text):
        return False
    if text.lower() in GENERIC_LABELS:
        return False
    # A templated label is not one identifier, it is a family of them.
    # `${ENVIRONMENT}` resolves differently per environment, so consolidating on it
    # collapses every environment's resource into one node and every edge follows -
    # the fabricate-relationships direction. Measured on a 361-repo estate: 25 such
    # labels, and rejecting them moved `consolidated` from 1,070 to 1,051, exactly
    # reproducing that store's committed count.
    if any(marker in text for marker in ("${", "{{", "%(")):
        return False
    # A separator that implies *addressing* - a host, a path, a registry tag, an
    # account. A bare dot does not, and accepting it was a defect: `values.yaml`,
    # `README.md`, `package.json` and `index.ts` all passed, so every one of them in
    # the estate would have consolidated into a single node, fabricating
    # relationships between unrelated files. That is the exact failure this function
    # exists to avoid, and it fired on every estate rather than only infra ones.
    #
    # The cost is that dotted names which ARE global - a Java FQCN, say - now stay
    # fragmented and are counted in `fragmented_left`. That is the direction this
    # stage deliberately errs in: a fabricated edge is worse than a missing one,
    # because nothing downstream can detect it.
    return any(sep in text for sep in "/:@")


def spec_stem(source_file: str, keep_extension: bool = False) -> str:
    """The id stem: the path, every segment kept, non-alphanumerics underscored.

    `keep_extension=False` is the extraction spec's rule and the default, because
    it is what every committed store's ids were generated with.

    **Dropping the extension is also the root of #115 and #129.** Two files sharing
    a path stem - a component and its template, a doc and its config sibling - are
    assigned one id by design. Measured on one estate: 98 collisions between the
    AST and semantic layers, all with disagreeing labels, all describing different
    files, carrying 311 edges; 92 of the 98 were an extension pair. Reproduced to
    the unit across two semantic id schemes, because the mechanism is the format
    rather than the scheme.

    `keep_extension=True` removes that class rather than resolving instances of it.
    It is opt-in and not the default, because it changes ids that stores have
    committed, and adopting it is a re-archive rather than an upgrade. What the
    default run reports is the *cost* of adopting: see `basis_would_change` in the
    counters, which is that number measured on the operator's own corpus rather
    than estimated from someone else's.
    """
    path = Path((source_file or "").strip())
    if not keep_extension:
        path = path.with_suffix("") if path.suffix else path
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(path).lower())).strip("_")


def read_chunks(directory: Path) -> tuple[list[tuple[str, dict]], list[str]]:
    """(chunk name, payload) per chunk file, plus the names that could not be read.

    An unreadable chunk is reported rather than skipped: the fan-out's own failure
    mode is an agent that wrote nothing, and a merge that quietly covers 1,555 of
    1,556 chunks reports success for a graph missing a chunk's worth of evidence.
    """
    chunks, unreadable = [], []
    for path in sorted(directory.glob(".graphify_chunk_*.json")):
        if path.name.endswith("_plan.json"):
            continue
        payload = io.read_json_dict(path)
        if not payload or "nodes" not in payload:
            unreadable.append(path.name)
            continue
        chunks.append((path.name, payload))
    return chunks, unreadable


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="knowledgestore merge-chunks",
        description="Merge per-chunk semantic extractions, keeping cross-chunk links "
        "and refusing to fuse unrelated entities.",
    )
    parser.add_argument(
        "--chunks",
        type=Path,
        help="directory holding .graphify_chunk_*.json (default: graphify-out/)",
    )
    parser.add_argument(
        "--out", type=Path, help="default: graphify-out/.graphify_semantic_new.json"
    )
    parser.add_argument(
        "--stem-basis",
        choices=("path", "path-with-extension"),
        default="path",
        help="how an id stem is derived. 'path' is the extraction spec's rule and what "
        "every committed store used; 'path-with-extension' removes the collision class "
        "in #115/#129 and changes ids, so adopting it is a re-archive. The cost is "
        "reported either way as `basis_would_change`",
    )
    parser.add_argument(
        "--no-consolidate",
        action="store_true",
        help="leave fragmented global identifiers scattered rather than collapsing them; "
        "the count is reported either way",
    )
    return parser.parse_args(argv)


def _collect(chunks: list[tuple[str, dict]]) -> tuple[dict, dict, dict, dict]:
    """One pass over every chunk: identities, the labels each id carried, provenance."""
    by_identity: dict[tuple[str, str, str], dict] = {}
    labels_for_id: dict[str, set[str]] = defaultdict(set)
    origin: dict[tuple[str, str], tuple[str, str, str]] = {}
    # Seeded here, not in `main`: `consolidate()` and `merge_nodes()` are both
    # public, and passing one's counters to the other raised KeyError for any caller
    # that did not go through `main`.
    counters = {
        "merged": 0,
        "namespaced": 0,
        "disambiguated": 0,
        "seen": 0,
        "consolidated": 0,
        "fragmented_left": 0,
        # Seeded like the rest: a caller that skips `main` must not get a KeyError,
        # which is exactly the defect #194 fixed for two of these keys.
        "basis_would_change": 0,
    }

    for chunk_name, payload in chunks:
        for node in payload.get("nodes") or []:
            if not isinstance(node, dict) or not node.get("id"):
                continue
            counters["seen"] += 1
            original = str(node["id"])
            identity = (original, str(node.get("label") or ""), str(node.get("file_type") or ""))
            labels_for_id[original].add(identity[1])
            origin[(chunk_name, original)] = identity
            existing = by_identity.get(identity)
            if existing is None:
                entry = dict(node)
                entry["source_files"] = sorted({str(node.get("source_file") or "")} - {""})
                by_identity[identity] = entry
                continue
            # Same id, same label, same kind: one entity seen twice. The union of
            # source files IS the cross-file evidence this layer exists to produce.
            counters["merged"] += 1
            existing["source_files"] = sorted(
                {*existing["source_files"], str(node.get("source_file") or "")} - {""}
            )
    return by_identity, labels_for_id, origin, counters


def merge_nodes(
    chunks: list[tuple[str, dict]], keep_extension: bool = False
) -> tuple[dict, dict, dict]:
    """(nodes by final id, id remap, counters).

    The remap is keyed `(chunk name, original id)`, because the same original id in
    two chunks may resolve to two different final ids - which is the whole point of
    the namespacing case, and why a global `{old: new}` table cannot express it.

    **It resolves through the identity, not through the original id.** The first
    version fixed the remap up by matching on the original id, so both chunks'
    entries were overwritten with whichever was processed last, and chunk c2's edges
    would have been re-pointed at chunk c1's entity. That is the fabrication this
    stage exists to prevent, reproduced inside it, and it showed only because a
    two-chunk case was printed where the two answers must differ.
    """
    by_identity, labels_for_id, origin, counters = _collect(chunks)

    # An id carrying more than one label is a collision, and every one of its
    # entries is renamed. Renaming only the later ones would leave the first holding
    # a bare slug a consumer would read as authoritative.
    final_for: dict[tuple[str, str, str], str] = {}
    nodes: dict[str, dict] = {}
    for identity, node in by_identity.items():
        original, label, _kind = identity
        if len(labels_for_id[original]) > 1:
            counters["namespaced"] += 1
            basis = node["source_files"][0] if node["source_files"] else label
            stem = spec_stem(basis, keep_extension=keep_extension)
            # The migration cost of #115, measured here rather than estimated
            # elsewhere: how many of this run's namespaced ids the other basis
            # would spell differently. Computed on every run, including the
            # default, so a store learns its own number without adopting anything.
            if spec_stem(basis, keep_extension=not keep_extension) != stem:
                counters["basis_would_change"] += 1
            node["original_id"] = original
            node["id"] = f"{stem}_{original}" if stem else original
        # Two identities differing only in label share `stem` and `original`, so the
        # namespaced form collides and a plain assignment drops one. That lost 13 of
        # 47,653 nodes on a real estate while `namespaced` reported 367 kept apart -
        # a counter overstating success, which is the reassuring direction and the
        # dangerous one. This is the very collision the namespacing exists to
        # resolve, reintroduced by the step that resolves it.
        if node["id"] in nodes:
            counters["disambiguated"] += 1
            base, suffix = node["id"], 2
            while f"{base}_{suffix}" in nodes:
                suffix += 1
            node["id"] = f"{base}_{suffix}"
        final_for[identity] = node["id"]
        nodes[node["id"]] = node

    return nodes, {key: final_for[identity] for key, identity in origin.items()}, counters


def consolidate(nodes: dict, remap: dict, counters: dict) -> None:
    """Collapse ids that are different names for one global identifier, in place.

    Agents following the spec's `{stem}_{entity}` rule produce ids unique by
    construction, so a genuinely shared entity gets one per chunk that saw it. This
    collapses only labels that pass `is_global_identifier` and deliberately
    under-merges; `counters["fragmented_left"]` is the residue, reported rather than
    hidden, because that number is the cost of the choice.
    """
    ids_for_label: dict[str, list[str]] = defaultdict(list)
    for node in nodes.values():
        ids_for_label[str(node.get("label") or "")].append(node["id"])

    for label, ids in ids_for_label.items():
        if len(ids) < 2:
            continue
        if not is_global_identifier(label):
            counters["fragmented_left"] += 1
            continue
        keep, *rest = sorted(ids)
        counters["consolidated"] += len(rest)
        for dropped in rest:
            _absorb(nodes, remap, keep, dropped)


def _absorb(nodes: dict, remap: dict, keep: str, dropped: str) -> None:
    """Fold one fragmented id into the id being kept, recording what was absorbed."""
    node = nodes.pop(dropped)
    merged = nodes[keep]
    merged["source_files"] = sorted(
        set(merged["source_files"]) | set(node.get("source_files") or [])
    )
    merged.setdefault("consolidated_ids", []).append(dropped)
    for key, value in remap.items():
        if value == dropped:
            remap[key] = keep


def _endpoint(
    chunk_name: str, raw: str, remap: dict, global_ids: dict, nodes: dict, counters: dict
) -> str | None:
    """The final id for one edge endpoint, or None when it cannot be resolved.

    An agent sometimes names an id it did not define in its own chunk. Two very
    different things look identical: the id exists in *another* chunk - the
    cross-chunk relationship this layer exists to capture - or it exists nowhere.
    Recovery is restricted to globally unambiguous ids, because guessing fabricates.
    """
    final = remap.get((chunk_name, raw))
    if final is None:
        candidates = global_ids.get(raw, set())
        if len(candidates) == 1:
            final = next(iter(candidates))
            counters["recovered"] += 1
        elif len(candidates) > 1:
            counters["ambiguous"] += 1
        else:
            counters["dangling"] += 1
    return final if final in nodes else None


def resolve_edges(chunks: list[tuple[str, dict]], nodes: dict, remap: dict) -> tuple[list, dict]:
    """Edges re-pointed at merged ids, cross-chunk targets recovered, rest dropped.

    Measured on one estate: of 43 endpoints an agent named without defining, 37
    resolved to exactly one other chunk and 6 existed nowhere - so a concatenating
    merge discarded 37 of the layer's highest-value edges as dangling. That estate
    reported 0 ambiguous, which means the rule was never exercised there; the
    counter exists so a store where it fires can see it.
    """
    global_ids: dict[str, set[str]] = defaultdict(set)
    for (_chunk, original), final in remap.items():
        global_ids[original].add(final)

    counters = {"recovered": 0, "ambiguous": 0, "dangling": 0, "duplicate": 0}
    seen: set[tuple] = set()
    edges: list[dict] = []

    for chunk_name, payload in chunks:
        for edge in payload.get("edges") or []:
            resolved = _resolve_one(chunk_name, edge, remap, global_ids, nodes, counters, seen)
            if resolved is not None:
                edges.append(resolved)
    return edges, counters


def _resolve_one(
    chunk_name: str, edge, remap: dict, global_ids: dict, nodes: dict, counters: dict, seen: set
) -> dict | None:
    """One edge, re-pointed - or None when it cannot be kept, with the reason counted."""
    if not isinstance(edge, dict):
        return None
    ends = [
        _endpoint(chunk_name, str(edge.get(role) or ""), remap, global_ids, nodes, counters)
        for role in ("source", "target")
    ]
    if ends[0] is None or ends[1] is None:
        return None
    key = (ends[0], ends[1], str(edge.get("relation") or ""))
    if key in seen:
        counters["duplicate"] += 1
        return None
    seen.add(key)
    merged = dict(edge)
    merged["source"], merged["target"] = ends
    return merged


def spec_breaches(nodes: dict, chunk_numbers: set[str]) -> list[str]:
    """Ids carrying a chunk-derived suffix, which the spec forbids outright.

    Asserted on this stage's OUTPUT. One estate's chunk-file gate enforces the same
    rule at zero tolerance on the input, where the violation cannot occur, and its
    merger emitted 187 ids the gate would have rejected. A check reads one artefact,
    and its silence licenses a claim about that artefact only.

    **Tested against the run's actual chunk numbers, not a digit pattern.** A
    pattern of `\d{2,}` matched 34 ids on a 361-repo estate that were all family
    court form codes - `C21`, `C43`, `C51`, `C63`, `C100`, with `C100` alone in 40
    files - and because this refusal is hard, a false positive is not a warning but
    total unavailability: the stage could not run on that estate at all.

    Widening the pattern to `\d{4}` would have cleared those 34 and been wrong for
    the reason it worked: 4-digit padding is one store's convention. `read_chunks`
    already knows the real numbers, so comparing against that set is exact in both
    directions and needs no heuristic.
    """
    breaches = []
    for nid in nodes:
        found = CHUNK_SUFFIX.search(nid)
        if found and found.group(1) in chunk_numbers:
            breaches.append(nid)
    return sorted(breaches)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    directory = arguments.chunks or (config.ROOT / "graphify-out")
    destination = arguments.out or (directory / ".graphify_semantic_new.json")

    chunks, unreadable = read_chunks(directory)
    if unreadable:
        print(
            f"{len(unreadable)} chunk file(s) could not be read as extractions, "
            f"starting with {unreadable[0]}. Reported rather than skipped: the fan-out's "
            "own failure mode is an agent that wrote nothing, and a merge covering all "
            "but one chunk reports success for a graph missing a chunk of evidence.",
            flush=True,
        )
    if not chunks:
        print(f"No chunk extractions in {directory}. Nothing merged.", flush=True)
        return 2

    nodes, remap, counters = merge_nodes(
        chunks, keep_extension=arguments.stem_basis == "path-with-extension"
    )
    if not arguments.no_consolidate:
        consolidate(nodes, remap, counters)
    edges, edge_counters = resolve_edges(chunks, nodes, remap)

    breaches = spec_breaches(
        nodes, {name.split("_")[-1].removesuffix(".json") for name, _p in chunks}
    )
    if breaches:
        print(
            f"REFUSING to write: {len(breaches)} merged id(s) carry a chunk-derived suffix, "
            f"starting with {breaches[0]}. The extraction spec forbids that outright, and "
            "such an id changes on any re-plan - so every downstream reference to it breaks "
            "silently. This is asserted on the output because a gate on the input cannot "
            "see it.",
            flush=True,
        )
        return 1

    io.write_json(
        destination,
        {
            "nodes": list(nodes.values()),
            "edges": edges,
            "hyperedges": [h for _n, p in chunks for h in (p.get("hyperedges") or [])],
            "input_tokens": sum(p.get("input_tokens", 0) for _n, p in chunks),
            "output_tokens": sum(p.get("output_tokens", 0) for _n, p in chunks),
        },
    )
    print(
        f"{len(chunks):,} chunks -> {len(nodes):,} nodes, {len(edges):,} edges "
        f"-> {destination}\n"
        f"  merged      {counters['merged']:>6}  same id and label across chunks: one entity\n"
        f"  namespaced  {counters['namespaced']:>6}  same id, different label: kept apart\n"
        f"  disambig.   {counters['disambiguated']:>6}  namespaced ids that still collided\n"
        f"  consolidated{counters['consolidated']:>6}  global identifiers collapsed to one id\n"
        f"  fragmented  {counters['fragmented_left']:>6}  label spans several ids, left alone\n"
        f"  recovered   {edge_counters['recovered']:>6}  endpoints resolved in another chunk\n"
        f"  ambiguous   {edge_counters['ambiguous']:>6}  endpoints dropped rather than guessed\n"
        f"  dangling    {edge_counters['dangling']:>6}  endpoints defined nowhere\n"
        f"  duplicate   {edge_counters['duplicate']:>6}  identical edges collapsed",
        flush=True,
    )
    if counters["namespaced"]:
        print(
            "  Every namespaced node carries `original_id`, so a consumer meeting two "
            "nodes that were once one slug can see that and go and look.",
            flush=True,
        )
    if counters["basis_would_change"]:
        other = "path" if arguments.stem_basis == "path-with-extension" else "path-with-extension"
        print(
            f"  {counters['basis_would_change']:,} of these ids would be spelled differently "
            f"under --stem-basis {other}. That is this corpus's migration cost for #115, "
            "measured here rather than estimated: adopting the other basis re-keys that "
            "many ids and is a re-archive, not an upgrade.",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
