"""Resolve JSON Schema `$ref`s against an estate-wide index of declared ids.

Pure: no graph, no filesystem, no fetching. A reference resolves to a file and
a pointer the stage already read, or to a named reason it did not.

Which repositories are forks is a parameter, not a read of configuration: the
caller owns that policy (the stage reads `KSB_FORK_REPOS`), and this module stays
testable.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from knowledgestore.json_schema import Ref, Walked, escape_pointer, normalise_uri

_META_SCHEMA_HOST = "json-schema.org"

Candidate = tuple[str, str, str]  # (repo, rel, pointer)


@dataclass(frozen=True)
class Owned:
    repo: str
    rel: str  # POSIX repository-relative path
    basis: str
    walked: Walked = field(compare=False, hash=False)


@dataclass(frozen=True)
class Target:
    repo: str
    rel: str
    pointer: str  # the entity's pointer
    target_pointer: str  # the full pointer resolved


@dataclass(frozen=True)
class Resolution:
    target: Target | None
    how: str  # local, anchor, by-id, relative-id, relative-path, or the unresolved reason
    rule: str = ""
    candidates: tuple[str, ...] = ()


class Index:
    """Declared ids across every owned file, and the owned files by path."""

    def __init__(self, by_id: dict[str, list[Candidate]], by_path: dict[tuple[str, str], Owned]):
        self.by_id = by_id
        self.by_path = by_path

    @classmethod
    def build(cls, owned: list[Owned]) -> Index:
        by_id: dict[str, list[Candidate]] = {}
        by_path: dict[tuple[str, str], Owned] = {}
        for item in sorted(owned, key=lambda o: (o.repo, o.rel)):
            by_path[(item.repo, item.rel)] = item
            seen: set[str] = set()
            for uri, pointer in item.walked.declared:
                if uri in seen:  # a file is one candidate, at its first declaration
                    continue
                seen.add(uri)
                by_id.setdefault(uri, []).append((item.repo, item.rel, pointer))
        for candidates in by_id.values():
            candidates.sort()
        return cls(by_id, by_path)


def choose(
    candidates: list[Candidate], repo: str, rel: str, forks: frozenset[str]
) -> tuple[Candidate | None, str]:
    """Pick the file that owns an id declared by `candidates`, or report a tie."""
    candidates = sorted(candidates)
    mine = [c for c in candidates if (c[0], c[1]) == (repo, rel)]
    if mine:
        return mine[0], "self"
    if len(candidates) == 1:
        return candidates[0], "only"
    same_repository = [c for c in candidates if c[0] == repo]
    if len(same_repository) == 1:
        return same_repository[0], "same-repository"
    non_forks = [c for c in candidates if c[0] not in forks]
    if len(non_forks) == 1:
        return non_forks[0], "not-a-fork"
    return None, "ambiguous"


def resolve(index: Index, source: Owned, ref: Ref, forks: frozenset[str]) -> Resolution:
    """Resolve one reference found in `source`."""
    value = ref.ref
    if ref.keyword != "$ref" or not isinstance(value, str):
        return Resolution(None, "malformed")
    try:
        split = urlsplit(value)
        absolute = bool(split.scheme)
        is_meta = absolute and _is_meta_schema(value)
        joined = urljoin(ref.base, value) if ref.base and not absolute else None
        if joined is not None and _is_meta_schema(joined):
            is_meta = True
    except ValueError:
        return Resolution(None, "malformed")
    if value.startswith("#"):
        return _resolve_local(source, split.fragment)
    if is_meta:
        return Resolution(None, "meta-schema")
    if absolute:
        found = _resolve_by_id(index, source, value, "by-id", forks)
        return found if found is not None else Resolution(None, "external")
    if joined is not None:
        found = _resolve_by_id(index, source, joined, "relative-id", forks)
        if found is not None:
            return found
    return _resolve_by_path(index, source, split.path, split.fragment)


def _is_meta_schema(uri: str) -> bool:
    return (urlsplit(uri).hostname or "").lower() == _META_SCHEMA_HOST


def _resolve_local(source: Owned, fragment: str) -> Resolution:
    named = bool(fragment) and not fragment.startswith("/")
    return _land(source, "", fragment, "anchor" if named else "local", "")


def _resolve_by_id(
    index: Index, source: Owned, uri: str, how: str, forks: frozenset[str]
) -> Resolution | None:
    base, fragment = urldefrag(uri)
    candidates = index.by_id.get(normalise_uri(base))
    if not candidates:
        return None
    listed = tuple(f"{c[0]}:{c[1]}" for c in sorted(candidates))
    chosen, rule = choose(candidates, source.repo, source.rel, forks)
    if chosen is None:
        return Resolution(None, "ambiguous", rule, listed)
    owner = index.by_path[(chosen[0], chosen[1])]
    return _land(owner, chosen[2], fragment, how, rule)


def _resolve_by_path(index: Index, source: Owned, path: str, fragment: str) -> Resolution:
    rel = posixpath.normpath(posixpath.join(posixpath.dirname(source.rel), unquote(path)))
    if not path or rel == ".." or rel.startswith("../") or rel.startswith("/"):
        return Resolution(None, "unrecognised-target")
    owner = index.by_path.get((source.repo, rel))
    if owner is None:
        return Resolution(None, "unrecognised-target")
    return _land(owner, "", fragment, "relative-path", "")


def _tokens(pointer: str) -> tuple[str, ...]:
    """Split an RFC 6901 pointer into unescaped reference tokens."""
    if not pointer:
        return ()
    return tuple(t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:])


def _pointer(tokens: tuple[str, ...]) -> str:
    return "".join("/" + escape_pointer(t) for t in tokens)


def _step(node: object, token: str) -> tuple[bool, object]:
    if isinstance(node, dict):
        return (token in node, node.get(token))
    if isinstance(node, list) and token.isdigit() and (token == "0" or token[0] != "0"):
        position = int(token)
        if position < len(node):
            return True, node[position]
    return False, None


def _land(owner: Owned, base_pointer: str, fragment: str, how: str, rule: str) -> Resolution:
    """Walk `fragment` from `base_pointer` in `owner` and land on the enclosing entity."""
    if fragment and not fragment.startswith("/"):
        pointer = owner.walked.anchors.get(unquote(fragment))
        if pointer is None:
            return Resolution(None, "no-such-pointer")
        tokens = _tokens(pointer)
    else:
        tokens = _tokens(base_pointer) + _tokens(unquote(fragment))
    node = owner.walked.document
    for token in tokens:
        found, node = _step(node, token)
        if not found:
            return Resolution(None, "no-such-pointer")
    best: str | None = None
    best_length = -1
    for entity in owner.walked.entities:
        entity_tokens = _tokens(entity.pointer)
        if tokens[: len(entity_tokens)] == entity_tokens and len(entity_tokens) > best_length:
            best, best_length = entity.pointer, len(entity_tokens)
    if best is None:
        return Resolution(None, "no-such-pointer")
    target = Target(owner.repo, owner.rel, best, _pointer(tokens))
    return Resolution(target, how, rule)
