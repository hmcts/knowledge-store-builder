"""The dependency tools (spec, Tools): `repo_answer`, `dependents` and
`dependencies`, answered from the relationships layer the library writes
(`knowledgestore relationships`, #455).

Each is a plain function of `(Session, ...) -> dict`, as the graph tools are, and
the server registers them. A repository name is looked up (`results.repository`),
never joined into a path: a `by-repo/` file is found by listing the `by-repo/`
directory inside the one the manifest names for the layer (`results.by_repo_files`)
and matching file stems against the name the lookup returned.

A layer whose registry carries no format stamp, or one this server does not read,
is present but unrecognised: each tool refuses with the reason
(`results.LayerUnrecognised`) and reads nothing in the layer. So is a layer with no
per-repository file, which would otherwise answer every repository as one nothing
depends on.

Every list is complete and paged under the cap (`results.page`), with `total` and
`next_page`. An empty answer says what was searched, and an absent layer is named
rather than read as "nothing depends on it". Store text - an example line, an
`extra` block - is returned only inside the store's own fields.
"""

import json
import re
from collections.abc import Callable, Iterator
from typing import Any

from . import results
from .locate import contained
from .results import DEFAULT_MAX_RESPONSE_BYTES, REGISTRY, Known, NotHeld
from .store import Notice, Session, Store
from .tools_graph import ArgumentRefused, _count, _paged, _reader, _repository, _respond, _text

LAYER = "relationships"

# `kinds.json`, beside the registry at the library's fixed layout.
KINDS = REGISTRY.parent / "kinds.json"

# The two lists of a by-repo file, in the order `repo_answer` pages them.
LISTS = ("depends_on", "depended_on_by")

# Spec, Multi-hop: `max_depth <= 4`. A walk is bounded, so its cost is.
MAX_DEPTH = 4

# A kind name a refusal may quote: one plain identifier.
_KIND_NAME = re.compile(r"[A-Za-z0-9_-]+")

Edge = dict[str, Any]


# --- Reading the layer --------------------------------------------------------------


def _present(store: Store) -> bool:
    """Present and in a format this server reads; `_lookup` has already refused a
    layer that is present but unrecognised."""
    return results.relationships_readable(store)


def _by_repo(store: Store, name: str) -> dict[str, Any] | None:
    """The repository's by-repo file, parsed, or None when the layer holds none."""
    path = results.by_repo_files(store).get(name)
    if path is None:
        return None
    document = json.loads(path.read_bytes())
    return document if isinstance(document, dict) else None


def _edges(document: dict[str, Any] | None, which: str) -> list[Edge]:
    found = document.get(which) if document is not None else None
    return [e for e in found if isinstance(e, dict)] if isinstance(found, list) else []


def _kinds(store: Store) -> list[str] | None:
    """The kinds `kinds.json` lists, every registered kind whether it met a
    reference or not; None when the file cannot be read as one."""

    def read() -> list[str] | None:
        path = contained(store.root, KINDS)
        if not path.is_file():
            return None
        document = json.loads(path.read_bytes())
        return sorted(document) if isinstance(document, dict) else None

    return store.memo("relationships.kinds", read)


def _check_kind(store: Store, kind: str | None) -> None:
    """Refuse a kind the layer does not list: a misspelt kind would otherwise read
    as "in the estate, nothing depends on it"."""
    if kind is None or not _present(store):
        return
    listed = _kinds(store)
    if listed is None or kind in listed:
        return
    quotable = [name for name in listed if _KIND_NAME.fullmatch(name)]
    unquoted = len(listed) - len(quotable)
    tail = f" and {unquoted} more" if unquoted else ""
    raise ArgumentRefused(
        f"kind is not one the relationships layer lists: {', '.join(quotable)}{tail}"
    )


def _of_kind(edges: list[Edge], kind: str | None) -> list[Edge]:
    if kind is None:
        return edges
    return [e for e in edges if isinstance(e.get("kinds"), dict) and kind in e["kinds"]]


def _pins_other_than(edge: Edge, prefix: str) -> bool:
    """At least one pinned ref that does not start with `prefix`; a floating ref,
    `""`, is always other, since it pins nothing."""
    pins = edge.get("refs_pinned")
    return any(ref == "" or not ref.startswith(prefix) for ref in pins or {})


# --- Shared framing -----------------------------------------------------------------


def _lookup(
    session: Session, name: str
) -> tuple[Store, Notice | None, str, Known | results.Unknown | NotHeld, list[str]]:
    """The store, the layer that answered the name, the three-way answer, and the
    layers the response used. A relationships layer present in a format this
    server does not read is refused before the name is looked up: the lookup
    itself would read it."""
    store, notice = session.current()
    results.require_relationships(store)
    base = _reader(store).layer
    answer = results.repository(store, name)
    layers_used = [base, LAYER] if _present(store) else [base]
    if isinstance(answer, Known):
        layers_used += results.answer_layers(answer)
    return store, notice, base, answer, layers_used


def _searched(store: Store, base: str) -> str:
    return results.searched(store, [base, LAYER])


def _listing(
    store: Store,
    notice: Notice | None,
    base: str,
    layers_used: list[str],
    items: list,
    *,
    key: Callable[[Any], Any],
    cursor: str | None,
    cap_bytes: int,
    **fields: Any,
) -> dict[str, Any]:
    """One page of `items`. An empty page says what was searched, naming the
    relationships layer when it is absent."""
    if not items:
        fields["searched"] = _searched(store, base)
    return _paged(
        store, notice, layers_used, items, key=key, cursor=cursor, cap_bytes=cap_bytes, **fields
    )


def _skips(store: Store) -> dict[str, int]:
    """`by_repo_links_skipped`, when a `by-repo/` entry resolved outside the store
    and was skipped: a count, never the path."""
    skipped = results.by_repo_listing(store).skipped
    return {"by_repo_links_skipped": skipped} if skipped else {}


def _edge_key(edge: Edge) -> tuple[str, str]:
    return (str(edge.get("repository")), str(edge.get("chart")))


# --- The tools ----------------------------------------------------------------------


def dependents(
    session: Session,
    target: str,
    kind: str | None = None,
    ref_prefix_not: str | None = None,
    transitive: bool = False,
    max_depth: int | None = None,
    cursor: str | None = None,
    *,
    cap_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """The repositories that depend on `target`, each edge with its references,
    kinds, pins and example lines with pinned URLs.

    `kind` keeps edges of that kind. `ref_prefix_not` keeps edges with at least one
    pinned ref not starting with it; a floating ref counts as other. `transitive`
    walks dependents of dependents breadth first, up to `max_depth` hops (1 to 4,
    default 4), each repository listed once with its path from the target; a
    repository already reached is not followed again, and `cycles` lists every loop
    among the edges the walk read - each set of repositories that depend on one
    another, and each that depends on itself.
    A target the estate references but does not hold is answered from the
    registry's targets outside the estate, one item per kind: by its exact key, or
    as `<org>/<target>` for the store's own `github_org` only (none recorded, exact
    keys only)."""
    name = _repository(target, "target")
    if kind is not None:
        kind = _text(kind, "kind")
    if ref_prefix_not is not None:
        ref_prefix_not = _text(ref_prefix_not, "ref_prefix_not")
    depth = _walk_depth(transitive, max_depth, ref_prefix_not)
    store, notice, base, answer, layers_used = _lookup(session, name)
    _check_kind(store, kind)
    common = {"store": store, "notice": notice, "base": base, "layers_used": layers_used}
    paging = {"cursor": cursor, "cap_bytes": cap_bytes}
    fields: dict[str, Any] = {"repository": answer.as_dict(), **_skips(store)}

    if isinstance(answer, NotHeld):
        entries = [
            outside.as_dict()
            for outside in results.outside_estate(store, name)
            if kind is None or outside.kind == kind
        ]
        fields.update(_not_applied_outside(ref_prefix_not, transitive))
        return _listing(
            **common,
            items=entries,
            key=lambda e: (e["kind"], e["target"]),
            **paging,
            **fields,
        )
    if not isinstance(answer, Known) or not _present(store):
        return _listing(**common, items=[], key=str, **paging, **fields)

    if transitive:
        items, cycles, beyond = _walk(store, answer.name, kind, depth)
        fields.update(max_depth=depth, cycles=cycles, beyond_max_depth=beyond)
        return _listing(
            **common,
            items=items,
            key=lambda item: (item["depth"], item["repository"]),
            **paging,
            **fields,
        )
    edges = _of_kind(_edges(_by_repo(store, answer.name), "depended_on_by"), kind)
    if ref_prefix_not is not None:
        edges = [e for e in edges if _pins_other_than(e, ref_prefix_not)]
    return _listing(**common, items=edges, key=_edge_key, **paging, **fields)


def _walk_depth(transitive: bool, max_depth: int | None, ref_prefix_not: str | None) -> int:
    """The hops a `dependents` walk takes, 0 when it does not walk; refuses the
    arguments that do not go together."""
    if not isinstance(transitive, bool):
        raise ArgumentRefused("transitive must be true or false")
    if not transitive:
        if max_depth is not None:
            raise ArgumentRefused("max_depth applies only with transitive: true")
        return 0
    depth = MAX_DEPTH if max_depth is None else max_depth
    depth = _count(depth, "max_depth", f"1 to {MAX_DEPTH}", lambda n: 1 <= n <= MAX_DEPTH)
    if ref_prefix_not is not None:
        raise ArgumentRefused(
            "ref_prefix_not filters a target's own consumers; it is not applied to a "
            "transitive walk: ask without transitive"
        )
    return depth


def _not_applied_outside(ref_prefix_not: str | None, transitive: bool) -> dict[str, Any]:
    """What a target outside the estate cannot apply. `transitive` and
    `ref_prefix_not` were refused together by `_walk_depth`."""
    if ref_prefix_not is not None:
        return {"not_applied": {"ref_prefix_not": "targets outside the estate record no pins"}}
    if transitive:
        return {
            "not_applied": {
                "transitive": "a target outside the estate has no dependency file to walk from"
            }
        }
    return {}


def _walk(
    store: Store, target: str, kind: str | None, max_depth: int
) -> tuple[list[dict[str, Any]], list[list[str]], list[str]]:
    """Breadth first from `target` over `depended_on_by`.

    Each repository is listed once, at the first depth that reaches it, with the
    path through the predecessor that sorts first and every edge from that
    predecessor; one already reached is never followed again. The hop past
    `max_depth` is looked at but not listed, and `beyond` names each repository at
    `max_depth` with a dependent the walk did not reach.

    `cycles` is found over every edge the walk read, not along single paths: each
    strongly connected component of more than one repository, and each repository
    listed among its own dependents, as its members by name. So a loop closed
    between two branches - each repository reached on its own - is still reported.
    """
    read: dict[str, list[Edge]] = {}

    def dependents_of(name: str) -> list[Edge]:
        if name not in read:
            read[name] = _of_kind(_edges(_by_repo(store, name), "depended_on_by"), kind)
        return read[name]

    visited = {target}
    frontier = [[target]]
    items: list[dict[str, Any]] = []
    walked: set[tuple[str, str]] = set()
    beyond: set[str] = set()
    for depth in range(1, max_depth + 2):
        reached = _hop(
            frontier, dependents_of, visited, walked, beyond if depth > max_depth else None
        )
        for other in sorted(reached):
            path, edges = reached[other]
            items.append({"repository": other, "depth": depth, "path": path, "edges": edges})
        visited.update(reached)
        frontier = sorted(path for path, _ in reached.values())
    return items, _cycles(walked), sorted(beyond)


def _hop(
    frontier: list[list[str]],
    dependents_of: Callable[[str], list[Edge]],
    visited: set[str],
    walked: set[tuple[str, str]],
    beyond: set[str] | None,
) -> dict[str, tuple[list[str], list[Edge]]]:
    """One hop of `_walk` from each path in `frontier`: every repository first
    reached, with its path and the edges from that path's end. Each edge read is
    added to `walked`. Past `max_depth`, `beyond` is given instead and collects each
    path end with a dependent not yet visited; nothing is reached."""
    reached: dict[str, tuple[list[str], list[Edge]]] = {}
    for path in frontier:
        for edge in dependents_of(path[-1]):
            other = str(edge.get("repository"))
            walked.add((path[-1], other))
            if other in visited:
                continue
            if beyond is not None:
                beyond.add(path[-1])
            elif other not in reached:
                reached[other] = ([*path, other], [edge])
            elif reached[other][0][:-1] == path:
                # The same predecessor again, by another chart.
                reached[other][1].append(edge)
    return reached


def _cycles(edges: set[tuple[str, str]]) -> list[list[str]]:
    """Tarjan's strongly connected components over `edges`, iteratively: each
    component of more than one repository, and each self-loop, sorted by name, in
    sorted order."""
    return _Tarjan(edges).components()


class _Tarjan:
    """The state of one `_cycles` run: an explicit work stack of (node, children
    not yet tried) in place of recursion."""

    def __init__(self, edges: set[tuple[str, str]]) -> None:
        self.edges = edges
        self.graph: dict[str, list[str]] = {}
        for source, target in edges:
            self.graph.setdefault(source, []).append(target)
            self.graph.setdefault(target, [])
        self.order: dict[str, int] = {}
        self.low: dict[str, int] = {}
        self.stack: list[str] = []
        self.on_stack: set[str] = set()
        self.work: list[tuple[str, Iterator[str]]] = []
        self.found: list[list[str]] = []

    def components(self) -> list[list[str]]:
        for root in sorted(self.graph):
            if root in self.order:
                continue
            self._enter(root)
            while self.work:
                self._step()
        return sorted(self.found)

    def _enter(self, node: str) -> None:
        self.order[node] = self.low[node] = len(self.order)
        self.stack.append(node)
        self.on_stack.add(node)
        self.work.append((node, iter(sorted(self.graph[node]))))

    def _step(self) -> None:
        """Descend to the next unvisited child of the top node, or finish it."""
        node, children = self.work[-1]
        for child in children:
            if child not in self.order:
                self._enter(child)
                return
            if child in self.on_stack:
                self.low[node] = min(self.low[node], self.order[child])
        self.work.pop()
        if self.work:
            parent = self.work[-1][0]
            self.low[parent] = min(self.low[parent], self.low[node])
        if self.low[node] == self.order[node]:
            self._close(node)

    def _close(self, node: str) -> None:
        """Pop the component rooted at `node`; keep it if it is a loop."""
        component = []
        while True:
            member = self.stack.pop()
            self.on_stack.discard(member)
            component.append(member)
            if member == node:
                break
        if len(component) > 1 or (node, node) in self.edges:
            self.found.append(sorted(component))


def dependencies(
    session: Session,
    repo: str,
    kind: str | None = None,
    cursor: str | None = None,
    *,
    cap_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """The repositories `repo` depends on, each edge with its references, kinds,
    pins and example lines with pinned URLs; then every target outside the estate
    it references, per kind and by the registry's key, each marked
    `outside_estate: true`, with the registry's `references` (counted across every
    referrer, not this repository alone) and `referenced_by`. The registry records
    no example lines for these. Both are paged as one list, held edges first, and
    `totals` counts each. `kind` keeps edges and targets of that kind.

    A name is looked up as `find_repo` looks it up. A target outside the estate is
    keyed as the library wrote it, `<org>/<repo>` for a Terraform source; a lookup
    by bare name matches that key only for the store's own `github_org`, so a store
    whose manifest records none matches only exact target names."""
    name = _repository(repo, "repo")
    if kind is not None:
        kind = _text(kind, "kind")
    store, notice, base, answer, layers_used = _lookup(session, name)
    _check_kind(store, kind)
    edges: list[Edge] = []
    outside: list[dict[str, Any]] = []
    if isinstance(answer, Known) and _present(store):
        edges = _of_kind(_edges(_by_repo(store, answer.name), "depends_on"), kind)
        outside = [
            {**target.as_dict(), "outside_estate": True}
            for target in results.targets_outside_estate(store)
            if answer.name in target.referenced_by and (kind is None or target.kind == kind)
        ]

    def key(item: dict[str, Any]) -> tuple[int, str, str]:
        if item.get("outside_estate") is True:
            return (1, str(item["kind"]), str(item["target"]))
        return (0, *_edge_key(item))

    return _listing(
        store,
        notice,
        base,
        layers_used,
        [*edges, *outside],
        key=key,
        cursor=cursor,
        cap_bytes=cap_bytes,
        repository=answer.as_dict(),
        totals={"depends_on": len(edges), "outside_estate": len(outside)},
        **_skips(store),
    )


def repo_answer(
    session: Session,
    repo: str,
    cursor: str | None = None,
    *,
    cap_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """The repository's relationships file whole, with `totals` per list, when the
    response fits the cap. Otherwise the file's other fields come with each page,
    and its edges are paged over `depends_on` then `depended_on_by`, each item
    naming its list and its position in it, so the pages reassemble to the file."""
    name = _repository(repo, "repo")
    store, notice, base, answer, layers_used = _lookup(session, name)
    document = (
        _by_repo(store, answer.name) if isinstance(answer, Known) and _present(store) else None
    )
    fields: dict[str, Any] = {"repository": answer.as_dict(), **_skips(store)}
    if document is None:
        return _respond(
            store,
            notice,
            layers_used,
            relationships=None,
            **fields,
            **results.empty(_searched(store, base)),
        )
    # Taken as written, not through `_edges`: a page set must reassemble to the file.
    lists = {
        which: found if isinstance(found := document.get(which), list) else [] for which in LISTS
    }
    totals = {which: len(lists[which]) for which in LISTS}
    if cursor is None:
        whole = _respond(
            store, notice, layers_used, relationships=document, totals=totals, **fields
        )
        if len(results.encode(whole)) <= cap_bytes:
            return whole
    header = {k: v for k, v in document.items() if k not in LISTS}
    items = [
        {"list": which, "position": position, "edge": edge}
        for which in LISTS
        for position, edge in enumerate(lists[which])
    ]
    return _paged(
        store,
        notice,
        layers_used,
        items,
        key=lambda item: (LISTS.index(item["list"]), item["position"]),
        cursor=cursor,
        cap_bytes=cap_bytes,
        relationships=header,
        totals=totals,
        **fields,
    )
