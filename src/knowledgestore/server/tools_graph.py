"""The graph tools (spec, Tools): `store_info`, `find_repo`, `search`, `node`,
`neighbours` and `community`.

Each is a plain function of `(Session, ...) -> dict`; the server registers them.
Every answer comes from the query index when it is open, and from the graph,
read whole, when it is not: the two sources sit behind one small reader, so a
tool gives the same items either way, and only the envelope says which answered.

Every SQL statement is a constant with bound parameters. Search matches word
prefixes: the caller's text is split into words by FTS5's own `unicode61`
tokenizer, the one the index was built with, and each word becomes a quoted
prefix term. So FTS syntax in the text (`NEAR`, `*`, `OR`, a column filter, an
unbalanced quote) is only ever a word, never an operator. Without the index,
`graph.GraphReader` runs the same query over an FTS5 table of the graph's labels,
so the two modes match and rank alike by construction rather than by imitation.
"""

import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from typing import Any

from . import graph, queries, results
from .graph import TOKENIZER, Edge, GraphReader, as_node
from .index import IndexHandle
from .locate import PLUGIN_ROOT
from .results import DEFAULT_MAX_RESPONSE_BYTES, Known
from .store import Notice, Session, Store

DEFAULT_SEARCH_LIMIT = 20
MAX_DEPTH = 2
# Search text is bounded before it is tokenized or searched: every word is a prefix
# scan of the label index, so an unbounded query is unbounded work.
MAX_SEARCH_BYTES = 1024
MAX_SEARCH_WORDS = 32
# A repository name is bounded as search text is: a name is compared against every
# name the estate holds, for near misses, so an unbounded one is unbounded work.
MAX_NAME_BYTES = 1024
# A community id is an SQLite INTEGER; anything wider cannot be bound.
INT64 = range(-(2**63), 2**63)

# The three flags the manifest may record, each of which may be null (unknown).
FLAGS = (("graph", "source_urls"), ("intent", "reach"), ("deployments", "present"))

# A NUL ends a C string inside SQLite's FTS5 parser, and a lone surrogate cannot be
# bound as UTF-8: neither is text a label can hold.
_UNSEARCHABLE = re.compile("[\x00\ud800-\udfff]")


class ArgumentRefused(ValueError):
    """A tool argument this server will not act on; the message says why. It never
    quotes store text, and quotes caller input only as a number."""


# --- Arguments --------------------------------------------------------------------


def _text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise ArgumentRefused(f"{what} must be text")
    if _UNSEARCHABLE.search(value):
        raise ArgumentRefused(f"{what} holds a NUL or a lone surrogate, which no label can")
    return value


def _repository(value: object, what: str) -> str:
    """A repository name to look up. One that is empty after stripping names no
    repository, so it is refused rather than looked up."""
    name = _text(value, what)
    if not name.strip():
        raise ArgumentRefused(f"{what} is empty: a repository name needs a word")
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise ArgumentRefused(f"{what} must be at most {MAX_NAME_BYTES} bytes")
    return name


# A calendar date in ISO 8601's extended form, alone or with a time: the forms a
# reader takes for a date. The basic (`20261020`), week (`2026-W42-3`) and ordinal
# (`2026-293`) forms `fromisoformat` also accepts are refused.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(T.+)?", re.ASCII)


def _date(value: object) -> str | None:
    """`value`, as written, when it is a valid calendar date (`YYYY-MM-DD`) with an
    optional `T` and time; else None. Hand-written or not, a date returned as
    structure must be one."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def _count(value: object, what: str, allowed: str, ok: Callable[[int], bool]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not ok(value):
        raise ArgumentRefused(f"{what} must be {allowed}")
    return value


# --- The search query ---------------------------------------------------------------


def _words(text: str) -> list[str]:
    """`text` split and folded by FTS5's own `unicode61` tokenizer, in order.

    A fresh in-memory connection per call is deliberate: it costs about 0.13 ms,
    and keeps no state between callers. Do not replace it with a Python tokenizer:
    SQLite's Unicode tables are its own, and an imitation measurably disagrees
    with the index on some characters (unassigned code points, newer case pairs).
    """
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute(f"CREATE VIRTUAL TABLE words USING fts5(text, tokenize='{TOKENIZER}')")
        db.execute("CREATE VIRTUAL TABLE terms USING fts5vocab(words, 'instance')")
        db.execute("INSERT INTO words(text) VALUES (?)", (text,))
        return [term for (term,) in db.execute("SELECT term FROM terms ORDER BY offset")]


def _search_words(value: object, what: str) -> list[str]:
    """The words of a caller's search text, bounded before and after splitting:
    every word is a prefix scan, so an unbounded query is unbounded work."""
    text = _text(value, what)
    if len(text.encode("utf-8")) > MAX_SEARCH_BYTES:
        raise ArgumentRefused(f"{what} must be at most {MAX_SEARCH_BYTES} bytes")
    words = _words(text)
    if not words:
        raise ArgumentRefused(f"{what} holds no word to match")
    if len(words) > MAX_SEARCH_WORDS:
        raise ArgumentRefused(f"{what} must hold at most {MAX_SEARCH_WORDS} words")
    return words


def _query(words: list[str]) -> str:
    """The FTS5 query for `words`: each distinct word quoted and starred, so every
    word must begin some word of the label. `_query(_words(text))` is the library's
    documented `match_argument` helper (index-helper: match_argument, at b6cbc5a),
    split in two so the word bounds apply between; `tests/test_index_queries.py`
    holds the two equal."""
    return " ".join('"' + word.replace('"', '""') + '"*' for word in dict.fromkeys(words))


# --- The two readers --------------------------------------------------------------

# One node as every tool returns it.
Node = dict[str, Any]

# A community's members: the documented `node` query's columns and joins, by
# community. The library documents no community query, so this one is the
# server's own; its columns are `graph.Row`'s, in order. `repos` is a LEFT JOIN:
# an index may hold a file whose repository is NULL (a node with none), and that
# node must still be found, with the repository "" as the fallback gives it.
_NODE_SELECT = (
    "SELECT n.id, n.label, coalesce(r.name, ''), n.kind, f.path, n.url, n.community"
    " FROM nodes n JOIN files f ON f.k = n.file LEFT JOIN repos r ON r.k = f.repo"
)


class _Indexed:
    """The graph as the query index holds it. Read with constant, bound SQL."""

    layer = "index"

    def __init__(self, connection: sqlite3.Connection):
        self._db = connection

    def node(self, node_id: str) -> Node | None:
        row = self._db.execute(queries.NODE, (node_id,)).fetchone()
        return as_node(row) if row else None

    def edges(self, node_id: str, relation: str | None) -> list[Edge]:
        rows = self._db.execute(queries.NEIGHBOURS, (node_id, relation))
        return [(direction, name, other) for direction, name, other in rows]

    def members(self, community: int) -> list[Node]:
        rows = self._db.execute(_NODE_SELECT + " WHERE n.community = ? ORDER BY n.id", (community,))
        return [as_node(row) for row in rows]

    def search(self, query: str, repo: str | None, kind: str | None) -> list[Node]:
        # Every match (`LIMIT -1`): the tool counts them all, then keeps its own
        # limit. `repo` is never "" here, which the query would read as "no
        # repository": the tool refuses an empty name before it is asked.
        rows = self._db.execute(queries.FIND, (query, repo, kind, -1))
        return [as_node(row) for row in rows]


Reader = _Indexed | GraphReader


def _reader(store: Store) -> Reader:
    if isinstance(store.index, IndexHandle):
        return _Indexed(store.index.connection)
    return graph.read(store)


# --- Responses ----------------------------------------------------------------------


def _respond(
    store: Store,
    notice: Notice | None,
    layers_used: list[str],
    *,
    graph_read_whole: bool = False,
    **fields: Any,
) -> dict[str, Any]:
    """The envelope and `fields`. `graph_read_whole` marks an answer the graph gave
    although the index is open, because the index failed mid-call."""
    envelope = results.envelope(store, layers_used, notice)
    if graph_read_whole:
        envelope["store"]["index"] = results.INDEX_ABSENT
    return {**envelope, **fields}


def _paged(
    store: Store,
    notice: Notice | None,
    layers_used: list[str],
    items: list,
    *,
    key: Callable[[Any], Any],
    cursor: str | None,
    cap_bytes: int,
    graph_read_whole: bool = False,
    **fields: Any,
) -> dict[str, Any]:
    """One page of `items`, framed by the envelope and `fields`. An empty list
    says what was searched."""
    frame = _respond(store, notice, layers_used, graph_read_whole=graph_read_whole, **fields)
    if not items and "searched" not in frame:
        frame["searched"] = results.searched(store, layers_used)
    return results.page(
        items, cap_bytes, cursor, digest=store.manifest.digest, key=key, frame=frame
    )


def _repository_layers(store: Store, base: str, answer: Any) -> list[str]:
    """The layers a repository answer read: provenance when the answer quotes the
    sha it records; the relationships registry is consulted only for a name the
    estate does not hold, and only when it is present in a format this server
    reads."""
    if isinstance(answer, Known):
        return [base, *results.answer_layers(answer)]
    if results.relationships_readable(store):
        return [base, "relationships"]
    return [base]


def _searched_name(store: Store, layers_used: list[str]) -> str:
    """What a name lookup searched. A relationships layer present in a format this
    server does not read was not searched, and the sentence gives its reason, so
    "not in the estate" is not read as "the registry does not name it either"."""
    consulted = list(layers_used)
    if results.relationships_unrecognised(store) is not None:
        consulted.append("relationships")
    return results.searched(store, consulted)


# --- The tools ----------------------------------------------------------------------


def store_info(
    session: Session,
    cursor: str | None = None,
    *,
    cap_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """The store's format, versions, layer states and flags, as recorded, and
    `stale_days` from the session's clock; the layers whose files no longer match
    the manifest's digests, are missing, or are directories left unchecked; with
    each repository's provenance as the `items`, paged under the cap by name, since
    that list grows with the estate."""
    store, notice = session.current()
    found = store.manifest
    if isinstance(store.index, IndexHandle):
        row = store.index.connection.execute(
            "SELECT value FROM meta WHERE key = 'index_schema'"
        ).fetchone()
        index = {"schema": row[0] if row else None, "fallback": None}
    else:
        # `index.open_index` builds every reason without a host path.
        index = {"schema": None, "fallback": store.index.reason}
    layers: dict[str, dict[str, Any]] = {
        name: {"present": entry.get("present") if isinstance(entry, dict) else None}
        for name, entry in found.layers.items()
    }
    for name, reason in results.unrecognised_layers(store).items():
        layers.setdefault(name, {"present": True})["unrecognised"] = reason
    checked = store.digest_check()
    return _paged(
        store,
        notice,
        ["provenance"],
        _provenance(store.layer("provenance")),
        key=lambda row: row["repository"],
        cursor=cursor,
        cap_bytes=cap_bytes,
        stale_days=session.stale_days(),
        index=index,
        layers=layers,
        extra_layers={
            name: {
                "present": entry.get("present") if isinstance(entry, dict) else None,
                "kind": entry.get("kind") if isinstance(entry, dict) else None,
            }
            for name, entry in found.extra_layers.items()
        },
        unread=list(found.unread),
        flags={f"{layer}.{flag}": _flag(found.layers.get(layer), flag) for layer, flag in FLAGS},
        # Layer names only, hashed on this store's first `store_info`.
        digest_mismatch=list(checked.mismatch),
        digest_missing=list(checked.missing),
        digest_unchecked=list(checked.unchecked),
        # Which rule chose the store, and which copy it is: compared by a skill
        # with the clone it checked. Never the path (SECURITY.md).
        store_source=session.source,
        served={
            "store_id": found.store_id,
            "digest": session.digest(),
            "sources_as_of": found.sources_as_of,
            "plugin_root": session.source == PLUGIN_ROOT,
        },
    )


def _flag(entry: Any, flag: str) -> Any:
    return entry.get(flag) if isinstance(entry, dict) else None


def _provenance(document: Any) -> list[dict[str, Any]]:
    repositories = document.get("repositories") if isinstance(document, dict) else None
    if not isinstance(repositories, dict):
        return []
    rows = []
    for name in sorted(repositories):
        entry = repositories[name]
        entry = entry if isinstance(entry, dict) else {}
        rows.append(
            {"repository": name, "sha": entry.get("sha"), "committed": entry.get("committed")}
        )
    return rows


def find_repo(session: Session, name: str) -> dict[str, Any]:
    """The three-way answer for a repository name: an exact match, near misses
    (each labelled as one, never as the answer), or a target the estate references
    and does not hold."""
    name = _repository(name, "repository name")
    store, notice = session.current()
    base = _reader(store).layer
    answer = results.repository(store, name)
    layers_used = _repository_layers(store, base, answer)
    fields = answer.as_dict()
    if not isinstance(answer, Known):
        fields["searched"] = _searched_name(store, layers_used)
    return _respond(store, notice, layers_used, **fields)


def search(
    session: Session,
    text: str,
    repo: str | None = None,
    kind: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    cursor: str | None = None,
    *,
    cap_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Nodes whose label has a word beginning with each word of `text`, case and
    Latin accents ignored, best first: by FTS5 `bm25`, then id. The first `limit`
    are returned, paged under the cap; `matches` counts every node that matched.
    If the index's full-text table cannot be read, the graph answers and
    `fallback` says why."""
    query = _query(_search_words(text, "search text"))
    limit = _count(limit, "limit", "a whole number of 1 or more", lambda n: n >= 1)
    if kind is not None:
        kind = _text(kind, "kind")
    store, notice = session.current()
    reader = _reader(store)
    layers_used = [reader.layer]
    fields: dict[str, Any] = {}
    if repo is not None:
        answer = results.repository(store, _repository(repo, "repo"))
        fields["repository"] = answer.as_dict()
        if not isinstance(answer, Known):
            layers_used = _repository_layers(store, reader.layer, answer)
            return _paged(
                store,
                notice,
                layers_used,
                [],
                key=str,
                cursor=cursor,
                cap_bytes=cap_bytes,
                matches=0,
                searched=_searched_name(store, layers_used),
                **fields,
            )
        repo = answer.name
    try:
        found = reader.search(query, repo, kind)
    except sqlite3.Error as error:
        if not isinstance(reader, _Indexed):
            raise
        # A damaged full-text table: the graph still holds every label.
        reader = graph.read(store)
        layers_used = [reader.layer]
        fields["fallback"] = f"the index could not be searched ({error}): graph read whole"
        found = reader.search(query, repo, kind)
    ranked = found[:limit]
    rank = {item["id"]: position for position, item in enumerate(ranked)}
    return _paged(
        store,
        notice,
        layers_used,
        ranked,
        key=lambda item: rank[item["id"]],
        cursor=cursor,
        cap_bytes=cap_bytes,
        matches=len(found),
        graph_read_whole="fallback" in fields,
        **fields,
    )


def node(session: Session, id: str) -> dict[str, Any]:
    """One node by id, with its pinned `url`; null, with what was searched, when the
    store holds no such id."""
    node_id = _text(id, "id")
    store, notice = session.current()
    reader = _reader(store)
    found = reader.node(node_id)
    fields: dict[str, Any] = {"node": found}
    if found is None:
        fields["searched"] = results.searched(store, [reader.layer])
    return _respond(store, notice, [reader.layer], **fields)


def neighbours(
    session: Session,
    id: str,
    depth: int = 1,
    relation: str | None = None,
    cursor: str | None = None,
    *,
    cap_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Every node within `depth` hops (1 or 2) of `id`, along edges either way,
    each listed once at the hop that first reaches it, with every edge that did.
    The start node is never its own neighbour, so a cycle is visited once."""
    node_id = _text(id, "id")
    depth = _count(
        depth,
        "depth",
        f"1 or 2: neighbours walks at most {MAX_DEPTH} hops",
        lambda n: 1 <= n <= MAX_DEPTH,
    )
    if relation is not None:
        relation = _text(relation, "relation")
    store, notice = session.current()
    reader = _reader(store)
    start = reader.node(node_id)
    items = _walk(reader, node_id, depth, relation) if start is not None else []
    return _paged(
        store,
        notice,
        [reader.layer],
        items,
        key=lambda item: (item["depth"], item["node"]["id"]),
        cursor=cursor,
        cap_bytes=cap_bytes,
        node=start,
    )


def _walk(reader: Reader, start: str, depth: int, relation: str | None) -> list[dict[str, Any]]:
    seen = {start}
    frontier = [start]
    items: list[dict[str, Any]] = []
    for hop in range(1, depth + 1):
        reached: dict[str, list[dict[str, str]]] = {}
        for here in frontier:
            for direction, name, other in reader.edges(here, relation):
                if other not in seen:
                    reached.setdefault(other, []).append(
                        {"from": here, "relation": name, "direction": direction}
                    )
        for other in sorted(reached):
            found = reader.node(other)
            if found is None:
                continue
            via = sorted(reached[other], key=lambda v: (v["from"], v["relation"], v["direction"]))
            items.append({"depth": hop, "node": found, "via": via})
        seen.update(reached)
        frontier = sorted(reached)
    return items


def community(
    session: Session,
    id: int,
    cursor: str | None = None,
    *,
    cap_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """One community: its label and summary, and its member nodes by id, paged. The
    summary is store text, returned as data. It is written analysis, so it is
    dated: `summary_as_of` is the manifest's `sources_as_of`, because neither the
    summary nor the manifest records the build that wrote it; null with no
    summary."""
    community_id = _count(
        id, "community id", "a whole number from -2**63 to 2**63 - 1", lambda n: n in INT64
    )
    store, notice = session.current()
    reader = _reader(store)
    members = reader.members(community_id)
    label = _entry(store.layer("labels"), community_id)
    summary = _entry(store.layer("summaries"), community_id)
    layers_used = [reader.layer] + [
        name for name in ("labels", "summaries") if store.layer(name) is not None
    ]
    held = members or label is not None or summary is not None
    summary_as_of = _date(store.manifest.sources_as_of) if summary is not None else None
    header = (
        {"id": community_id, "label": label, "summary": summary, "summary_as_of": summary_as_of}
        if held
        else None
    )
    return _paged(
        store,
        notice,
        layers_used,
        members,
        key=lambda item: item["id"],
        cursor=cursor,
        cap_bytes=cap_bytes,
        community=header,
    )


def _entry(document: Any, community_id: int) -> Any:
    """A community's entry in a file keyed by its id as text. `_metadata` cannot be
    asked for: an int never formats to it."""
    return document.get(str(community_id)) if isinstance(document, dict) else None
