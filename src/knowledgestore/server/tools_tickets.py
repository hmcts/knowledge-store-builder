"""The ticket and answer tools (spec, Tools): `tickets`, `ticket` and `answers`.

Each is a plain function of `(Session, ...) -> dict`, as the graph tools are, and
the server registers them.

**Landed and proposed are never merged.** The ticket layer the library writes
(`build_intent_index`, `file-tickets.json.gz`) keeps, per file, the tickets whose
commits landed (`tickets`) apart from those seen only on a branch (`proposed`). The
tools answer with two lists, `landed` and `proposed`, paged together under one cap
(`_two_lists`), so no answer puts branch work in the landed list. How far the
separation can be trusted is the manifest's `intent.reach` flag, returned as
recorded: when it is not `true`, some history could not tell the two apart, and
the answer says so.

**The tracker's record is kept apart.** When the manifest's `ticket_tracker`
layer is present, `ticket` returns the tracker's own record of the key - title,
status, resolution, description and comments, as knowledge-store-builder's
`fetch-tickets` stage caches them, dated by `as_of`: the day the stage last asked
the tracker, or for a resolved record, which is final and never asked again, the
day it resolved; null with neither - under
`tracker`, beside the commit-mined record under
`ticket` and never merged into it, so an answer can say which source it quotes.
A layer that is absent, a directory, or not a JSON object of records, gives
null, and `tracker_note` says why. Tracker text is written prose, not a
mechanical layer.

**Store text is data.** Ticket descriptions, commit subjects and bodies, and the
titles and bodies of written answers, and the tracker's titles, statuses,
descriptions and comments, were written by people. They are returned
only in fields named `text`, `summary` or `description`, never in a sentence the
server composes, and never bound into SQL other than as a parameter. What is
structure - ticket keys, paths, repository names, branch names (which git forbids
spaces in) - is returned in its own field.

**Written answers are dated.** Each carries `as_of`, when it was written, and
`commit`, the store commit that last touched it, where its entry records them.
The library's briefs and deep dives record neither, and nothing in the store
records the build that wrote them, so they are dated by the manifest's
`sources_as_of`: the newest source of the build that holds them, which the prose
may predate. Only a calendar date (`YYYY-MM-DD`, with an optional `T` and time)
is passed as a date, whichever of these it came from. Anything else undated is null, and is presented as undated. Both
fields are written by hand in an answers layer, so only a date and a hex commit
id are passed; anything else is null.

**Answers cite a store-relative path.** A written answer is cited by its layer,
its id and the `source` path its entry declares, with the manifest's `store.id`.
The server reads files and never runs git, and the manifest carries no URL for
the store's own repository, so there is nothing to pin a URL with.
"""

import gzip
import html
import json
import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import queries, results
from .index import IndexHandle
from .locate import contained
from .manifest import KNOWN_LAYERS
from .results import DEFAULT_MAX_RESPONSE_BYTES, Known
from .store import Notice, Session, Store
from .tools_graph import (
    ArgumentRefused,
    _count,
    _date,
    _paged,
    _repository,
    _repository_layers,
    _search_words,
    _text,
    _words,
)

DEFAULT_ANSWERS_LIMIT = 10

# The library's answer layers. Extra layers the manifest declares with
# `"kind": "answers"` are searched beside them.
ANSWER_LAYERS = ("topics", "deep_dives")

# Said beside the lists when `intent.reach` is not `true`.
REACH_NOTE = (
    "the store does not record that every repository's history separates landed "
    "from branch-only work, so the landed list may include work that never landed"
)

# Common English words, as FTS5 `unicode61` folds them. They never count toward an
# answer's score: OR-matched, they would rank an answer for "how" and "the" alone.
STOPWORDS = frozenset(
    "a an and are as at be by can do does for from has how in is it of on or that "
    "the this to was what when where which who why will with".split()
)

# A query word shorter than this matches a whole word only: as a prefix, `le` would
# match most of the store.
PREFIX_MIN = 3

# Why `ticket` returns no tracker record. Fixed text: no store text is quoted.
TRACKER = "ticket_tracker"
TRACKER_ABSENT = "the ticket_tracker layer is absent"
TRACKER_UNRECORDED = "the ticket_tracker layer is not recorded"
TRACKER_NO_RECORD = "the ticket_tracker layer holds no record of this ticket"
TRACKER_RECORD_UNREAD = (
    "the ticket_tracker layer's record of this ticket is in a shape this server does not read"
)
TRACKER_NOT_RECORDS = (
    "ticket_tracker layer present but not a JSON object of ticket records; not read"
)
TRACKER_NOT_JSON = "ticket_tracker layer present but not valid JSON; not read"
TRACKER_A_DIRECTORY = (
    "ticket_tracker layer present but a directory: it is read as one JSON file of "
    "ticket records; not read"
)

# A tracker record's text, under `tracker.text`: (record field, name returned).
_TRACKER_TEXT = (
    ("summary", "title"),
    ("status", "status"),
    ("resolution", "resolution"),
    ("description", "description"),
)

# Why an answers layer was not searched.
A_DIRECTORY = "a directory: an answers layer is read as one JSON file of entries"
NOT_ENTRIES = "not a JSON object of entries"
NOT_JSON = "not valid JSON"

# The bytes two lists add to a response over the one list `results.page` measures:
# `"landed":[],"proposed":[]` in place of `"items":[]`. Nothing else differs, so a
# page cut to the cap less this always fits the cap once split.
_SPLIT_MARGIN = len(results.encode({"landed": [], "proposed": []})) - len(
    results.encode({"items": []})
)

# An answer entry's id, and a `source`, are returned as structure only when they
# have its shape: an id is one plain segment, a source a relative path of plain
# segments (`_plain_segment`). Prose has spaces, so it cannot pass for either.
_MARKUP = re.compile(r"<[^>]*>")

# A commit id as git writes one: lowercase hex, abbreviated to seven or more.
_COMMIT = re.compile(r"[0-9a-f]{7,64}", re.ASCII)

# The library's metadata block in the briefs and deep-dives files: not an entry.
_METADATA = "_metadata"

# (repository, path, ticket, commits, landed)
Row = tuple[str, str, str, int, bool]


@dataclass(frozen=True)
class _Unreadable:
    """Why a layer cannot be read - as answers, or as tracker records: one of this
    module's own reasons, never text from the store."""

    reason: str


# --- Reading the ticket layer ----------------------------------------------------

# The index's ticket rows come from the library's documented queries, run verbatim
# (`queries.TICKETS_FOR_FILE` for a repository or one path of it, and
# `queries.FILES_FOR_TICKET`). Each is reshaped to a `Row` here; the lists are
# ordered by the tool's own key when paged, so the SQL's order is not relied on.


def _layer_rows(layer: Any) -> list[Row]:
    """The ticket layer's rows as the index builder stages them
    (`store_index._ticket_rows`): per file, landed tickets first, and a ticket in
    both sections of one file counted once, as landed."""
    rows: list[Row] = []
    for repo, paths in (layer if isinstance(layer, dict) else {}).items():
        if not isinstance(paths, dict):
            continue
        for path, entry in paths.items():
            if isinstance(entry, dict):
                rows.extend(_file_rows(repo, path, entry))
    return rows


def _file_rows(repo: str, path: str, entry: dict[str, Any]) -> list[Row]:
    """One file's rows of the ticket layer: landed tickets first, and a ticket in
    both sections counted once, as landed."""
    rows: list[Row] = []
    seen: set[str] = set()
    for section, landed in (("tickets", True), ("proposed", False)):
        counts = entry.get(section)
        for ticket, count in (counts if isinstance(counts, dict) else {}).items():
            if ticket not in seen:
                seen.add(ticket)
                rows.append((repo, path, ticket, count, landed))
    return rows


def _rows(
    store: Store,
    sql: str,
    parameters: tuple,
    shape: Callable[[tuple], Row],
    keep: Callable[[Row], bool],
) -> list[Row]:
    """The rows `sql` selects from the index when it is open, each made a `Row` by
    `shape`; otherwise the ticket layer's rows that `keep` accepts, read once per
    loaded store."""
    if isinstance(store.index, IndexHandle):
        return [shape(row) for row in store.index.connection.execute(sql, parameters)]
    every = store.memo("intent.rows", lambda: _layer_rows(store.read("intent")))
    return [row for row in every if keep(row)]


def _present(store: Store, name: str) -> bool:
    entry = store.manifest.layers.get(name)
    return isinstance(entry, dict) and entry.get("present") is True


def _descriptions(store: Store) -> dict[str, Any] | None:
    """The ticket records by key, or None when the layer is absent."""
    if not _present(store, "ticket_descriptions"):
        return None
    document = store.layer("ticket_descriptions")
    return document if isinstance(document, dict) else {}


def _reach(store: Store) -> dict[str, Any]:
    entry = store.manifest.layers.get("intent")
    reach = entry.get("reach") if isinstance(entry, dict) else None
    return {"reach": reach, "reach_note": None if reach is True else REACH_NOTE}


def _searched(store: Store, layers: list[str], *also: str) -> str:
    return results.searched(store, sorted({*layers, *also}))


def _two_lists(
    store: Store,
    notice: Notice | None,
    layers_used: list[str],
    landed: list[dict[str, Any]],
    proposed: list[dict[str, Any]],
    *,
    key: Callable[[dict[str, Any]], Any],
    cursor: str | None,
    cap_bytes: int,
    **fields: Any,
) -> dict[str, Any]:
    """One page of `landed` and `proposed`, returned as those two lists in `key`
    order, with `totals` for each. The pager measures one list; the page is cut to
    the cap less the bytes the second list adds, so the response as sent stays
    within the cap, and `oversize` is judged again on that response. (The pager's
    own flag already agrees: it flags only a page of one item or none, where one
    list is empty and the split adds exactly the margin. It is recomputed so that
    nothing rests on that.)"""
    marked = {id(item) for item in proposed}
    response = _paged(
        store,
        notice,
        layers_used,
        [*landed, *proposed],
        key=key,
        cursor=cursor,
        cap_bytes=cap_bytes - _SPLIT_MARGIN,
        totals={"landed": len(landed), "proposed": len(proposed)},
        **fields,
    )
    items = response.pop("items")
    response.pop("oversize", None)
    response["landed"] = [item for item in items if id(item) not in marked]
    response["proposed"] = [item for item in items if id(item) in marked]
    if len(results.encode(response)) > cap_bytes:
        response["oversize"] = True
    return response


# --- The ticket tools -----------------------------------------------------------


def tickets(
    session: Session,
    repo: str,
    path: str | None = None,
    cursor: str | None = None,
    *,
    cap_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """The tickets whose commits touched `repo` (or one `path` in it), as two
    lists: `landed`, and `proposed` - seen only on a branch - each with its
    `branches` (null when the descriptions layer is absent). Each item is a ticket
    and a file, with the number of commits. `reach` is the manifest's
    `intent.reach`, as recorded; when it is not `true`, `reach_note` says the
    landed list may hold branch work."""
    name = _repository(repo, "repo")
    if path is not None:
        path = _text(path, "path")
    store, notice = session.current()
    indexed = isinstance(store.index, IndexHandle)
    answer = results.repository(store, name)
    layers_used = _repository_layers(store, "index" if indexed else "graph", answer)
    if not indexed and _present(store, "intent"):
        # Read for the estate's names in every case, and for the lists.
        layers_used.append("intent")
    rows: list[Row] = []
    if isinstance(answer, Known):
        found = answer.name
        rows = _rows(
            store,
            queries.TICKETS_FOR_FILE,
            (found, path),
            # Columns: ticket, count, landed, repo, path.
            lambda r: (r[3], r[4], r[0], r[1], bool(r[2])),
            lambda row: row[0] == found and (path is None or row[1] == path),
        )
    landed = [{"ticket": t, "path": p, "commits": n} for _, p, t, n, done in rows if done]
    proposed = [{"ticket": t, "path": p, "commits": n} for _, p, t, n, done in rows if not done]
    if proposed:
        records = _descriptions(store)
        if records is not None:
            layers_used.append("ticket_descriptions")
        for item in proposed:
            item["branches"] = _branches(records, item["ticket"])
    fields: dict[str, Any] = {"repository": answer.as_dict(), **_reach(store)}
    if not rows:
        fields["searched"] = _searched(store, layers_used, "intent")
    return _two_lists(
        store,
        notice,
        layers_used,
        landed,
        proposed,
        key=lambda item: (item["ticket"], item["path"]),
        cursor=cursor,
        cap_bytes=cap_bytes,
        **fields,
    )


def _branches(records: dict[str, Any] | None, key: str) -> list[str] | None:
    """A ticket's branches; None when there is no record to say."""
    record = records.get(key) if records is not None else None
    if not isinstance(record, dict):
        return None
    branches = record.get("branches")
    return list(branches) if isinstance(branches, list) else []


def _ticket_layers(store: Store, indexed: bool) -> list[str]:
    """The layer `ticket` reads its files from: the index when open, else the
    ticket layer when present, else none."""
    if indexed:
        return ["index"]
    if _present(store, "intent"):
        return ["intent"]
    return []


def ticket(
    session: Session,
    key: str,
    cursor: str | None = None,
    *,
    cap_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """One ticket: whether the record says it never landed (`proposed`), its
    `branches`, its descriptions, and - under `text` - its commit subjects and
    bodies, with a landed ticket's branch-only subjects and bodies kept apart; then
    the files it touched, as `landed` and `proposed` lists. `ticket` is null when no
    record holds the key."""
    key = _text(key, "ticket key")
    store, notice = session.current()
    indexed = isinstance(store.index, IndexHandle)
    layers_used = _ticket_layers(store, indexed)
    records = _descriptions(store)
    if records is not None:
        layers_used.append("ticket_descriptions")
    record = records.get(key) if records is not None else None
    rows = _rows(
        store,
        queries.FILES_FOR_TICKET,
        (key,),
        # Columns: repo, path, count, landed.
        lambda r: (r[0], r[1], key, r[2], bool(r[3])),
        lambda row: row[2] == key,
    )
    landed = [{"repository": r, "path": p, "commits": n} for r, p, _, n, done in rows if done]
    proposed = [{"repository": r, "path": p, "commits": n} for r, p, _, n, done in rows if not done]
    tracker = _tracker(store)
    if isinstance(tracker, dict):
        layers_used.append(TRACKER)
    found, note = _tracker_record(store, tracker, key)
    fields: dict[str, Any] = {
        "ticket": _record(key, record),
        "tracker": found,
        "tracker_note": note,
        **_reach(store),
    }
    if not rows and fields["ticket"] is None and found is None:
        # An unrecognised tracker layer was not searched: its reason is the note.
        also = [] if isinstance(tracker, _Unreadable) else [TRACKER]
        fields["searched"] = _searched(store, layers_used, "intent", "ticket_descriptions", *also)
    return _two_lists(
        store,
        notice,
        layers_used,
        landed,
        proposed,
        key=lambda item: (item["repository"], item["path"]),
        cursor=cursor,
        cap_bytes=cap_bytes,
        **fields,
    )


# The record's commit text, under `text`: (record field, name returned).
_TEXT_FIELDS = (
    ("s", "subjects"),
    ("b", "bodies"),
    ("ps", "branch_only_subjects"),
    ("pb", "branch_only_bodies"),
)


def _record(key: str, record: Any) -> dict[str, Any] | None:
    """A ticket record as `build_intent_index._ticket_record` writes it, renamed
    so its text sits only in `description` and `text`. A field the record omits
    is omitted here too: the library writes text fields only when it found some."""
    if not isinstance(record, dict):
        return None
    branches = record.get("branches")
    return {
        "key": key,
        "proposed": record.get("proposed") is True,
        "branches": list(branches) if isinstance(branches, list) else [],
        "description": record.get("d", []),
        "text": {name: record[field] for field, name in _TEXT_FIELDS if field in record},
        "first": record.get("first"),
        "last": record.get("last"),
        "repos": record.get("repos"),
        "commits": record.get("n"),
    }


def _tracker(store: Store) -> dict[str, Any] | _Unreadable | None:
    """The tracker layer's records by key; why the layer is not read; or None when
    the manifest does not record it as present. Read once per loaded store."""
    if not _present(store, TRACKER):
        return None
    return store.memo("tracker.records", lambda: _read_tracker(store))


def _read_tracker(store: Store) -> dict[str, Any] | _Unreadable:
    if _tracker_is_a_directory(store):
        return _Unreadable(TRACKER_A_DIRECTORY)
    try:
        document = store.read(TRACKER)
    except (ValueError, EOFError, gzip.BadGzipFile, zlib.error):
        # Text that is not UTF-8 is a `ValueError` too.
        return _Unreadable(TRACKER_NOT_JSON)
    # A declared file that is missing reads as None here, and so is not records.
    if not isinstance(document, dict):
        return _Unreadable(TRACKER_NOT_RECORDS)
    return document


def _tracker_is_a_directory(store: Store) -> bool:
    """Whether the manifest declares the tracker layer at a directory: listed as one
    (a trailing `/`), or a path that is one. Its paths stay inside the store
    (`store._refuse_escaping_layers`)."""
    entry = store.manifest.layers.get(TRACKER)
    paths = entry.get("paths") if isinstance(entry, dict) else None
    return any(
        isinstance(rel, str) and (rel.endswith("/") or contained(store.root, rel).is_dir())
        for rel in (paths if isinstance(paths, list) else [])
    )


def _tracker_record(
    store: Store, tracker: dict[str, Any] | _Unreadable | None, key: str
) -> tuple[dict[str, Any] | None, str | None]:
    """The tracker's record of `key` as `ticket` returns it, and the note that says
    why there is none. A layer whose presence the manifest did not record is said
    to be not recorded, never absent."""
    if isinstance(tracker, _Unreadable):
        return None, tracker.reason
    if tracker is None:
        entry = store.manifest.layers.get(TRACKER)
        unrecorded = isinstance(entry, dict) and entry.get("present") is None
        return None, TRACKER_UNRECORDED if unrecorded else TRACKER_ABSENT
    if key not in tracker:
        return None, TRACKER_NO_RECORD
    record = tracker[key]
    state = _tracker_state(record)
    if state is None:
        return None, TRACKER_RECORD_UNREAD
    return {
        "key": key,
        "state": state,
        # When the stage last asked the tracker about it; a resolved record is
        # final, never asked again and carries no `checked`, so when it resolved.
        "as_of": _date(record.get("checked")) or _date(record.get("resolved")),
        "text": _tracker_text(record) if state == "fetched" else {},
        "comments_truncated": record.get("comments_truncated") is True,
    }, None


def _tracker_text(record: dict[str, Any]) -> dict[str, Any]:
    """A fetched record's text as the layer holds it. A field the record omits, or
    holds as another type, is omitted: the stage drops empty fields, so absence
    here means the tracker gave none."""
    text: dict[str, Any] = {
        name: record[field] for field, name in _TRACKER_TEXT if isinstance(record.get(field), str)
    }
    comments = record.get("comments")
    if isinstance(comments, list):
        text["comments"] = [comment for comment in comments if isinstance(comment, str)]
    return text


def _tracker_state(record: Any) -> str | None:
    """Which of `fetch-tickets`' cached outcomes a record is: `fetched` (it holds
    the tracker's `summary`), `absent` (the tracker has no such ticket) or
    `denied` (the build could not read it); None for any other shape."""
    if not isinstance(record, dict):
        return None
    if isinstance(record.get("summary"), str):
        return "fetched"
    if record.get("absent") is True:
        return "absent"
    if record.get("denied") is True:
        return "denied"
    return None


# --- Written answers ------------------------------------------------------------


def answers(
    session: Session,
    query: str,
    limit: int = DEFAULT_ANSWERS_LIMIT,
    cursor: str | None = None,
    *,
    cap_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Written answers - topic briefs, deep dives and extra layers declared with
    `"kind": "answers"` - whose title or body holds a word beginning with a word
    of `query`, case and Latin accents ignored. Common words (`STOPWORDS`) never
    count, a query word under three letters matches a whole word only, and a query
    of common words alone is refused. Ranked by how many of the query's other
    words match, then by id, then layer; the first `limit` are returned, paged,
    and `matches` counts them all. Each match carries its layer, id, the
    store-relative `source` its entry declares (null when that is not a plain
    relative path), the store's id, its title as `summary` and its body as
    `text`."""
    words = [w for w in dict.fromkeys(_search_words(query, "query")) if w not in STOPWORDS]
    if not words:
        raise ArgumentRefused(
            "query holds only common words (such as how, the, does), which never count "
            "toward a match: add a word that names the subject"
        )
    limit = _count(limit, "limit", "a whole number of 1 or more", lambda n: n >= 1)
    store, notice = session.current()
    library = store.memo("answers.library", lambda: _library(store))
    scored = []
    for layer, entry_id, terms, item in library.entries:
        score = sum(1 for word in words if _matches(word, terms))
        if score:
            scored.append((-score, entry_id, layer, {**item, "score": score}))
    scored.sort(key=lambda found: found[:3])
    ranked = [item for *_, item in scored[:limit]]
    rank = {(item["layer"], item["id"]): position for position, item in enumerate(ranked)}
    fields: dict[str, Any] = {"matches": len(scored)}
    if library.not_read:
        fields["not_read"] = library.not_read
    if library.skipped:
        fields["entries_skipped"] = library.skipped
    if not ranked:
        fields["searched"] = _answers_searched(store, library)
    return _paged(
        store,
        notice,
        library.read,
        ranked,
        key=lambda item: rank[(item["layer"], item["id"])],
        cursor=cursor,
        cap_bytes=cap_bytes,
        **fields,
    )


def _matches(word: str, terms: frozenset[str]) -> bool:
    if len(word) < PREFIX_MIN:
        return word in terms
    return any(term.startswith(word) for term in terms)


def _answers_searched(store: Store, library: "_Library") -> str:
    """What an empty `answers` searched: `searched 4 answers in the deep_dives, faq
    and topics layers; the context layer is absent`. Built from counts and layer
    names `results` accepts as nameable, so no store text reaches it."""
    count = len(library.entries)
    noun = "answer" if count == 1 else "answers"
    present = library.read
    if present:
        layer = "layer" if len(present) == 1 else "layers"
        parts = [f"searched {count} {noun} in the {results.join_names(present)} {layer}"]
    else:
        parts = ["searched no layer"]
    nameable = results.nameable_layers(store)
    absent, unrecorded = [], []
    for name in library.searched:
        if name in present:
            continue
        entry = nameable[name]
        state = entry.get("present") if isinstance(entry, dict) else False
        (unrecorded if state is None else absent).append(name)
    if absent:
        parts.append(results.layers_clause(absent, "absent"))
    if unrecorded:
        parts.append(results.layers_clause(unrecorded, "not recorded"))
    return "; ".join(parts)


class _Library:
    """Every written answer in the store, read once per loaded store."""

    def __init__(self) -> None:
        # (layer, id, words, item)
        self.entries: list[tuple[str, str, frozenset[str], dict[str, Any]]] = []
        self.searched: list[str] = []
        self.read: list[str] = []
        self.not_read: dict[str, str] = {}
        self.skipped = 0


def _library(store: Store) -> _Library:
    library = _Library()
    nameable = results.nameable_layers(store)
    extras = sorted(name for name in nameable if name not in KNOWN_LAYERS)
    for layer in sorted([*ANSWER_LAYERS, *extras]):
        entry = nameable[layer]
        if not isinstance(entry, dict) or entry.get("present") is not True:
            # Searched and found absent, or unrecorded: `searched` says which.
            library.searched.append(layer)
            continue
        document = store.layer(layer) if layer in ANSWER_LAYERS else _extra(store, entry)
        if isinstance(document, _Unreadable):
            library.not_read[layer] = document.reason
            continue
        if not isinstance(document, dict):
            library.not_read[layer] = NOT_ENTRIES
            continue
        library.searched.append(layer)
        library.read.append(layer)
        _add_answers(library, store, layer, document)
    return library


def _add_answers(library: _Library, store: Store, layer: str, document: dict) -> None:
    """Each answer of one layer's document into `library`; an id that is not a
    plain segment is counted as skipped, never listed."""
    for entry_id, answer in document.items():
        if entry_id == _METADATA or not isinstance(answer, dict):
            continue
        if not _plain_segment(entry_id):
            library.skipped += 1
            continue
        library.entries.append(_entry(store, layer, entry_id, answer))


def _entry(
    store: Store, layer: str, entry_id: str, answer: dict[str, Any]
) -> tuple[str, str, frozenset[str], dict[str, Any]]:
    """One written answer as `_Library.entries` holds it: its words are those of
    its title and its body with the markup removed."""
    title, body = answer.get("title"), answer.get("html")
    title = title if isinstance(title, str) else ""
    body = body if isinstance(body, str) else ""
    plain = html.unescape(_MARKUP.sub(" ", body))
    item = {
        "layer": layer,
        "id": entry_id,
        "store": store.manifest.store_id,
        "source": _plain_path(answer.get("source")),
        "summary": title,
        "text": body,
        **_dated(store, layer, answer),
    }
    return (layer, entry_id, frozenset(_words(f"{title} {plain}")), item)


def _dated(store: Store, layer: str, answer: dict[str, Any]) -> dict[str, Any]:
    """An answer's `as_of` and `commit`: its own, where it records them; for the
    library's briefs and deep dives, which record no date, the store's
    `sources_as_of`; otherwise null."""
    as_of = _date(answer.get("as_of"))
    if as_of is None and layer in ANSWER_LAYERS:
        as_of = _date(store.manifest.sources_as_of)
    commit = answer.get("commit")
    return {
        "as_of": as_of,
        "commit": commit if isinstance(commit, str) and _COMMIT.fullmatch(commit) else None,
    }


def _extra(store: Store, entry: dict[str, Any]) -> Any:
    """An extra layer's parsed file, or why it cannot be read as answers. Its path
    comes from the manifest and stays inside the store."""
    rel = entry.get("path")
    if not isinstance(rel, str) or not rel:
        return _Unreadable(NOT_ENTRIES)
    path = contained(store.root, rel)
    if path.is_dir():
        return _Unreadable(A_DIRECTORY)
    data = path.read_bytes()
    try:
        if path.suffix == ".gz":
            data = gzip.decompress(data)
        return json.loads(data)
    except (ValueError, EOFError, gzip.BadGzipFile, zlib.error):
        # One malformed layer is reported, not fatal: the others still answer. Text
        # that is not UTF-8 is a `ValueError` too. A missing file is not caught.
        return _Unreadable(NOT_JSON)


def _plain_segment(segment: str) -> bool:
    """One path segment or id: letters and digits in any script, `.`, `_` and `-`,
    and not dots alone (`.`, `..`, `...`). A control character, a separator or a
    space is none of these."""
    return bool(segment.strip(".")) and all(c.isalnum() or c in "._-" for c in segment)


def _plain_path(value: Any) -> str | None:
    """`value` when it is a relative path of plain segments, else None."""
    if not isinstance(value, str) or not all(map(_plain_segment, value.split("/"))):
        return None
    return value
