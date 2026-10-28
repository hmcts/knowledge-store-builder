"""Open the query index read-only (spec, Start-up step 3).

The committed `store.sqlite.gz` cannot be opened by SQLite, so it is decompressed once into
`<cache_home>/knowledge-store/<store.id>/<sha256 of the archive>.sqlite`, beside
`<that name>.sha256`, the sha256 of the decompressed bytes. That cache is the
server's only write: it is never inside the store, each file is written to a temp
file in the same directory and renamed into place, so no partial entry is ever
opened, and a failure to write it falls back to the graph.

An entry is checked against its `.sha256` before it is first opened in a process,
so damage on disk is caught whatever SQLite would make of it: the entry is
written again.

The archive wins whenever it exists: the manifest's index digest describes it.
A plain `store.sqlite` is a local, gitignored build that goes stale after a pull,
so it is opened in place only when there is no archive.
"""

import gzip
import hashlib
import io
import os
import re
import sqlite3
import tempfile
import time
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .locate import STORE_ID, OutsideStore, contained
from .manifest import Manifest, unrecognised

INDEX_PLAIN = Path("knowledge") / "index" / "store.sqlite"
INDEX_ARCHIVE = Path("knowledge") / "index" / "store.sqlite.gz"

CHUNK = 1 << 20

# The index schema major this server reads. A newer minor only adds to it.
# Source: SCHEMA in knowledge-store-builder src/knowledgestore/store_index.py
# (#457, feat/query-index at e4c55a2), written to meta.index_schema as "1.0".
SCHEMA_MAJOR = 1

# A temp file this old belongs to a writer that crashed: no decompress takes a day.
# A younger one may be a live peer's write, so it is never touched.
STALE_TEMP_SECONDS = 24 * 60 * 60

# Decompression stops past this many bytes: a hostile or broken archive must not
# fill the disk. Configurable per call; the default is far above any real index.
DEFAULT_MAX_INDEX_BYTES = 8 * 1024**3


class IndexTooLarge(OSError):
    """The archive decompresses to more than the cap allows."""

    def __init__(self, cap: int):
        super().__init__(f"the index archive decompresses to more than {cap} bytes")


# The entries whose bytes matched their `.sha256` in this process, by (path, size,
# mtime_ns). Checking costs one read and hash of the whole index per process
# start; reopening an entry unchanged since it was checked costs a stat.
_VERIFIED: set[tuple[str, int, int]] = set()

# What a temp file's name holds, for an entry and for its digest.
_TEMP_MARKERS = (".sqlite.tmp-", ".sqlite.sha256.tmp-")


@dataclass(frozen=True)
class IndexHandle:
    """A read-only connection to the index, and the file it reads."""

    connection: sqlite3.Connection
    path: Path
    # The sha256 of the archive a cache entry was decompressed from; None in place.
    archive_sha256: str | None = None

    def close(self) -> None:
        self.connection.close()


@dataclass(frozen=True)
class Fallback:
    """The index is not used; `reason` says why, and the graph is read instead."""

    reason: str


class UnrecognisedIndex(Fallback):
    """The entry opened and holds an index schema this server does not read: the
    index layer is present but unrecognised, and the graph answers. The schema came
    from the archive, so decompressing it again cannot change it."""


def cache_home(env: Mapping[str, str], home: Path) -> Path:
    """`${XDG_CACHE_HOME:-~/.cache}`. A relative value is ignored, as the XDG base
    directory specification says: it would put the cache under the working
    directory, which may be the store."""
    value = env.get("XDG_CACHE_HOME") or ""
    return Path(value) if Path(value).is_absolute() else home / ".cache"


def archive_state(root: Path) -> tuple[int, int] | None:
    """The archive's (size, mtime_ns), or None when there is none: a stat, so a
    per-call freshness check can notice a new archive without hashing it."""
    try:
        info = contained(root, INDEX_ARCHIVE).stat()
    except (OSError, OutsideStore):
        return None
    return (info.st_size, info.st_mtime_ns)


def archive_sha256(root: Path) -> str | None:
    try:
        return _sha256(contained(root, INDEX_ARCHIVE))
    except (OSError, OutsideStore):
        return None


def open_index(
    root: Path,
    manifest: Manifest,
    cache_home: Path,
    *,
    max_bytes: int = DEFAULT_MAX_INDEX_BYTES,
) -> IndexHandle | Fallback:
    try:
        plain = contained(root, INDEX_PLAIN)
        archive = contained(root, INDEX_ARCHIVE)
    except OutsideStore:
        return Fallback(
            f"the index is outside the store ({INDEX_ARCHIVE.as_posix()} or {INDEX_PLAIN.name})"
        )
    if not archive.is_file():
        if plain.is_file():
            # Not immutable: a rebuild may rewrite this file while the server runs.
            return _connect(plain, INDEX_PLAIN.as_posix(), immutable=False)
        return Fallback(f"no index in the store ({INDEX_ARCHIVE.as_posix()} or {INDEX_PLAIN.name})")

    if not STORE_ID.fullmatch(manifest.store_id):
        return Fallback(f"store.id {manifest.store_id!r} cannot name a cache directory")
    directory = cache_home / "knowledge-store" / manifest.store_id
    resolved = directory.resolve()
    if resolved == root or root in resolved.parents:
        return Fallback("the index cache directory would be inside the store")
    return _open_cached(archive, directory, max_bytes)


def _open_cached(archive: Path, directory: Path, max_bytes: int) -> IndexHandle | Fallback:
    """The archive's cache entry in `directory`, written if need be, opened."""
    result: IndexHandle | Fallback = Fallback("unreachable")
    # Twice at most. An entry that will not open was either left bad by a crash or
    # removed by a peer serving another archive under this store id, after our
    # rename and before our open. Either way it is discarded and written again.
    # An entry that opens but holds an unread schema is not retried.
    for _ in range(2):
        try:
            entry = _cached(archive, directory, max_bytes)
        except IndexTooLarge as error:
            return Fallback(str(error))
        except (OSError, EOFError, zlib.error) as error:
            return Fallback(f"the index cache could not be written: {_reason(error)}")
        # Immutable is safe here: an entry is named by its content, appears only by
        # an atomic rename, and is never written again. Skipping SQLite's locking
        # also means a peer removing the entry cannot disturb an open handle.
        result = _connect(entry, "the cached index", immutable=True, archive_sha256=entry.stem)
        if isinstance(result, IndexHandle):
            return result
        if isinstance(result, UnrecognisedIndex):
            # The entry is a whole copy of the archive: keep it, and fall back now.
            return result
        try:
            _discard(entry)
        except OSError as error:
            return Fallback(
                f"{result.reason}; the bad cache entry could not be removed: {_reason(error)}"
            )
    return result


def _reason(error: BaseException) -> str:
    """Why a cache write or removal failed, without the filename an `OSError` names:
    its `strerror`, or what kind of failure it was."""
    if isinstance(error, (EOFError, zlib.error, gzip.BadGzipFile)):
        return "the archive is damaged"
    if isinstance(error, OSError) and error.strerror:
        return error.strerror
    return type(error).__name__


class _HashingReader:
    """Hashes the archive's bytes as gzip reads them, so the entry is named by the
    bytes actually decompressed even if the archive changes mid-read (a pull)."""

    def __init__(self, raw):
        self._raw = raw
        self.sha256 = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self.sha256.update(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        # gzip seeks only to rewind, which a single forward read never does.
        raise io.UnsupportedOperation("the archive is read once, forwards")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _cached(archive: Path, directory: Path, max_bytes: int) -> Path:
    """The cache entry for `archive`, decompressing it when it is absent or does
    not match its digest."""
    entry = directory / f"{_sha256(archive)}.sqlite"
    if not _verified(entry):
        _discard(entry)
        entry = _write(archive, directory, entry, max_bytes)
    _remove_others(directory, entry)
    return entry


def _digest_path(entry: Path) -> Path:
    return entry.with_name(entry.name + ".sha256")


def _stat_key(path: Path) -> tuple[str, int, int]:
    info = path.stat()
    return (str(path), info.st_size, info.st_mtime_ns)


def _verified(entry: Path) -> bool:
    """Whether `entry` holds the bytes its `.sha256` names. A missing or unreadable
    entry or digest is not verified."""
    try:
        key = _stat_key(entry)
        if key in _VERIFIED:
            return True
        expected = _digest_path(entry).read_text(encoding="ascii").strip()
        if _sha256(entry) != expected:
            return False
    except (OSError, ValueError):
        return False
    _VERIFIED.add(key)
    return True


def _discard(entry: Path) -> None:
    """Remove an entry and its digest; either may already be gone."""
    entry.unlink(missing_ok=True)
    _digest_path(entry).unlink(missing_ok=True)


def _write(archive: Path, directory: Path, entry: Path, max_bytes: int) -> Path:
    """Decompress `archive` into `directory`, named by the archive bytes actually
    read, with its digest. The digest is renamed into place first, so an entry is
    never visible without one."""
    directory.mkdir(parents=True, exist_ok=True)
    # The pid says whose file it is; the random suffix keeps two threads of one
    # process, which share a pid, from writing the same temp file.
    fd, name = tempfile.mkstemp(dir=directory, prefix=f"{entry.name}.tmp-{os.getpid()}-")
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as out, archive.open("rb") as raw:
            reader = _HashingReader(raw)
            with gzip.GzipFile(fileobj=reader, mode="rb") as stream:
                content = _copy_capped(stream, out, max_bytes)
            # The bytes reach the disk before the name does, so a crash cannot
            # leave a whole-looking entry over a short file.
            out.flush()
            os.fsync(out.fileno())
        entry = directory / f"{reader.sha256.hexdigest()}.sqlite"
        _write_digest(directory, _digest_path(entry), content)
        os.replace(temp, entry)
        _fsync_directory(directory)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    try:
        _VERIFIED.add(_stat_key(entry))
    except OSError:
        # A peer already removed it: opening it fails, and the caller retries.
        pass
    return entry


def _write_digest(directory: Path, digest: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(dir=directory, prefix=f"{digest.name}.tmp-{os.getpid()}-")
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as out:
            out.write(content + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, digest)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _copy_capped(source, out, max_bytes: int) -> str:
    """Copy at most `max_bytes`; the sha256 of what was copied."""
    digest = hashlib.sha256()
    written = 0
    while chunk := source.read(CHUNK):
        written += len(chunk)
        if written > max_bytes:
            raise IndexTooLarge(max_bytes)
        out.write(chunk)
        digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    """Make the rename durable where the platform can open a directory to sync it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _remove_others(directory: Path, keep: Path) -> None:
    """Remove this store id's other entries and their digests, and temp files a
    crashed writer left. A young temp file may be a live peer's write and is kept;
    a file a peer has already removed is not an error."""
    stale_before = time.time() - STALE_TEMP_SECONDS
    kept = {keep.name, _digest_path(keep).name}
    for other in directory.iterdir():
        try:
            if other.name.endswith((".sqlite", ".sqlite.sha256")):
                remove = other.name not in kept
            else:
                remove = (
                    any(marker in other.name for marker in _TEMP_MARKERS)
                    and other.stat().st_mtime < stale_before
                )
            if remove:
                other.unlink()
        except OSError:
            pass


def _connect(
    path: Path, described: str, *, immutable: bool, archive_sha256: str | None = None
) -> IndexHandle | Fallback:
    """`described` names the file in a reason: store-relative, or "the cached
    index", never its path on this host."""
    uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        return Fallback(f"{described} could not be opened: {error}")
    try:
        connection.execute("PRAGMA query_only=1")
        row = connection.execute("SELECT value FROM meta WHERE key = 'index_schema'").fetchone()
    except sqlite3.Error as error:
        connection.close()
        return Fallback(f"{described} could not be read: {error}")
    schema = row[0] if row else None
    if _major(schema) != SCHEMA_MAJOR:
        connection.close()
        return UnrecognisedIndex(unrecognised("index", _schema_found(schema), (SCHEMA_MAJOR,)))
    return IndexHandle(connection, path, archive_sha256)


# A schema a reason may quote: a short version number. Anything else is store text.
_VERSION = re.compile(r"\d{1,6}(?:\.\d{1,6}){0,2}", re.ASCII)


def _schema_found(schema: object) -> str:
    """The schema found, as a reason may say it: quoted only as a version number."""
    if schema is None:
        return "none"
    if isinstance(schema, str) and _VERSION.fullmatch(schema):
        return schema
    return "a schema that is not a version number"


def _major(schema: object) -> int | None:
    if not isinstance(schema, str):
        return None
    major = schema.split(".", 1)[0]
    return int(major) if major.isdigit() else None
