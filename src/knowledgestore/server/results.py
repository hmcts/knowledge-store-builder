"""What every tool returns around its answer (spec, Tools).

- `envelope` says which store, version and index answered, and which layers.
- `searched` and `empty`: an empty result always says what was searched, with
  the store's own counts, and which layers were absent or unrecognised.
- `relationships_unrecognised` checks the relationships registry's format stamp
  on first use: a layer without one this server reads is present but
  unrecognised, never read, and refused by every tool that needs it.
  `minimum_standard_unrecognised` does the same for the minimum-standard layer's
  summary.
- `repository` is the three-way answer for a repository name: in the estate, not
  in the estate (with near misses), or referenced by the estate but not held.
  The estate is every repository the index lists, provenance records, or the
  relationships layer has a `by-repo` file for.
- `page` cuts a list so the whole response fits the cap, without cutting an
  item, and says so.

Store text is data. The only prose composed here is `NO_GRAPH_NODES`, which is
fixed, and `searched`'s sentence, built
from counts and layer names: the ones this server knows, and answers layers the
manifest declares, which are store structure and must be plain identifiers; and
an unrecognised layer's reason, which quotes a stamp only as integers and a plain
identifier. So
nothing a person wrote into the store can reach it; every store value goes in a
named field.
"""

import difflib
import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import graph
from .index import IndexHandle, UnrecognisedIndex
from .locate import OutsideStore, contained
from .manifest import KNOWN_LAYERS, unrecognised
from .store import Notice, Store

# The response cap. The spec sets it above the largest per-repository file a store
# holds, and that figure belongs to a consuming store; this default is overridden
# by `--max-response-bytes`.
DEFAULT_MAX_RESPONSE_BYTES = 512 * 1024

# The relationships registry, at the library's fixed layout, read from here rather
# than from the manifest.
REGISTRY = Path("knowledge") / "relationships" / "repository-dependencies.json"

# The per-repository files' directory, inside the layer's. knowledge-store-builder
# 0.18.0 names the layer by its directory (`knowledge/relationships/`), which also
# holds the registry, `kinds.json` and any extra layer a store keeps there; a
# manifest edited to name `by-repo/` itself is read as naming this directory.
BY_REPO_DIR = "by-repo"

# A layer name a sentence may carry: one plain identifier.
_LAYER_NAME = re.compile(r"[A-Za-z0-9_-]+")

# Plan rulings, Near misses are deterministic.
NEAR_MISS_COUNT = 5
NEAR_MISS_CUTOFF = 0.75

# What a repository the estate holds and the graph does not is answered with. Fixed
# text: the repository's name and sha go in their own fields.
NO_GRAPH_NODES = "in the estate, but the graph holds no nodes for it"

INDEX_PRESENT = "present"
INDEX_ABSENT = "absent: graph read whole"
INDEX_UNRECOGNISED = "present but format unrecognised: graph read whole"

# The relationships registry's format majors this server reads. Source: FORMAT in
# knowledge-store-builder src/knowledgestore/relationships.py (#455, PR #460),
# written as the registry's top-level `"format": {"layer", "major", "minor"}`.
RELATIONSHIPS = "relationships"
RELATIONSHIPS_MAJORS = (1,)

# The minimum-standard layer, at the library's fixed layout. Its stamp is the
# summary's top-level `"format": {"layer", "major", "minor"}`. Source: FORMAT and
# LAYER in knowledge-store-builder src/knowledgestore/minimum_standard.py, and
# MINIMUM_STANDARD_STAMP in its store_manifest.py.
MINIMUM_STANDARD = "minimum_standard"
MINIMUM_STANDARD_MAJORS = (1,)
MINIMUM_STANDARD_DIR = Path("knowledge") / "minimum-standard"
MINIMUM_STANDARD_SUMMARY = MINIMUM_STANDARD_DIR / "summary.json"


# A lone UTF-16 surrogate, which `json.loads` accepts from `"\\ud800"` but UTF-8
# cannot carry.
_SURROGATE = re.compile("[\ud800-\udfff]")


def encode(value: Any) -> bytes:
    """The bytes a response is sent as, and so the bytes the cap measures: compact,
    key-sorted UTF-8 JSON. A lone surrogate in store text becomes U+FFFD, the
    replacement character, rather than failing the whole response.

    The cap bounds this text, the tool result the model reads. The JSON-RPC frame
    around it (the envelope, and an escape before every quote) is transport, and
    is not counted."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _SURROGATE.sub("\ufffd", text).encode("utf-8")


# An absolute path on this host, consumed whole:
# - a `file:` URI;
# - a quoted path, to its closing quote, so a space inside it is part of it;
# - an unquoted path: a `/` that starts a word, or follows a colon (`error:/x`),
#   but not `//` (a URL's `https://`), with every following space-separated piece
#   that still holds a `/` (`/x/Application Support/y`).
_PIECE = r"[^\s'\",;()]"
_HOST_PATH = re.compile(
    rf"file:/+{_PIECE}*(?:\s+{_PIECE}*/{_PIECE}*)*"
    r"|'/[^']*'"
    r'|"/[^"]*"'
    rf"|(?<![\w.~/-])/(?!/){_PIECE}+(?:\s+{_PIECE}*/{_PIECE}*)*"
)


def _replace(match: re.Match[str]) -> str:
    found = match.group(0)
    quote = found[0] if found[0] in "'\"" else ""
    return f"{quote}<host path>{quote}"


def without_host_paths(text: str) -> str:
    """`text` with every absolute path replaced: a message the server composes
    around an OS or SQLite error never says where anything is on this host. A
    quoted path keeps its quotes."""
    return _HOST_PATH.sub(_replace, text)


# --- The envelope -----------------------------------------------------------------


def _index_state(store: Store) -> str:
    if isinstance(store.index, IndexHandle):
        return INDEX_PRESENT
    if isinstance(store.index, UnrecognisedIndex):
        return INDEX_UNRECOGNISED
    return INDEX_ABSENT


def envelope(store: Store, layers_used: list[str], notice: Notice | None) -> dict:
    found = store.manifest
    return {
        "store": {
            "id": found.store_id,
            "format": {"major": found.major, "minor": found.minor},
            "library_version": found.library_version,
            "sources_as_of": found.sources_as_of,
            "index": _index_state(store),
            "layers_used": sorted(set(layers_used)),
            "notice": notice.message if notice else None,
        }
    }


# --- Unrecognised layers ----------------------------------------------------------


class LayerUnrecognised(ValueError):
    """A tool needs a layer that is present in a format this server does not read.
    The message is `manifest.unrecognised`'s sentence, which quotes no store text."""


def _present(store: Store, name: str) -> bool:
    entry = store.manifest.layers.get(name)
    return isinstance(entry, dict) and entry.get("present") is True


# Why a present, stamped layer with no per-repository file is not read. Fixed text:
# no store path is quoted.
NO_BY_REPO_FILES = (
    f"{RELATIONSHIPS} layer present but holds no per-repository files "
    f"({BY_REPO_DIR}/<repo>.json); not read"
)


def relationships_unrecognised(store: Store) -> str | None:
    """Why the relationships layer is present but not read, or None when it is
    absent or readable. Checked once per loaded store, on first use: the registry's
    stamp before anything else in the layer is read, then that the layer holds a
    per-repository file at all. A layer with none would otherwise answer every
    repository as "in the estate" with nothing depending on it."""
    if not _present(store, RELATIONSHIPS):
        return None
    return store.memo("relationships.unrecognised", lambda: _check_relationships(store))


def _check_relationships(store: Store) -> str | None:
    found = _stamp_found(store)
    if found is not None:
        return unrecognised(RELATIONSHIPS, found, RELATIONSHIPS_MAJORS)
    return None if by_repo_files(store) else NO_BY_REPO_FILES


def _stamp_found(store: Store) -> str | None:
    return store.memo("relationships.stamp", lambda: _relationships_stamp_found(store))


def _relationships_stamp_found(store: Store) -> str | None:
    return _stamp_found_in(store, REGISTRY, "registry", RELATIONSHIPS, RELATIONSHIPS_MAJORS)


def _stamp_found_in(
    store: Store, rel: Path, what: str, layer_name: str, majors: tuple[int, ...]
) -> str | None:
    """None when the file at `rel`, the layer's `what`, carries a stamp for
    `layer_name` at one of `majors`; otherwise what was found, in this server's own
    words. Only a layer name that is a plain identifier, and integers, are quoted."""
    path = contained(store.root, rel)
    if not path.is_file():
        return "none"
    try:
        document = json.loads(path.read_bytes())
    except ValueError:
        return f"a {what} that is not JSON"
    return stamp_found(document, layer_name, majors)


def stamp_found(document: Any, layer_name: str, majors: tuple[int, ...]) -> str | None:
    """None when `document` carries a top-level stamp for `layer_name` at one of
    `majors`; otherwise what was found, in this server's own words."""
    stamp = document.get("format") if isinstance(document, dict) else None
    if not isinstance(stamp, dict):
        return "none"
    layer, major, minor = stamp.get("layer"), stamp.get("major"), stamp.get("minor")
    if layer != layer_name:
        if isinstance(layer, str) and _LAYER_NAME.fullmatch(layer):
            return f"a stamp for the {layer} layer"
        return "a stamp for another layer"
    if isinstance(major, bool) or not isinstance(major, int):
        return "a stamp with no integer major"
    if major in majors:
        return None
    shown = minor if isinstance(minor, int) and not isinstance(minor, bool) else "x"
    return f"{major}.{shown}"


def minimum_standard_unrecognised(store: Store) -> str | None:
    """Why the minimum-standard layer is present but not read, or None when it is
    absent or readable: its summary's stamp, checked once per loaded store, before
    anything else in the layer is read."""
    if not _present(store, MINIMUM_STANDARD):
        return None

    def check() -> str | None:
        found = _stamp_found_in(
            store, MINIMUM_STANDARD_SUMMARY, "summary", MINIMUM_STANDARD, MINIMUM_STANDARD_MAJORS
        )
        if found is None:
            return None
        return unrecognised(MINIMUM_STANDARD, found, MINIMUM_STANDARD_MAJORS)

    return store.memo("minimum_standard.unrecognised", check)


def minimum_standard_readable(store: Store) -> bool:
    """The minimum-standard layer is present and in a format this server reads."""
    return _present(store, MINIMUM_STANDARD) and minimum_standard_unrecognised(store) is None


def relationships_readable(store: Store) -> bool:
    """The relationships layer is present and in a format this server reads."""
    return _present(store, RELATIONSHIPS) and relationships_unrecognised(store) is None


def require_relationships(store: Store) -> None:
    """Refuse, with the reason, a tool that needs a relationships layer this server
    cannot read: an unrecognised layer is never answered as an empty one."""
    reason = relationships_unrecognised(store)
    if reason is not None:
        raise LayerUnrecognised(reason)


def unrecognised_layers(store: Store) -> dict[str, str]:
    """Each layer present in a format this server does not read, with the reason."""
    found: dict[str, str] = {}
    if isinstance(store.index, UnrecognisedIndex):
        found["index"] = store.index.reason
    reason = relationships_unrecognised(store)
    if reason is not None:
        found[RELATIONSHIPS] = reason
    reason = minimum_standard_unrecognised(store)
    if reason is not None:
        found[MINIMUM_STANDARD] = reason
    return found


# --- Absence ----------------------------------------------------------------------


def repository_names(store: Store) -> list[str]:
    """The estate's repositories, sorted: the repositories the index lists (or the
    graph and ticket layer read whole list), with every repository provenance
    records and every one the relationships layer has a `by-repo` file for. A
    clone with no graph node and no ticket is still in the estate. Never the empty
    name: a node with no repository is not one, whatever the index holds."""
    return _estate(store, _listed(store))


def _listed(store: Store) -> set[str]:
    """The repositories the index's `repos` lists, or, without it, the graph's and
    the ticket layer's, collected as the index builder collects them: those with
    a graph node or a ticket."""
    if isinstance(store.index, IndexHandle):
        # An index built before the library stopped listing "" still holds it;
        # `_estate` drops it in every mode.
        rows = store.index.connection.execute("SELECT name FROM repos")
        return {name for (name,) in rows}
    return set(graph.read(store).repositories()) | store.memo(
        "intent.repositories", lambda: graph.ticket_repositories(store.read("intent"))
    )


def _estate(store: Store, listed: set[str]) -> list[str]:
    names = listed | set(provenance_commits(store)) | set(by_repo_files(store))
    return sorted(name for name in names if name)


def provenance_commits(store: Store) -> dict[str, str | None]:
    """Each repository provenance records, with the sha it was cloned at (None when
    the entry carries no text sha). Read once per loaded store, through the
    store's layer reader, which keeps the path inside the store; empty when the
    layer is absent."""
    return store.memo("provenance.commits", lambda: _read_provenance(store.layer("provenance")))


def _read_provenance(document: Any) -> dict[str, str | None]:
    repositories = document.get("repositories") if isinstance(document, dict) else None
    if not isinstance(repositories, dict):
        return {}
    commits: dict[str, str | None] = {}
    for name, entry in repositories.items():
        sha = entry.get("sha") if isinstance(entry, dict) else None
        commits[name] = sha if isinstance(sha, str) else None
    return commits


@dataclass(frozen=True)
class ByRepo:
    """The `by-repo/` listing: each file by its stem, and how many entries were
    skipped because they resolve outside the store."""

    files: dict[str, Path]
    skipped: int


def by_repo_listing(store: Store) -> ByRepo:
    """The `by-repo/` directory inside the one the manifest names for the
    relationships layer, listed once per loaded store and never parsed here. An
    entry - or the directory itself - that resolves outside the store is skipped
    and counted, never followed and never quoted: one hostile link must not fail
    every lookup. Empty when the layer is absent."""
    return store.memo("relationships.by_repo", lambda: _list_by_repo(store))


def by_repo_files(store: Store) -> dict[str, Path]:
    """Each `by-repo/` file by its stem. A name is matched against these keys; it
    never becomes part of a path."""
    return by_repo_listing(store).files


def _list_by_repo(store: Store) -> ByRepo:
    # The stamp, not `relationships_readable`: that asks whether this listing is
    # empty, and an unstamped layer is never listed.
    if not _present(store, RELATIONSHIPS) or _stamp_found(store) is not None:
        return ByRepo({}, 0)
    entry = store.manifest.layers[RELATIONSHIPS]
    paths = entry.get("paths")
    skipped = 0
    for rel in paths if isinstance(paths, list) else []:
        if not isinstance(rel, str) or not rel.strip("/"):
            continue
        try:
            directory = contained(store.root, _by_repo_path(rel))
        except OutsideStore:
            skipped += 1
            continue
        if not directory.is_dir():
            continue
        files, outside = by_repo_directory(store, directory)
        return ByRepo(files, skipped + outside)
    return ByRepo({}, skipped)


def _by_repo_path(layer_path: str) -> str:
    """The store-relative `by-repo/` directory for a path the manifest names the
    layer by: the layer's directory, or `by-repo/` itself."""
    named = PurePosixPath(layer_path.rstrip("/"))
    return (named if named.name == BY_REPO_DIR else named / BY_REPO_DIR).as_posix()


def by_repo_directory(store: Store, directory: Path) -> tuple[dict[str, Path], int]:
    """The `.json` files of one `by-repo/` directory by stem, and how many entries
    resolve outside the store."""
    files: dict[str, Path] = {}
    skipped = 0
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json":
            continue
        try:
            resolved = contained(store.root, path.relative_to(store.root))
        except OutsideStore:
            skipped += 1
            continue
        if resolved.is_file():
            files[path.stem] = resolved
    return files, skipped


def join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def layers_clause(names: list[str], verb: str) -> str:
    """`the context layer is absent`, `the context and history layers are absent`."""
    noun = "layer is" if len(names) == 1 else "layers are"
    return f"the {join_names(names)} {noun} {verb}"


def nameable_layers(store: Store) -> dict[str, Any]:
    """The layers a sentence may name, with their manifest entries: the ones this
    server knows, and each `extra_layers` entry of kind `answers` whose name is a
    plain identifier."""
    names: dict[str, Any] = {name: store.manifest.layers.get(name) for name in KNOWN_LAYERS}
    for name, entry in store.manifest.extra_layers.items():
        if (
            name not in names
            and isinstance(entry, dict)
            and entry.get("kind") == "answers"
            and _LAYER_NAME.fullmatch(name)
        ):
            names[name] = entry
    return names


def searched(store: Store, layers: Sequence[str]) -> str:
    """What an empty answer searched: `searched 12 repositories in the graph layer;
    the relationships layer is absent`. A layer whose presence the store did not
    record (`present: null`) is said to be not recorded, never absent; a layer
    present in a format this server does not read is given its reason, never
    counted as searched."""
    nameable = nameable_layers(store)
    refused = set(layers) - set(nameable)
    if refused:
        # Counted, not quoted: a refused name may be anything the store holds.
        raise ValueError(f"{len(refused)} layer name(s) this server cannot name")
    unread = unrecognised_layers(store)
    present, absent, unrecorded = [], [], []
    for name in sorted(set(layers) - set(unread)):
        entry = nameable[name]
        state = entry.get("present") if isinstance(entry, dict) else False
        if state is True:
            present.append(name)
        elif state is None:
            unrecorded.append(name)
        else:
            absent.append(name)
    parts = [_searched_present(store, present)]
    if absent:
        parts.append(layers_clause(absent, "absent"))
    parts.extend(unread[name] for name in sorted(set(layers) & set(unread)))
    if unrecorded:
        parts.append(layers_clause(unrecorded, "not recorded"))
    return "; ".join(parts)


def _searched_present(store: Store, present: list[str]) -> str:
    """`searched`'s opening clause: the estate's count, in the layers present."""
    if not present:
        return "searched no layer"
    count = len(repository_names(store))
    noun = "repository" if count == 1 else "repositories"
    layer = "layer" if len(present) == 1 else "layers"
    return f"searched {count} {noun} in the {join_names(present)} {layer}"


def empty(searched: str) -> dict:
    """An empty result, which never stands alone: it says what was searched."""
    return {"items": [], "total": 0, "searched": searched}


# --- The three-way answer for a repository name ------------------------------------


@dataclass(frozen=True)
class Known:
    """In the estate. A tool may still have nothing to say about it."""

    name: str
    matched_case_insensitively: bool
    # False for a repository the index does not list - a clone with no graph node
    # and no ticket - so the answer says the graph holds nothing for it.
    listed: bool = True
    cloned_at: str | None = None

    def as_dict(self) -> dict:
        found: dict[str, Any] = {
            "answer": "in the estate",
            "exact": self.name,
            "matched_case_insensitively": self.matched_case_insensitively,
        }
        if not self.listed:
            found["graph_nodes"] = 0
            found["cloned_at"] = self.cloned_at
            found["note"] = NO_GRAPH_NODES
        return found


@dataclass(frozen=True)
class Unknown:
    """Not in the estate. Each near miss is a different repository, not the answer."""

    near_misses: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "answer": "not in the estate",
            "exact": None,
            "near_misses": [{"repository": n, "near_miss": True} for n in self.near_misses],
        }


@dataclass(frozen=True)
class NotHeld:
    """A target the estate references and no clone holds."""

    name: str
    referenced_by: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "answer": "referenced by the estate but not held",
            "exact": None,
            "target": self.name,
            "referenced_by": list(self.referenced_by),
        }


def repository(store: Store, name: str) -> Known | Unknown | NotHeld:
    """Look `name` up; it is never joined into a path."""
    listed = _listed(store)
    names = _estate(store, listed)

    def known(found: str, folded: bool) -> Known:
        if found in listed:
            return Known(found, matched_case_insensitively=folded)
        cloned_at = provenance_commits(store).get(found)
        return Known(found, folded, listed=False, cloned_at=cloned_at)

    if name in names:
        return known(name, False)
    folded = [n for n in names if n.casefold() == name.casefold()]
    if len(folded) == 1:
        return known(folded[0], True)
    if folded:
        # Case-only twins: the name cannot say which is meant, so offer each.
        return Unknown(tuple(folded))
    entries = outside_estate(store, name)
    if entries:
        referrers = {r for entry in entries for r in entry.referenced_by}
        return NotHeld(name, tuple(sorted(referrers)))
    return Unknown(near_misses(name, names))


def answer_layers(answer: Known) -> list[str]:
    """The layers a known answer quotes beyond the one that listed it: provenance,
    for the sha of a repository the graph holds nothing for."""
    return ["provenance"] if answer.cloned_at is not None else []


def _remainder(name: str) -> str:
    """`name` without its first hyphen-separated segment; "" when it has none."""
    return name.partition("-")[2]


def _similarity(candidate: str, name: str) -> float:
    # Scored as difflib scores them: the candidate is the first sequence.
    return difflib.SequenceMatcher(None, candidate, name).ratio()


def _score(candidate: str, name: str) -> float:
    """The better of the whole-name score and the score of what follows the first
    `-` in each. An equal remainder scores 1.0, which no other different name can,
    so a swapped first segment is always offered, and first."""
    whole = _similarity(candidate, name)
    rest, asked = _remainder(candidate), _remainder(name)
    if not (rest and asked):
        return whole
    return max(whole, _similarity(rest, asked))


def near_misses(name: str, names: list[str]) -> tuple[str, ...]:
    """The top five, at or above the cutoff, by similarity, a tie broken by name
    ascending. Each candidate is scored on its whole name and on what follows its
    first `-`, against the same of `name`, and keeps the better: a swapped prefix
    (`<one>-module-x` for `<other>-module-x`) is the substitution an asker makes,
    and the whole name alone ranks it below every name sharing the asker's
    prefix. Every candidate is scored first: `difflib.get_close_matches` keeps a
    tie at its fifth place by name descending, so it would drop the wrong names."""
    scored = sorted((-_score(c, name), c) for c in names)
    close = [c for negative, c in scored if -negative >= NEAR_MISS_CUTOFF]
    return tuple(close[:NEAR_MISS_COUNT])


@dataclass(frozen=True)
class Outside:
    """One target outside the estate, under one kind, as the registry holds it.
    `target` is the registry's key, which may be `<org>/<repo>` for a name asked
    without its organisation."""

    kind: str
    target: str
    references: Any
    referenced_by: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "references": self.references,
            "referenced_by": list(self.referenced_by),
        }


def outside_estate(store: Store, name: str) -> tuple[Outside, ...]:
    """Every registry entry `name` names, by (kind, target): its own key, and a
    Terraform-style `<org>/<name>` whose organisation is the store's own, compared
    ignoring case with `lower`, as the library's resolver compares it. Another
    organisation's repository of the same name is a different target."""
    return _outside_estate(store).get(name, ())


def targets_outside_estate(store: Store) -> tuple[Outside, ...]:
    """Every registry entry once, by (kind, target)."""
    unique = {(o.kind, o.target): o for found in _outside_estate(store).values() for o in found}
    return tuple(unique[key] for key in sorted(unique))


def _outside_estate(store: Store) -> dict[str, tuple[Outside, ...]]:
    """Each name a target outside the estate answers to, with its entries. Read
    once per loaded store."""
    return store.memo("relationships.outside_estate", lambda: _read_outside_estate(store))


def _outside(kind: str, target: str, detail: Any) -> Outside:
    """One registry entry, its referrers de-duplicated and sorted."""
    detail = detail if isinstance(detail, dict) else {}
    referrers = detail.get("referenced_by")
    return Outside(
        kind,
        target,
        detail.get("references"),
        tuple(sorted(set(referrers))) if isinstance(referrers, list) else (),
    )


def _outside_names(target: str, own: str) -> set[str]:
    """The names a registry target answers to: its key, and `<repo>` for a key
    `<org>/<repo>` under the store's own organisation `own` (lower-cased)."""
    names = {target}
    org, slash, rest = target.partition("/")
    if own and slash and org.lower() == own and rest and "/" not in rest:
        names.add(rest)
    return names


def _read_outside_estate(store: Store) -> dict[str, tuple[Outside, ...]]:
    if not relationships_readable(store):
        return {}
    path = contained(store.root, REGISTRY)
    if not path.is_file():
        return {}
    document = json.loads(path.read_bytes())
    by_kind = (
        document.get("unresolved_targets_outside_estate") if isinstance(document, dict) else None
    )
    # `lower`, not `casefold`: the library's resolver compares organisations so.
    own = store.manifest.github_org.lower()
    found: dict[str, list[Outside]] = {}
    for kind, targets in (by_kind if isinstance(by_kind, dict) else {}).items():
        for target, detail in (targets if isinstance(targets, dict) else {}).items():
            outside = _outside(kind, target, detail)
            for name in _outside_names(target, own):
                found.setdefault(name, []).append(outside)
    return {
        name: tuple(sorted(entries, key=lambda o: (o.kind, o.target)))
        for name, entries in found.items()
    }


# --- Paging -----------------------------------------------------------------------


# A cursor is `<page>.<first item>.<tag>`. Its token is rebuilt from the parsed
# integers and compared whole, so any other spelling of a number is refused.
# ASCII digits only, as that rebuilt token is spelt: `int` reads any Unicode digit.
_CURSOR = re.compile(r"(\d{1,9})\.(\d{1,9})\.([0-9a-f]{16})", re.ASCII)

# The fields `page` adds to the response; the frame may not carry them.
PAGING_FIELDS = ("items", "total", "page", "next_page", "oversize")


class CursorRefused(ValueError):
    """A `next_page` that does not belong to this listing of this store."""

    def __init__(self, cursor: str):
        shown = cursor if len(cursor) <= 64 else cursor[:64] + "…"
        super().__init__(
            f"page cursor {shown!r} is not from this listing of the current store "
            "(the store may have changed since): start again without a page cursor"
        )


def page(
    items: list,
    cap_bytes: int,
    cursor: str | None,
    *,
    digest: str,
    key: Callable[[Any], Any],
    frame: dict,
) -> dict:
    """The whole response for one page of `items`: `frame` (the envelope, and
    anything else the tool returns beside the list) plus `items`, `total`, `page`
    and `next_page`.

    Items sort by `key`, with the item's own bytes as the tiebreak, so equal keys
    still order the same on every call. A page holds the whole items that keep the
    response, as `encode` writes it, within `cap_bytes`. An item that does not fit
    alone is a page of its own, flagged `oversize`.

    `cursor` is None for the first page, or a `next_page` from the same listing,
    cap and store `digest`; anything else raises `CursorRefused`. It names the
    first item of its page, so a frame that grows between calls (a notice) cannot
    skip or repeat an item.
    """
    clash = sorted(set(frame) & set(PAGING_FIELDS))
    if clash:
        raise ValueError(f"the frame carries paging fields: {clash}")
    encoded = sorted(((key(item), encode(item), item) for item in items), key=lambda t: t[:2])
    total = len(encoded)
    listing = hashlib.sha256()
    for _, item_bytes, _ in encoded:
        listing.update(len(item_bytes).to_bytes(8, "big") + item_bytes)
    bound = f"{digest}\0{cap_bytes}\0{listing.hexdigest()}"

    def token(number: int, start: int) -> str:
        tag = hashlib.sha256(f"{bound}\0{number}\0{start}".encode("utf-8")).hexdigest()
        return f"{number}.{start}.{tag[:16]}"

    number, start = (1, 0) if cursor is None else _parse(cursor, token)

    def following(end: int) -> str | None:
        return token(number + 1, end) if end < total else None

    def response(end: int) -> dict:
        return {
            **frame,
            "items": [item for _, _, item in encoded[start:end]],
            "total": total,
            "page": number,
            "next_page": following(end),
        }

    def fits(end: int, body: int) -> bool:
        # The items of [start, end) add `body`, their bytes and commas, to `[]`.
        shell = {**response(start), "next_page": following(end)}
        return len(encode(shell)) + body <= cap_bytes

    end, body = start, 0
    if start < total:
        end, body = start + 1, len(encoded[start][1])
        while end < total and fits(end + 1, body + 1 + len(encoded[end][1])):
            body += 1 + len(encoded[end][1])
            end += 1
    result = response(end)
    # Over the cap only when nothing smaller could be sent: one item that does not
    # fit alone, or a frame that is over the cap by itself.
    if len(encode(result)) > cap_bytes:
        result["oversize"] = True
    return result


def _parse(cursor: str, token: Callable[[int, int], str]) -> tuple[int, int]:
    match = _CURSOR.fullmatch(cursor)
    if match is None:
        raise CursorRefused(cursor)
    number, start = int(match[1]), int(match[2])
    if token(number, start) != cursor:
        raise CursorRefused(cursor)
    return number, start
