"""The store a call is answered from, and the check that it is still current.

`Session.current()` re-reads the small manifest on every call and compares its
`digest`, which is computed from the layers' content: a rebuild or pull that
changes the store reloads it, and a clone or identical rebuild does not. A
manifest without a digest is compared by the sha256 of its bytes instead.

A pull can write the new manifest before the new index archive, so each call also
stats the archive. A changed (size, mtime) costs one hash, and reloads only when
the archive's content is not the one the loaded index came from.
"""

import gzip
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import digests, index, manifest
from .digests import DigestCheck
from .index import INDEX_ARCHIVE, Fallback, IndexHandle
from .locate import LISTED, MANIFEST, PLUGIN_ROOT, AmbiguousStoreId, OutsideStore, contained
from .manifest import Manifest, Refused

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Notice:
    """Something about the store the result must say, beside the answer."""

    message: str


RELOADED = Notice("reloaded: store digest changed")
RELOADED_ARCHIVE = Notice("reloaded: index archive changed")
DEFERRED = Notice("reload deferred: manifest unreadable")
ESCAPING = Notice("reload refused: a layer path leaves the store; still serving the previous store")
SWITCHED = Notice("reloaded: now serving the registered clone")
AMBIGUOUS = Notice("reload deferred: more than one registered clone carries this store id")


@dataclass(frozen=True)
class Relocate:
    """How a session served from the plugin root finds and opens the clone the
    asker registered later. `registered` maps a `store.id` to the clone the
    locations file lists for it, or None, and raises `AmbiguousStoreId` when more
    than one carries it; it may answer None without looking again while nothing
    it reads has changed (`locate.Registered`). `open` opens a store at a root."""

    registered: Callable[[str], Path | None]
    open: Callable[[Path], "Store"]


class LayerEscapes(Refused):
    """A declared layer path resolves outside the store. Kept apart from other
    refusals so a reload that meets it can say so rather than "unreadable"."""


class Store:
    """One loaded store. Layers other than the index are read on first use.

    A `Store` is valid until `Session.current()` reloads: the reload closes its
    index connection. Take the store from `current()` per call; do not keep it.
    """

    def __init__(
        self,
        root: Path,
        manifest: Manifest,
        index: IndexHandle | Fallback,
        archive_state: tuple[int, int] | None = None,
    ):
        self.root = root
        self.manifest = manifest
        self.index = index
        # The archive's (size, mtime_ns) when the index was opened.
        self.archive_state = archive_state
        self._layers: dict[str, Any] = {}
        self._memo: dict[str, Any] = {}
        self._closers: list[Callable[[], None]] = []

    def layer(self, name: str) -> Any:
        """The parsed JSON of a file layer, or None when the manifest says it is
        absent. The path comes from the manifest and stays inside the store."""
        if name not in self._layers:
            self._layers[name] = self.read(name)
        return self._layers[name]

    def memo(
        self,
        key: str,
        compute: Callable[[], Any],
        *,
        close: Callable[[Any], None] | None = None,
    ) -> Any:
        """`compute()` once per loaded store: a reload builds a new `Store`, so
        anything kept here is read again from the store that replaced it. `close`,
        if given, is called on the value when this store is closed."""
        if key not in self._memo:
            value = compute()
            self._memo[key] = value
            if close is not None:
                self._closers.append(lambda: close(value))
        return self._memo[key]

    def digest_check(self) -> DigestCheck:
        """Each layer file against its manifest digest, hashed on first use and held
        for this store: a reload builds a new store, so it is checked again. The
        index archive's digest is taken from the open handle, which hashed the
        archive as it read it, rather than hashed a second time."""
        known: dict[str, str] = {}
        if isinstance(self.index, IndexHandle) and self.index.archive_sha256:
            known[INDEX_ARCHIVE.as_posix()] = self.index.archive_sha256[:16]
        return self.memo("digests", lambda: digests.check(self.root, self.manifest, known))

    def read(self, name: str) -> Any:
        """`layer(name)` without keeping it: for a layer turned into another form at
        once, so the parsed JSON can be freed rather than held beside that form."""
        entry = self.manifest.layers.get(name)
        if not isinstance(entry, dict) or entry.get("present") is not True:
            return None
        for rel in entry.get("paths") or []:
            if rel.endswith("/"):
                raise ValueError(f"layer {name} is a directory, not a file")
            path = contained(self.root, rel)
            if path.is_file():
                data = path.read_bytes()
                if path.suffix == ".gz":
                    data = gzip.decompress(data)
                return json.loads(data)
        return None

    def close(self) -> None:
        if isinstance(self.index, IndexHandle):
            self.index.close()
        for closer in self._closers:
            closer()
        self._closers.clear()


def open_store(
    root: Path, cache_home: Path, *, max_index_bytes: int = index.DEFAULT_MAX_INDEX_BYTES
) -> Store:
    loaded = manifest.load(root)
    _refuse_escaping_layers(root, loaded)
    # Stat before opening: a change in between is then seen as a change next call.
    state = index.archive_state(root)
    opened = index.open_index(root, loaded, cache_home, max_bytes=max_index_bytes)
    return Store(root, loaded, opened, state)


def _declared_paths(found: Manifest) -> list[tuple[str, Any]]:
    """(layer, path) for every path the manifest declares for a present layer: each
    library layer's `paths`, and each extra layer's `path`."""
    declared: list[tuple[str, Any]] = []
    for name, entry in sorted(found.layers.items()):
        if isinstance(entry, dict) and entry.get("present") is True:
            paths = entry.get("paths")
            declared.extend((name, rel) for rel in (paths if isinstance(paths, list) else []))
    for name, entry in sorted(found.extra_layers.items()):
        if isinstance(entry, dict) and entry.get("present") is True:
            declared.append((name, entry.get("path")))
    return declared


def _refuse_escaping_layers(root: Path, found: Manifest) -> None:
    """Refuse a store any of whose declared layer paths resolves outside it: a
    symlinked layer is a hostile or broken store, not one to serve in part. The
    files inside a layer directory are checked as they are read, and an escaping
    one is skipped there. The refusal names the layer and its declared path, never
    where it resolves to."""
    for name, rel in _declared_paths(found):
        if not isinstance(rel, str) or not rel:
            continue
        try:
            contained(root, rel)
        except OutsideStore:
            raise LayerEscapes(
                f"layer {name!r} at {rel!r}, which resolves outside the store",
                manifest.SUPPORTED_MAJORS,
            ) from None


def _manifest_sha256(root: Path) -> str | None:
    try:
        return hashlib.sha256(contained(root, MANIFEST).read_bytes()).hexdigest()
    except (OSError, OutsideStore):
        return None


class Session:
    """The current store, reloaded when the manifest digest on disk changes.

    A `Store` returned by `current()` stays valid until a later call reloads.

    `source` is the rule that chose the store (`locate.SOURCES`). A store chosen
    through the plugin root gives way to the clone the locations file lists for
    the same `store.id`, once one is listed (`relocate`): the asker's setup
    registers its clone after the server has started. No other source switches,
    and a switch is never undone.
    """

    def __init__(
        self,
        store_factory: Callable[[], Store],
        clock: Callable[[], datetime],
        *,
        source: str | None = None,
        relocate: Relocate | None = None,
    ):
        self._factory = store_factory
        self._clock = clock
        self._store = store_factory()
        self._manifest_sha256 = _manifest_sha256(self._store.root)
        self._archive_state = self._store.archive_state
        self.source = source
        self._relocate = relocate

    def digest(self) -> str | None:
        """The served copy's identity, as the reload check compares it: the
        manifest's `digest`, or the sha256 of its bytes when it carries none."""
        return self._store.manifest.digest or self._manifest_sha256

    def current(self) -> tuple[Store, Notice | None]:
        switch = self._switch()
        if switch is SWITCHED:
            return self._store, SWITCHED
        store, notice = self._check()
        # A reload's notice comes first; a deferred switch is said when nothing else is.
        return store, notice or switch

    def _check(self) -> tuple[Store, Notice | None]:
        root = self._store.root
        try:
            on_disk = manifest.load(root)
        except Refused:
            # Mid-pull, the manifest may be half written: answer from what is loaded.
            return self._store, DEFERRED
        if on_disk.digest or self._store.manifest.digest:
            manifest_changed = on_disk.digest != self._store.manifest.digest
        else:
            manifest_changed = _manifest_sha256(root) != self._manifest_sha256
        if manifest_changed:
            return self._reload(RELOADED)
        state = index.archive_state(root)
        if state != self._archive_state:
            loaded = self._store.index
            known = loaded.archive_sha256 if isinstance(loaded, IndexHandle) else None
            if state is None or known is None or index.archive_sha256(root) != known:
                return self._reload(RELOADED_ARCHIVE)
            # Touched, not changed: remember the new stat so it is hashed once.
            self._archive_state = state
        return self._store, None

    def _switch(self) -> Notice | None:
        """Open the registered clone in place of the plugin root's copy: `SWITCHED`
        when it did, `AMBIGUOUS` when more than one clone carries the store id, else
        None. The locations file is read only while the plugin root is served. A
        clone that cannot be opened, for any reason, is not switched to: the plugin
        copy keeps answering, so a bad clone never makes serving worse."""
        if self.source != PLUGIN_ROOT or self._relocate is None:
            return None
        relocate, store_id = self._relocate, self._store.manifest.store_id
        try:
            clone = relocate.registered(store_id)
        except AmbiguousStoreId:
            return AMBIGUOUS
        if clone is None or clone == self._store.root:
            return None
        # Hash before loading, as `_reload` does.
        manifest_sha256 = _manifest_sha256(clone)
        try:
            fresh = relocate.open(clone)
        except Exception:
            log.warning(
                "the registered clone could not be opened; serving the plugin copy", exc_info=True
            )
            return None
        if fresh.manifest.store_id != store_id:
            # Rewritten since the locations file was read: not the same store.
            fresh.close()
            return None
        previous, self._store = self._store, fresh
        self._factory = lambda: relocate.open(clone)
        self._manifest_sha256 = manifest_sha256
        self._archive_state = fresh.archive_state
        self.source = LISTED
        previous.close()
        return SWITCHED

    def _reload(self, notice: Notice) -> tuple[Store, Notice | None]:
        # Hash before loading: a change in between then shows as a change next call.
        manifest_sha256 = _manifest_sha256(self._store.root)
        try:
            fresh = self._factory()
        except LayerEscapes:
            return self._store, ESCAPING
        except Refused:
            return self._store, DEFERRED
        previous, self._store = self._store, fresh
        self._manifest_sha256 = manifest_sha256
        self._archive_state = fresh.archive_state
        previous.close()
        return fresh, notice

    def stale_days(self) -> int | None:
        """Whole days from `sources_as_of` to the clock's now, or None if unrecorded."""
        recorded = self._store.manifest.sources_as_of
        if not recorded:
            return None
        try:
            as_of = datetime.fromisoformat(recorded)
        except ValueError:
            return None
        now = self._clock()
        if as_of.tzinfo is None or now.tzinfo is None:
            # An instant without an offset cannot be placed against another.
            return None
        return (now - as_of).days
