"""The minimum-standard tool (spec, Tools): `minimum_standard`, answered from the
evidence layer the library writes (`knowledgestore minimum-standard`).

The layer records, per repository, each of the seven points of the GOV.UK minimum
standard for publicly accessible systems as `present`, `absent` or
`not_determinable`, with cited evidence, the commit it is true of (`as_of`) and when
the GitHub settings it cites were read (`settings_read_at`). It is evidence, not a
verdict: this tool serves each point as the layer holds it and never computes or
returns a pass, a score or an overall result, however it is asked.

What is served is a fixed set of fields, not whatever a file holds. A record keeps
its `as_of`, `settings_read_at` and points; a point its `state`, `evidence` and
`reason`; a piece of evidence only the citation fields the library writes - and a
secret-scan finding only its rule, path and line - so no value a store's file might
carry reaches an answer.

A layer whose summary carries no stamp this server reads is present but
unrecognised: the tool refuses with the reason (`results.LayerUnrecognised`) and
reads nothing in the layer. A `by-repo` file with another stamp is not served, and
the answer names it. Every answer carries `meanings`: what each state does and does
not mean, and that a record is true of a point in time.
"""

import fnmatch
import json
from typing import Any

from . import results
from .locate import contained
from .results import (
    DEFAULT_MAX_RESPONSE_BYTES,
    MINIMUM_STANDARD,
    MINIMUM_STANDARD_DIR,
    MINIMUM_STANDARD_MAJORS,
    MINIMUM_STANDARD_SUMMARY,
    Known,
)
from .store import Session, Store
from .tools_graph import MAX_NAME_BYTES, ArgumentRefused, _paged, _reader, _repository, _text

LAYER = MINIMUM_STANDARD

# The standard's points and the layer's states. Source: POINTS and STATES in
# knowledge-store-builder src/knowledgestore/minimum_standard.py.
POINTS = (
    "owner",
    "security_contact",
    "no_secrets",
    "secure_by_design",
    "hygiene",
    "patching",
    "unmaintained",
)
STATES = ("present", "absent", "not_determinable")

# The citation fields the library's evidence carries, and the type each holds. A
# `no_secrets` finding is a location and a rule only.
_TEXT = ("commit", "date", "item", "path", "reason", "rule", "setting", "state")
_TEXT_LISTS = ("checked", "paths")
FINDING_FIELDS = ("line", "path", "rule")

# How many names one `repos` may hold: each is looked up, near misses and all.
MAX_REPOS = 100

# Said in every answer. Fixed text: no store text is quoted.
MEANINGS = {
    "present": "evidence was found, as cited",
    "absent": (
        "the practice is not evidenced in the repository, having looked where it would "
        "be; not that the team does not do it"
    ),
    "not_determinable": (
        "what the build could see cannot settle the point, for the reason given; "
        "not_determinable is not absent"
    ),
    "as_of": (
        "each record is true of its as_of commit and its settings_read_at, a point in "
        "time; it is not a statement about today"
    ),
    "evidence": "evidence per point only: no pass, score or overall verdict is given",
}

RECORD_NOT_READ = (
    "a by-repo file without this layer's format stamp at a major this server reads; not read"
)

# Product lookups: (answer, exact, ...) as a repository lookup gives them.
PRODUCT_DECLARED = "a declared product"
PRODUCT_UNKNOWN = "not a declared product"
NO_PRODUCTS = "no products declared"


# --- Reading the layer --------------------------------------------------------------


def _summary(store: Store) -> dict[str, Any]:
    """The summary, parsed; `{}` when the layer is absent. Read only once its stamp
    has been checked."""

    def read() -> dict[str, Any]:
        if not results.minimum_standard_readable(store):
            return {}
        document = json.loads(contained(store.root, MINIMUM_STANDARD_SUMMARY).read_bytes())
        return document if isinstance(document, dict) else {}

    return store.memo("minimum_standard.summary", read)


def _records(store: Store) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Each `by-repo` record this server reads, by file stem, served; and the stems
    of the files it does not, by their stamp. Empty when the layer is absent."""

    def read() -> tuple[dict[str, dict[str, Any]], list[str]]:
        if not results.minimum_standard_readable(store):
            return {}, []
        directory = contained(store.root, MINIMUM_STANDARD_DIR / results.BY_REPO_DIR)
        if not directory.is_dir():
            return {}, []
        files, _ = results.by_repo_directory(store, directory)
        records: dict[str, dict[str, Any]] = {}
        unread: list[str] = []
        for name in sorted(files):
            document = json.loads(files[name].read_bytes())
            if results.stamp_found(document, LAYER, MINIMUM_STANDARD_MAJORS) is not None:
                unread.append(name)
                continue
            records[name] = _served(name, document)
        return records, unread

    return store.memo("minimum_standard.records", read)


def _served(name: str, document: dict[str, Any]) -> dict[str, Any]:
    """A record as the tool serves it: the fixed fields, nothing else."""
    as_of = document.get("as_of")
    read_at = document.get("settings_read_at")
    points = document.get("points")
    points = points if isinstance(points, dict) else {}
    return {
        "repository": name,
        "as_of": {k: v for k, v in as_of.items() if k in ("commit", "date") and isinstance(v, str)}
        if isinstance(as_of, dict)
        else None,
        "settings_read_at": read_at if isinstance(read_at, str) else None,
        "points": {
            key: _point(key, points[key]) for key in POINTS if isinstance(points.get(key), dict)
        },
    }


def _point(key: str, point: dict[str, Any]) -> dict[str, Any]:
    state = point.get("state")
    reason = point.get("reason")
    evidence = point.get("evidence")
    keep = _finding if key == "no_secrets" else _citation
    return {
        "state": state if state in STATES else None,
        "evidence": [keep(e) for e in evidence if isinstance(e, dict)]
        if isinstance(evidence, list)
        else [],
        "reason": reason if isinstance(reason, str) else None,
    }


def _line(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finding(entry: dict[str, Any]) -> dict[str, Any]:
    """A secret-scan finding: where, and by which rule. Never anything else."""
    return {
        field: entry[field]
        for field in FINDING_FIELDS
        if (_line(entry.get(field)) if field == "line" else isinstance(entry.get(field), str))
    }


def _citation(entry: dict[str, Any]) -> dict[str, Any]:
    """A piece of evidence: the citation fields the library writes, each of its type."""
    kept: dict[str, Any] = {k: entry[k] for k in _TEXT if isinstance(entry.get(k), str)}
    for field in _TEXT_LISTS:
        found = entry.get(field)
        if isinstance(found, list):
            kept[field] = [v for v in found if isinstance(v, str)]
    if _line(entry.get("line")):
        kept["line"] = entry["line"]
    return kept


def _products(store: Store) -> dict[str, list[str]]:
    found = _summary(store).get("products")
    if not isinstance(found, dict):
        return {}
    return {
        name: sorted({r for r in repos if isinstance(r, str)})
        for name, repos in found.items()
        if isinstance(name, str) and isinstance(repos, list)
    }


def _notes(store: Store) -> list[str]:
    found = _summary(store).get("notes")
    return [note for note in found if isinstance(note, str)] if isinstance(found, list) else []


# --- Scopes -------------------------------------------------------------------------


def _names(repos: str | list[str]) -> list[str]:
    """The names in `repos`, a list of names or (for callers on v0.2.1) comma-separated
    text, each bounded, once each, in order. Only the text is split on commas: a list's
    elements are names as given, and one holding a comma is looked up as it stands."""
    if isinstance(repos, str):
        repos = _text(repos, "repos").split(",")
    if not isinstance(repos, list) or not all(isinstance(item, str) for item in repos):
        raise ArgumentRefused(
            "repos must be a list of repository names (a comma-separated string is also accepted)"
        )
    texts = [_text(item, "repos") for item in repos]
    names = list(dict.fromkeys(text.strip() for text in texts if text.strip()))
    if not names:
        raise ArgumentRefused("repos is empty: name at least one repository")
    if len(names) > MAX_REPOS:
        raise ArgumentRefused(f"repos must name at most {MAX_REPOS} repositories")
    return [_repository(name, "repos") for name in names]


def _bounded(value: str, what: str) -> str:
    """A pattern or product name: not blank, and bounded as a repository name is."""
    text = _text(value, what)
    if not text.strip():
        raise ArgumentRefused(f"{what} is empty: give a name")
    if len(text.encode("utf-8")) > MAX_NAME_BYTES:
        raise ArgumentRefused(f"{what} must be at most {MAX_NAME_BYTES} bytes")
    return text


def _product(store: Store, name: str) -> tuple[dict[str, Any], list[str]]:
    """The answer for a product name, and its repositories (none when unknown)."""
    declared = _products(store)
    if not declared:
        return {"answer": NO_PRODUCTS, "exact": None, "near_misses": []}, []
    found = name if name in declared else None
    if found is None:
        folded = [p for p in declared if p.casefold() == name.casefold()]
        found = folded[0] if len(folded) == 1 else None
    if found is None:
        misses = results.near_misses(name, sorted(declared))
        return {
            "answer": PRODUCT_UNKNOWN,
            "exact": None,
            "near_misses": [{"product": p, "near_miss": True} for p in misses],
        }, []
    repos = declared[found]
    return {"answer": PRODUCT_DECLARED, "exact": found, "repositories": repos}, repos


# --- The tool -----------------------------------------------------------------------


def _scoped(
    store: Store,
    names: list[str] | None,
    glob: str | None,
    product: str | None,
    held: list[str],
) -> tuple[list[str], list[str], dict[str, Any]]:
    """The names in scope, the layers the scope read besides this one, and the
    fields that say how the scope was resolved. `held` is every name the layer has a
    `by-repo` file for, read or not."""
    if names is not None:
        answers = [results.repository(store, name) for name in names]
        known = [answer for answer in answers if isinstance(answer, Known)]
        consulted = [_reader(store).layer]
        for answer in known:
            consulted += results.answer_layers(answer)
        return (
            [answer.name for answer in known],
            consulted,
            {"repositories": [answer.as_dict() for answer in answers]},
        )
    if glob is not None:
        return [name for name in held if fnmatch.fnmatchcase(name, glob)], [], {}
    if product is not None:
        answer, wanted = _product(store, product)
        return wanted, [], {"product": answer}
    return list(held), [], {}


def minimum_standard(
    session: Session,
    repos: str | list[str] | None = None,
    pattern: str | None = None,
    product: str | None = None,
    cursor: str | None = None,
    *,
    cap_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Each repository's points against the minimum standard, with their evidence,
    `as_of` and `settings_read_at`, sorted by repository and paged under the cap.

    Scoped by one of: `repos`, a list of names (or comma-separated text), each looked up
    as `find_repo` looks it up; `pattern`, a glob (`*`, `?`) over the names the layer holds; or
    `product`, a name in the summary's `products` map. With none, the whole layer.
    An unknown repository or product is answered with its near misses and no
    record; a repository in scope with no record is named under `no_record`, and
    one whose file this server does not read under `records_not_read`."""
    scopes = {"repos": repos, "pattern": pattern, "product": product}
    given = [name for name, value in scopes.items() if value is not None]
    if len(given) > 1:
        raise ArgumentRefused("give at most one of repos, pattern and product")
    names = _names(repos) if repos is not None else None
    glob = _bounded(pattern, "pattern") if pattern is not None else None
    product = _bounded(product, "product") if product is not None else None
    store, notice = session.current()
    reason = results.minimum_standard_unrecognised(store)
    if reason is not None:
        raise results.LayerUnrecognised(reason)
    records, unread = _records(store)
    wanted, consulted, fields = _scoped(store, names, glob, product, sorted({*records, *unread}))
    fields["scope"] = given[0] if given else "estate"
    missing = [name for name in wanted if name not in records and name not in unread]
    if missing:
        fields["no_record"] = missing
    not_read = [name for name in unread if name in wanted]
    if not_read:
        fields["records_not_read"] = {"repositories": not_read, "reason": RECORD_NOT_READ}
    items = [records[name] for name in dict.fromkeys(wanted) if name in records]
    if not items:
        fields["searched"] = results.searched(store, [*consulted[:1], LAYER])
    readable = [LAYER] if results.minimum_standard_readable(store) else []
    return _paged(
        store,
        notice,
        [*consulted, *readable],
        items,
        key=lambda item: item["repository"],
        cursor=cursor,
        cap_bytes=cap_bytes,
        meanings=MEANINGS,
        notes=_notes(store),
        **fields,
    )
