"""Find the one store this process serves, and keep every path inside it.

The root is `realpath`-resolved once, here. Every later path is joined to it and
passed through `contained`, so a symlink or `..` in a store cannot lead a read
outside the clone.
"""

import argparse
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MANIFEST = Path("knowledge") / "store-manifest.json"

# `store.id` becomes one directory name, so it must be one plain path component.
STORE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

SOURCES = ("--store", "KNOWLEDGE_STORE_PATH", "--store-id", "CLAUDE_PLUGIN_ROOT")
# The source of a clone found through the locations file: by `--store-id`, or by a
# switch from the plugin root to the clone registered for the same store.
LISTED, PLUGIN_ROOT = SOURCES[2], SOURCES[3]


@dataclass(frozen=True)
class Resolved:
    root: Path
    source: str


class NotFound(Exception):
    """No rule resolved a store. `checked` names every place looked at."""

    def __init__(self, checked: list[str]):
        self.checked = checked
        super().__init__("no knowledge store found. Checked: " + "; ".join(checked))


class AmbiguousStoreId(Exception):
    """Two locations entries carry the same `store.id`, so neither can be picked."""

    def __init__(self, store_id: str, paths: list[Path]):
        self.store_id = store_id
        self.paths = paths
        super().__init__(
            f"store id {store_id!r} is carried by more than one clone in the locations file: "
            + ", ".join(str(p) for p in paths)
            + ". Remove the entries you do not mean, or start with --store PATH."
        )


class OutsideStore(Exception):
    """A path resolved, after symlinks, to somewhere outside the store root."""

    def __init__(self, root: Path, rel: str | Path, resolved: Path):
        self.resolved = resolved
        super().__init__(f"{rel} resolves to {resolved}, which is outside the store at {root}")


def contained(root: Path, rel: str | Path) -> Path:
    """`root / rel` after `realpath`, or `OutsideStore` if it left `root`.

    `root` must already be a `realpath`-resolved path (as `resolve` returns).
    """
    resolved = (root / rel).resolve()
    if resolved != root and root not in resolved.parents:
        raise OutsideStore(root, rel, resolved)
    return resolved


def _has_manifest(root: Path) -> bool:
    try:
        return contained(root, MANIFEST).is_file()
    except OutsideStore:
        return False


def _store_id_of(root: Path) -> str | None:
    """The `store.id` in the clone's manifest, or None if it cannot be read as one."""
    if not _has_manifest(root):
        return None
    try:
        document = json.loads(contained(root, MANIFEST).read_text(encoding="utf-8"))
        store_id = document["store"]["id"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return store_id if isinstance(store_id, str) else None


def locations_file(home: Path) -> Path:
    """Fixed at ~/.config: the library's skill appends there whatever XDG_CONFIG_HOME says."""
    return home / ".config" / "knowledge-store" / "locations"


def _listed_clones(path: Path) -> tuple[list[Path], list[str]]:
    """Clone roots in file order, and a reason for each entry skipped.

    Blank lines and `#` comments are not entries. A comment is never an absolute
    path, so it would be skipped as one; it is dropped first so it is not counted.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return [], []
    clones: list[Path] = []
    skipped: list[str] = []
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        candidate = Path(entry)
        # The locations file is the operator's own configuration, and naming a
        # directory to serve is its purpose: each clone is then read only through
        # `contained`, which resolves before it compares.
        if not candidate.is_absolute():
            skipped.append(f"{entry} (not absolute)")
        elif not candidate.is_dir():  # NOSONAR(S6549) the operator's own locations file
            skipped.append(f"{entry} (not a directory)")
        else:
            clones.append(candidate.resolve())  # NOSONAR(S6549) the operator's own locations file
    return clones, skipped


def _matching(store_id: str, clones: list[Path]) -> Path | None:
    matches: list[Path] = []
    for clone in clones:
        # The same clone listed twice, or by a symlinked path, is one entry.
        if _store_id_of(clone) == store_id and clone not in matches:
            matches.append(clone)
    if len(matches) > 1:
        raise AmbiguousStoreId(store_id, matches)
    return matches[0] if matches else None


def _by_store_id(store_id: str, listing: Path) -> tuple[Path | None, list[str]]:
    clones, skipped = _listed_clones(listing)
    return _matching(store_id, clones), skipped


def _stat(path: Path) -> tuple[int, int] | None:
    """(mtime_ns, size), or None when there is nothing to stat."""
    try:
        found = path.stat()
    except OSError:
        return None
    return found.st_mtime_ns, found.st_size


class Registered:
    """The clone the locations file lists for a `store.id`, looked up again only
    when something it depends on has changed.

    The key is the locations file's (mtime_ns, size) and that of each listed
    clone's manifest, as the last scan found them. A call whose key is unchanged
    returns None without reading anything but those stats: the last scan found no
    clone to switch to, and nothing it read has moved. A clone pulled after it was
    listed changes its manifest's stat, so it is found without a locations write.
    """

    def __init__(self, home: Path):
        self._listing = locations_file(home)
        self._clones: list[Path] = []
        self._seen: tuple | None = None

    def _key(self, clones: list[Path]) -> tuple:
        return _stat(self._listing), tuple(_stat(clone / MANIFEST) for clone in clones)

    def __call__(self, store_id: str) -> Path | None:
        """The listed clone carrying `store_id`, or None. Raises `AmbiguousStoreId`
        when more than one does; that too is not looked at again until it changes."""
        if self._seen is not None and self._key(self._clones) == self._seen:
            return None
        # Stat before reading: a change in between is then seen as a change next call.
        listing = _stat(self._listing)
        clones, _ = _listed_clones(self._listing)
        self._clones = clones
        self._seen = (listing, tuple(_stat(clone / MANIFEST) for clone in clones))
        return _matching(store_id, clones)


def resolve(args: argparse.Namespace, env: Mapping[str, str], home: Path) -> Resolved:
    """The spec's order; the first rule that resolves wins."""
    checked: list[str] = []

    explicit = getattr(args, "store", None)
    if explicit:
        return Resolved(Path(explicit).resolve(), "--store")
    checked.append("--store (not given)")

    from_env = env.get("KNOWLEDGE_STORE_PATH")
    if from_env:
        return Resolved(Path(from_env).resolve(), "KNOWLEDGE_STORE_PATH")
    checked.append("KNOWLEDGE_STORE_PATH (not set)")

    store_id = getattr(args, "store_id", None)
    if store_id:
        found, why = _from_listing(store_id, home)
        if found is not None:
            return Resolved(found, "--store-id")
        checked.append(why)
    else:
        checked.append("--store-id (not given)")

    plugin_root = env.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        found, why = _from_plugin_root(plugin_root, store_id)
        if found is not None:
            return Resolved(found, "CLAUDE_PLUGIN_ROOT")
        checked.append(why)
    else:
        checked.append("CLAUDE_PLUGIN_ROOT (not set)")

    raise NotFound(checked)


def _from_listing(store_id: str, home: Path) -> tuple[Path | None, str]:
    """The listed clone holding `store_id`, or None and what was checked."""
    listing = locations_file(home)
    found, skipped = _by_store_id(store_id, listing)
    if found is not None:
        return found, ""
    note = f"no clone with that store.id listed in {listing}"
    if skipped:
        note += f"; {len(skipped)} entries skipped: " + ", ".join(skipped)
    return None, f"--store-id {store_id!r} ({note})"


def _from_plugin_root(plugin_root: str, store_id: str | None) -> tuple[Path | None, str]:
    """The plugin root when it holds a store, and the one asked for if any; or None
    and what was checked."""
    root = Path(plugin_root).resolve()
    held = _store_id_of(root)
    if _has_manifest(root) and (not store_id or held == store_id):
        return root, ""
    if _has_manifest(root):
        return None, f"CLAUDE_PLUGIN_ROOT holds store {held!r}, not {store_id!r}"
    return None, f"CLAUDE_PLUGIN_ROOT ({root} holds no {MANIFEST.as_posix()})"
