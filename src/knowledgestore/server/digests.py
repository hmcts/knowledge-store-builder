"""Whether each layer's file is still the one the manifest's digest describes.

The server takes a store's identity and freshness from its manifest. A manifest
written before a layer file was rewritten or restored still claims the old
digest, and nothing else says so. So each single-file layer that records a
digest - library layers and extra layers alike - is hashed the library's way and
compared. `Store.digest_check` runs this on the first `store_info` of each loaded
store, never at open: hashing at open would slow start-up, and a reload after a
pull runs inside whichever tool call notices it.

A directory layer is not checked. Its digest is taken over the files a git clone
of the store receives (knowledge-store-builder v0.19.1 `store_manifest._probe`),
which depends on the store's ignore rules, and the server cannot reproduce that
faithfully. Those layers are named as unchecked, so the omission is visible.

Every field holds layer names only, never a path or file contents.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .locate import OutsideStore, contained
from .manifest import Manifest

CHUNK = 1 << 20


@dataclass(frozen=True)
class DigestCheck:
    """Layer names, each list sorted: a file whose digest differs from the
    manifest's, a declared file not on disk, and a directory layer not checked."""

    mismatch: list[str]
    missing: list[str]
    unchecked: list[str]


def file_digest(path: Path) -> str:
    """The first 16 hex digits of the file's sha256, streamed in 1 MiB chunks so a
    large layer is never read whole: knowledge-store-builder v0.19.1
    `store_manifest._file_digest`."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _declared(found: Manifest) -> list[tuple[str, str, str]]:
    """(layer, path, recorded digest) for every present layer that records a
    digest. A library layer's digest is of its first path, the one the library
    chose; an extra layer has one path."""
    declared: list[tuple[str, str, str]] = []
    for name, entry in found.layers.items():
        rel = _first(entry.get("paths") if isinstance(entry, dict) else None)
        recorded = _recorded(entry)
        if rel is not None and recorded is not None:
            declared.append((name, rel, recorded))
    for name, entry in found.extra_layers.items():
        rel = entry.get("path") if isinstance(entry, dict) else None
        recorded = _recorded(entry)
        if isinstance(rel, str) and rel and recorded is not None:
            declared.append((name, rel, recorded))
    return declared


def _first(paths: Any) -> str | None:
    if isinstance(paths, list) and paths and isinstance(paths[0], str) and paths[0]:
        return paths[0]
    return None


def _recorded(entry: Any) -> str | None:
    if not isinstance(entry, dict) or entry.get("present") is not True:
        return None
    digest = entry.get("digest")
    return digest if isinstance(digest, str) and digest else None


def check(root: Path, found: Manifest, known: Mapping[str, str] | None = None) -> DigestCheck:
    """Hash each declared single-file layer under `root` and compare. A file
    that is gone, not a file, unreadable or outside the store is missing, never a
    mismatch. `known` maps a store-relative path to a digest already taken of it,
    which is used rather than hashing the file again."""
    known = known or {}
    mismatch: set[str] = set()
    missing: set[str] = set()
    unchecked: set[str] = set()
    for name, rel, recorded in _declared(found):
        if rel.endswith("/"):
            unchecked.add(name)
            continue
        try:
            path = contained(root, rel)
            if not path.is_file():
                missing.add(name)
            elif (known.get(rel) or file_digest(path)) != recorded:
                mismatch.add(name)
        except (OSError, OutsideStore):
            missing.add(name)
    return DigestCheck(sorted(mismatch), sorted(missing), sorted(unchecked))
