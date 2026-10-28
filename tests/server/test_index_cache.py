"""Opening the query index (spec, Start-up step 3), on the synthetic store.

The cache directory is a real temporary directory and the archives are the
fixture's real files: every assertion lands on what is on disk afterwards or on
what a query through the handle returns. Expected repository names are the
literals in fixture_store/DESIGN.md section 1.
"""

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from multiprocessing import get_context
from pathlib import Path
from unittest import mock

from fixture_store.build import GRAPH, INDEX_ARCHIVE, INDEX_PLAIN, MANIFEST, build
from knowledgestore.server import index, manifest, store
from knowledgestore.server.index import Fallback, IndexHandle

# DESIGN.md 1: the index's repositories in `k` order: the twelve. The one node with
# no repository is not listed, as the library's index leaves the empty name out.
REPOS = [
    "accounts-web",
    "audit-service",
    "chart-web",
    "gateway-service",
    "infra-modules",
    "ledger-service",
    "notifications-service",
    "payment-apis",
    "payments-api",
    "reporting-worker",
    "shared-lib-java",
    "ui-components",
]


def repo_names(handle: IndexHandle) -> list[str]:
    return [row[0] for row in handle.connection.execute("SELECT name FROM repos ORDER BY k")]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pair(name: str) -> list[str]:
    """A cache entry's listing: the entry and its digest."""
    return [name, name + ".sha256"]


def snapshot(root: Path) -> dict[str, tuple]:
    """Every file and directory under `root`, with its bytes' hash, mode and mtime."""
    state: dict[str, tuple] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        content = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        state[path.relative_to(root).as_posix()] = (info.st_mode, info.st_mtime_ns, content)
    return state


def open_in_child(root: str, cache: str, barrier, results) -> None:
    """One server's start-up, in its own process (the concurrency test)."""
    root_path = Path(root)
    barrier.wait()
    handle = index.open_index(root_path, manifest.load(root_path), Path(cache))
    if isinstance(handle, IndexHandle):
        results.put(repo_names(handle))
        handle.close()
    else:
        results.put(handle.reason)


class CacheTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.cache = self.tmp / "cache"
        # Nothing here may reach the real cache, even through a default.
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.entries = self.cache / "knowledge-store" / "example-store"

    def open(self, root: Path) -> IndexHandle | Fallback:
        result = index.open_index(root, manifest.load(root), self.cache)
        if isinstance(result, IndexHandle):
            self.addCleanup(result.close)
        return result

    def open_handle(self, root: Path) -> IndexHandle:
        result = self.open(root)
        assert isinstance(result, IndexHandle), result
        return result

    def listing(self, directory: Path) -> list[str]:
        return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


class CacheHomeTest(unittest.TestCase):
    def test_xdg_cache_home_is_used_when_set(self):
        """Catches: ignoring XDG_CACHE_HOME, so the cache lands in ~/.cache regardless."""
        got = index.cache_home({"XDG_CACHE_HOME": "/srv/cache"}, Path("/home/u"))
        self.assertEqual(got, Path("/srv/cache"))

    def test_unset_or_empty_xdg_cache_home_means_home_dot_cache(self):
        """Catches: `${XDG_CACHE_HOME-}` instead of `:-` semantics, so an empty value
        writes the cache relative to the working directory."""
        self.assertEqual(index.cache_home({}, Path("/home/u")), Path("/home/u/.cache"))
        self.assertEqual(
            index.cache_home({"XDG_CACHE_HOME": ""}, Path("/home/u")), Path("/home/u/.cache")
        )

    def test_a_relative_xdg_cache_home_is_ignored(self):
        """Catches: honouring a relative value, which writes the cache under the
        working directory - inside the store when the server is started there."""
        got = index.cache_home({"XDG_CACHE_HOME": "cache"}, Path("/home/u"))
        self.assertEqual(got, Path("/home/u/.cache"))


class ArchiveTest(CacheTestCase):
    def test_the_cache_entry_is_named_by_the_archives_sha256(self):
        """Catches: keying the entry on anything but the archive's full sha256 (the
        decompressed bytes, a truncated digest, the manifest digest), or decompressing
        to the wrong bytes."""
        root = build(self.tmp / "s")
        archive = root / INDEX_ARCHIVE
        handle = self.open_handle(root)
        name = sha256_of(archive) + ".sqlite"
        self.assertEqual(handle.path, self.entries / name)
        self.assertEqual(self.listing(self.entries), pair(name))
        self.assertEqual(handle.path.read_bytes(), gzip.decompress(archive.read_bytes()))
        self.assertEqual(repo_names(handle), REPOS)

    def test_a_second_open_reuses_the_entry_without_rewriting_it(self):
        """Catches: decompressing on every start, which rewrites the entry (a new
        inode, a new mtime) and costs the start-up time the cache exists to save."""
        root = build(self.tmp / "s")
        first = self.open_handle(root)
        first.close()
        os.utime(first.path, ns=(1_000_000_000, 1_000_000_000))
        before = first.path.stat()
        second = self.open_handle(root)
        after = second.path.stat()
        self.assertEqual(second.path, first.path)
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, 1_000_000_000))
        self.assertEqual(repo_names(second), REPOS)

    def test_a_changed_archive_gets_a_new_entry_and_the_old_one_is_removed(self):
        """Catches: serving a stale entry after the archive changes, or leaving every
        old entry behind so the cache grows by an index per rebuild."""
        root = build(self.tmp / "s")
        archive = root / INDEX_ARCHIVE
        old = self.open_handle(root).path.name
        # The same database, re-compressed with another header time: new archive bytes.
        archive.write_bytes(gzip.compress(gzip.decompress(archive.read_bytes()), mtime=1))
        new = sha256_of(archive) + ".sqlite"
        self.assertNotEqual(new, old)
        handle = self.open_handle(root)
        self.assertEqual(handle.path.name, new)
        self.assertEqual(self.listing(self.entries), pair(new))
        self.assertEqual(repo_names(handle), REPOS)

    def test_an_archive_replaced_after_hashing_is_named_by_what_was_decompressed(self):
        """Catches: naming the entry by the first hash when a pull replaced the
        archive between hashing it and decompressing it, which files the new index
        under the old archive's name and serves it if that archive returns."""
        root = build(self.tmp / "s")
        archive = root / INDEX_ARCHIVE
        database = gzip.decompress(archive.read_bytes())
        hash_file = index._sha256

        def hash_then_pull(path: Path) -> str:
            digest = hash_file(path)
            archive.write_bytes(gzip.compress(database, mtime=1))  # the pull lands
            return digest

        with mock.patch.object(index, "_sha256", hash_then_pull):
            handle = self.open_handle(root)
        name = sha256_of(archive) + ".sqlite"
        self.assertEqual(handle.path, self.entries / name)
        self.assertEqual(self.listing(self.entries), pair(name))

    def test_other_store_ids_and_in_flight_temp_files_survive_the_cleanup(self):
        """Catches: a cleanup that removes more than this store id's other entries -
        another store's cache, an unrelated file, or a peer server's temp file that
        is still being written."""
        other = self.cache / "knowledge-store" / "other-store"
        other.mkdir(parents=True)
        (other / "0123.sqlite").write_bytes(b"other")
        self.entries.mkdir(parents=True)
        (self.entries / "stale.sqlite").write_bytes(b"stale")
        (self.entries / "abc.sqlite.tmp-1-xyz").write_bytes(b"peer")
        (self.entries / "README").write_bytes(b"note")
        # Old enough for the temp sweep, but not a temp file: it must stay.
        then = time.time() - 48 * 3600
        os.utime(self.entries / "README", (then, then))
        root = build(self.tmp / "s")
        name = self.open_handle(root).path.name
        self.assertEqual(self.listing(other), ["0123.sqlite"])
        self.assertEqual((other / "0123.sqlite").read_bytes(), b"other")
        self.assertEqual(
            self.listing(self.entries), sorted(["README", "abc.sqlite.tmp-1-xyz", *pair(name)])
        )

    def test_an_emptied_entry_is_replaced_on_the_next_open(self):
        """Catches: a cache entry left bad by a crash (renamed before its bytes were
        on disk) being reused for ever - every later start falls back, or fails."""
        root = build(self.tmp / "s")
        first = self.open_handle(root)
        first.close()
        first.path.write_bytes(b"")
        handle = self.open_handle(root)
        self.assertEqual(handle.path, first.path)
        self.assertEqual(repo_names(handle), REPOS)
        expected = gzip.decompress((root / INDEX_ARCHIVE).read_bytes())
        self.assertEqual(handle.path.read_bytes(), expected)

    def test_the_digest_is_the_sha256_of_the_decompressed_bytes(self):
        """Catches: a digest of the archive, or of nothing, written beside the entry,
        which every later start would find mismatched and decompress again."""
        root = build(self.tmp / "s")
        handle = self.open_handle(root)
        expected = hashlib.sha256(gzip.decompress((root / INDEX_ARCHIVE).read_bytes()))
        digest = handle.path.with_name(handle.path.name + ".sha256")
        self.assertEqual(digest.read_text(encoding="ascii"), expected.hexdigest() + "\n")

    def test_an_entry_with_one_byte_flipped_is_rebuilt_on_the_next_start(self):
        """Catches: serving a damaged entry because its name still matches the
        archive. Damage SQLite would not notice reads as wrong answers, so the
        bytes are checked against the digest on the first open in a process."""
        root = build(self.tmp / "s")
        first = self.open_handle(root)
        first.close()
        damaged = bytearray(first.path.read_bytes())
        damaged[len(damaged) // 2] ^= 0xFF
        first.path.write_bytes(bytes(damaged))
        # The next start: a new process has checked nothing.
        with mock.patch.object(index, "_VERIFIED", set()):
            handle = self.open_handle(root)
        self.assertEqual(handle.path, first.path)
        expected = gzip.decompress((root / INDEX_ARCHIVE).read_bytes())
        self.assertEqual(handle.path.read_bytes(), expected)
        self.assertEqual(repo_names(handle), REPOS)
        self.assertEqual(self.listing(self.entries), pair(first.path.name))

    def test_an_entry_without_its_digest_is_rebuilt(self):
        """Catches: trusting an entry nothing can check - one written before digests
        existed, or whose digest was lost - instead of decompressing it again."""
        root = build(self.tmp / "s")
        first = self.open_handle(root)
        first.close()
        # An mtime no write now can have; an inode number may be reused, so it
        # cannot tell a rewritten entry from the old one.
        os.utime(first.path, ns=(1_000_000_000, 1_000_000_000))
        first.path.with_name(first.path.name + ".sha256").unlink()
        with mock.patch.object(index, "_VERIFIED", set()):
            handle = self.open_handle(root)
        self.assertNotEqual(handle.path.stat().st_mtime_ns, 1_000_000_000)
        self.assertEqual(self.listing(self.entries), pair(first.path.name))
        self.assertEqual(repo_names(handle), REPOS)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file modes")
    def test_a_damaged_entry_that_cannot_be_replaced_falls_back_naming_no_path(self):
        """Catches: a damaged entry served because it could not be removed, or the
        fallback's reason carrying the cache path an `OSError` names."""
        root = build(self.tmp / "s")
        first = self.open_handle(root)
        first.close()
        first.path.write_bytes(b"damaged")
        self.entries.chmod(0o500)
        self.addCleanup(self.entries.chmod, 0o700)
        with mock.patch.object(index, "_VERIFIED", set()):
            result = self.open(root)
        assert isinstance(result, Fallback), result
        self.assertIn("could not be written", result.reason)
        self.assertNotIn(str(self.tmp), result.reason)
        self.assertNotIn(first.path.name, result.reason)

    def test_the_cleanup_removes_and_keeps_digests_with_their_entries(self):
        """Catches: another archive's digest, or a digest whose entry is gone, left
        behind for ever; a crashed writer's old digest temp file never swept; or
        the current entry's digest removed, so every start decompresses again."""
        self.entries.mkdir(parents=True)
        (self.entries / "stale.sqlite").write_bytes(b"stale")
        (self.entries / "stale.sqlite.sha256").write_bytes(b"0" * 64)
        (self.entries / "orphan.sqlite.sha256").write_bytes(b"0" * 64)
        old = self.entries / "aaa.sqlite.sha256.tmp-1-old"
        young = self.entries / "bbb.sqlite.sha256.tmp-2-young"
        for path, hours in ((old, 25), (young, 23)):
            path.write_bytes(b"partial")
            then = time.time() - hours * 3600
            os.utime(path, (then, then))
        root = build(self.tmp / "s")
        name = self.open_handle(root).path.name
        self.assertEqual(self.listing(self.entries), sorted([*pair(name), young.name]))

    def test_an_entry_a_peer_deletes_before_it_opens_is_decompressed_again(self):
        """Catches: falling back when a server for the same store id, holding another
        archive, removes this entry between its rename and its open."""
        root = build(self.tmp / "a")
        peer = build(self.tmp / "b")
        peer_archive = peer / INDEX_ARCHIVE
        peer_archive.write_bytes(gzip.compress(gzip.decompress(peer_archive.read_bytes()), mtime=1))
        real = index._cached
        fired: list[bool] = []

        def cached_then_peer_opens(archive: Path, directory: Path, max_bytes: int) -> Path:
            entry = real(archive, directory, max_bytes)
            if not fired:
                fired.append(True)
                # The peer's open removes this store id's other entries: ours.
                result = index.open_index(peer, manifest.load(peer), self.cache)
                assert isinstance(result, IndexHandle), result
                result.close()
                assert not entry.exists()
            return entry

        with mock.patch.object(index, "_cached", cached_then_peer_opens):
            handle = self.open_handle(root)
        self.assertEqual(fired, [True])
        self.assertEqual(handle.path.name, sha256_of(root / INDEX_ARCHIVE) + ".sqlite")
        self.assertEqual(repo_names(handle), REPOS)

    def test_two_archives_under_one_store_id_both_keep_serving(self):
        """Catches: an open handle failing once a peer with another archive removes
        its entry - the handle must hold the file it opened."""
        first_root = build(self.tmp / "a")
        second_root = build(self.tmp / "b")
        archive = second_root / INDEX_ARCHIVE
        archive.write_bytes(gzip.compress(gzip.decompress(archive.read_bytes()), mtime=1))
        first = self.open_handle(first_root)
        second = self.open_handle(second_root)
        self.assertNotEqual(first.path, second.path)
        self.assertFalse(first.path.exists())
        self.assertEqual(repo_names(first), REPOS)
        self.assertEqual(repo_names(second), REPOS)

    def test_decompression_stops_at_the_cap_and_falls_back(self):
        """Catches: an archive that inflates without bound filling the disk, or a
        cap that is off by one. At exactly its size the index is written; one byte
        under, nothing is left and the reason says why."""
        root = build(self.tmp / "s")
        size = len(gzip.decompress((root / INDEX_ARCHIVE).read_bytes()))
        result = index.open_index(root, manifest.load(root), self.cache, max_bytes=size - 1)
        self.assertIsInstance(result, Fallback)
        assert isinstance(result, Fallback)
        self.assertIn(f"decompresses to more than {size - 1} bytes", result.reason)
        self.assertEqual(self.listing(self.entries), [])
        handle = index.open_index(root, manifest.load(root), self.cache, max_bytes=size)
        assert isinstance(handle, IndexHandle), handle
        self.addCleanup(handle.close)
        self.assertEqual(repo_names(handle), REPOS)

    def test_open_store_passes_its_cap_to_the_index(self):
        """Catches: a configured cap that never reaches the decompress, so the server
        option changes nothing."""
        loaded = store.open_store(build(self.tmp / "s"), self.cache, max_index_bytes=1024)
        self.addCleanup(loaded.close)
        assert isinstance(loaded.index, Fallback), loaded.index
        self.assertIn("decompresses to more than 1024 bytes", loaded.index.reason)

    def test_the_default_cap_is_8_gib(self):
        """Catches: a default cap small enough to refuse a real store's index."""
        self.assertEqual(index.DEFAULT_MAX_INDEX_BYTES, 8 * 1024**3)

    def test_temp_files_older_than_a_day_are_removed_and_younger_ones_kept(self):
        """Catches: crashed writers' temp files accumulating for ever, or a sweep that
        deletes a temp file a live peer may still be writing (younger than 24 hours)."""
        self.entries.mkdir(parents=True)
        old = self.entries / "aaa.sqlite.tmp-1-old"
        young = self.entries / "bbb.sqlite.tmp-2-young"
        for path, hours in ((old, 25), (young, 23)):
            path.write_bytes(b"partial")
            then = time.time() - hours * 3600
            os.utime(path, (then, then))
        root = build(self.tmp / "s")
        name = self.open_handle(root).path.name
        self.assertEqual(self.listing(self.entries), sorted([*pair(name), young.name]))
        # The sweep runs when an existing entry is reused, too.
        then = time.time() - 25 * 3600
        os.utime(young, (then, then))
        self.open_handle(root)
        self.assertEqual(self.listing(self.entries), pair(name))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file modes")
    def test_a_read_only_cache_directory_falls_back_and_leaves_nothing(self):
        """Catches: an OSError writing the cache escaping as a crash, instead of the
        spec's fallback to the graph."""
        self.entries.mkdir(parents=True)
        self.entries.chmod(0o500)
        self.addCleanup(self.entries.chmod, 0o700)
        root = build(self.tmp / "s")
        result = self.open(root)
        self.assertIsInstance(result, Fallback)
        assert isinstance(result, Fallback)
        self.assertIn("could not be written", result.reason)
        self.assertEqual(self.listing(self.entries), [])

    def test_a_write_that_fails_part_way_removes_its_temp_file(self):
        """Catches: a failed decompress leaving its partial `.tmp-` file behind, or
        renaming it into place as an entry. The archive is cut mid-stream, so the
        temp file exists and holds bytes when the error arrives."""
        root = build(self.tmp / "s")
        archive = root / INDEX_ARCHIVE
        whole = archive.read_bytes()
        archive.write_bytes(whole[: len(whole) // 2])
        result = self.open(root)
        self.assertIsInstance(result, Fallback)
        self.assertEqual(self.listing(self.entries), [])

    def test_two_processes_decompressing_at_once_leave_one_whole_entry(self):
        """Catches: a fixed temp name that two servers share, so one renames the
        other's half-written file into place, or a cleanup that deletes a peer's
        entry before it is renamed."""
        root = build(self.tmp / "s")
        expected = gzip.decompress((root / INDEX_ARCHIVE).read_bytes())
        context = get_context("spawn")
        for _ in range(3):
            shutil.rmtree(self.cache, ignore_errors=True)
            barrier = context.Barrier(2)
            results = context.Queue()
            children = [
                context.Process(
                    target=open_in_child, args=(str(root), str(self.cache), barrier, results)
                )
                for _ in range(2)
            ]
            for child in children:
                child.start()
            got = [results.get(timeout=60) for _ in children]
            for child in children:
                child.join(timeout=60)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(got, [REPOS, REPOS])
            name = sha256_of(root / INDEX_ARCHIVE) + ".sqlite"
            self.assertEqual(self.listing(self.entries), pair(name))
            self.assertEqual((self.entries / name).read_bytes(), expected)

    def test_two_threads_decompressing_at_once_leave_one_whole_entry(self):
        """Catches: a temp name keyed on the pid alone, which two threads of one
        process share."""
        root = build(self.tmp / "s")
        loaded = manifest.load(root)
        expected = gzip.decompress((root / INDEX_ARCHIVE).read_bytes())
        for _ in range(5):
            shutil.rmtree(self.cache, ignore_errors=True)
            barrier = threading.Barrier(4)
            got: list[object] = []

            def run():
                barrier.wait()
                result = index.open_index(root, loaded, self.cache)
                got.append(repo_names(result) if isinstance(result, IndexHandle) else result)
                if isinstance(result, IndexHandle):
                    result.close()

            threads = [threading.Thread(target=run) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            self.assertEqual(got, [REPOS] * 4)
            name = sha256_of(root / INDEX_ARCHIVE) + ".sqlite"
            self.assertEqual(self.listing(self.entries), pair(name))
            self.assertEqual((self.entries / name).read_bytes(), expected)


class PlacementTest(CacheTestCase):
    def test_a_plain_index_without_an_archive_is_opened_in_place(self):
        """Catches: copying or decompressing a plain `store.sqlite` that is the only
        index, instead of opening it where it is."""
        root = build(self.tmp / "s", index="plain")
        handle = self.open_handle(root)
        self.assertEqual(handle.path, root / INDEX_PLAIN)
        self.assertEqual(repo_names(handle), REPOS)
        self.assertFalse(self.cache.exists())

    def test_a_stale_plain_index_beside_the_archive_is_ignored(self):
        """Catches: preferring the local, gitignored `store.sqlite` over the committed
        archive, so a pull that brings a newer archive keeps serving the old build.
        The plain file here renames a repository, so reading it shows."""
        root = build(self.tmp / "s")
        plain = root / INDEX_PLAIN
        plain.write_bytes(gzip.decompress((root / INDEX_ARCHIVE).read_bytes()))
        with sqlite3.connect(plain) as stale:
            stale.execute("UPDATE repos SET name = 'renamed-since' WHERE name = 'chart-web'")
        stale.close()
        handle = self.open_handle(root)
        self.assertEqual(handle.path.parent, self.entries)
        self.assertEqual(repo_names(handle), REPOS)

    def test_no_index_falls_back_and_says_so(self):
        """Catches: treating a missing index as an error, or falling back silently."""
        root = build(self.tmp / "s", index="none")
        result = self.open(root)
        self.assertIsInstance(result, Fallback)
        assert isinstance(result, Fallback)
        self.assertIn("no index in the store", result.reason)
        self.assertFalse(self.cache.exists())

    def test_an_unsafe_store_id_is_not_joined_into_the_cache_path(self):
        """Catches: joining `store.id` into a path unchecked, so `../x` writes the
        cache outside its directory."""
        root = build(self.tmp / "s")
        path = root / MANIFEST
        document = json.loads(path.read_text(encoding="utf-8"))
        document["store"]["id"] = "../escaped"
        path.write_text(json.dumps(document), encoding="utf-8")
        result = self.open(root)
        self.assertIsInstance(result, Fallback)
        self.assertFalse(self.cache.exists())
        self.assertFalse((self.tmp / "escaped").exists())

    def test_a_cache_home_inside_the_store_is_refused(self):
        """Catches: writing the cache into the store when the cache home points there,
        which would break the server's only promise about writes."""
        root = build(self.tmp / "s")
        before = snapshot(root)
        result = index.open_index(root, manifest.load(root), root / ".cache")
        self.assertIsInstance(result, Fallback)
        self.assertEqual(snapshot(root), before)

    def test_an_index_symlinked_outside_the_store_is_not_opened(self):
        """Catches: following a symlink in the store to an index outside it."""
        root = build(self.tmp / "s", index="plain")
        plain = root / INDEX_PLAIN
        outside = self.tmp / "outside.sqlite"
        plain.rename(outside)
        plain.symlink_to(outside)
        result = self.open(root)
        self.assertIsInstance(result, Fallback)
        assert isinstance(result, Fallback)
        self.assertIn("outside the store", result.reason)


class SchemaTest(CacheTestCase):
    def set_schema(self, root: Path, value: str | None) -> None:
        with sqlite3.connect(root / INDEX_PLAIN) as db:
            db.execute("DELETE FROM meta WHERE key = 'index_schema'")
            if value is not None:
                db.execute("INSERT INTO meta(key, value) VALUES ('index_schema', ?)", (value,))
        db.close()

    def test_an_unknown_schema_major_falls_back_and_names_it(self):
        """Catches: querying an index whose layout this server does not know, or
        falling back without saying which schema was found and which is read, in
        the terms every unrecognised layer is stated in (spec, Absence semantics);
        or quoting a schema that is not a version number, which is store text."""
        for value, shown in (
            ("2.0", "2.0"),
            ("one", "a schema that is not a version number"),
            (None, "none"),
        ):
            with self.subTest(index_schema=value):
                root = build(self.tmp / f"s-{value}", index="plain")
                self.set_schema(root, value)
                result = self.open(root)
                self.assertIsInstance(result, index.UnrecognisedIndex)
                assert isinstance(result, Fallback)
                self.assertEqual(
                    result.reason,
                    "index layer present but format unrecognised "
                    f"(found {shown}, supports 1.x); not read",
                )

    def test_an_unknown_schema_archive_is_decompressed_once_and_its_entry_kept(self):
        """Catches: discarding the entry and decompressing again when only the schema
        check failed. The schema is in the archive, so a second decompress reads the
        same schema: the retry costs a full decompress and never succeeds."""
        plain = build(self.tmp / "p", index="plain")
        self.set_schema(plain, "2.0")
        root = build(self.tmp / "s")
        (root / INDEX_ARCHIVE).write_bytes(gzip.compress((plain / INDEX_PLAIN).read_bytes()))
        real = index._copy_capped
        decompressed: list[int] = []

        def copy_and_count(source, out, max_bytes: int) -> str:
            content = real(source, out, max_bytes)
            decompressed.append(out.tell())
            return content

        with mock.patch.object(index, "_copy_capped", copy_and_count):
            first = self.open(root)
            again = self.open(root)
        for result in (first, again):
            self.assertIsInstance(result, Fallback)
            assert isinstance(result, Fallback)
            self.assertIn("(found 2.0, supports 1.x)", result.reason)
        # One decompress over two opens: the entry is kept and reused, unchanged.
        self.assertEqual(len(decompressed), 1)
        self.assertEqual(
            self.listing(self.entries), pair(sha256_of(root / INDEX_ARCHIVE) + ".sqlite")
        )

    def test_a_newer_schema_minor_is_read(self):
        """Catches: refusing an index only because a newer library added to it."""
        root = build(self.tmp / "s", index="plain")
        self.set_schema(root, "1.7")
        self.assertEqual(repo_names(self.open_handle(root)), REPOS)


class ReadOnlyTest(CacheTestCase):
    def assert_read_only(self, handle: IndexHandle) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            handle.connection.execute("CREATE TEMP TABLE scratch(a)")
        # With query_only lifted, the file itself must still refuse the write.
        handle.connection.execute("PRAGMA query_only=0")
        with self.assertRaises(sqlite3.OperationalError):
            handle.connection.execute("INSERT INTO meta(key, value) VALUES ('k', 'v')")

    def test_a_cache_entry_refuses_writes(self):
        """Catches: dropping `PRAGMA query_only` (the temp-table write succeeds) or the
        read-only URI (the insert succeeds once query_only is lifted)."""
        self.assert_read_only(self.open_handle(build(self.tmp / "s")))

    def test_a_plain_index_in_the_store_refuses_writes(self):
        """Catches: opening the store's own file writable, so a stray statement
        could change the store."""
        self.assert_read_only(self.open_handle(build(self.tmp / "s", index="plain")))


class StoreTest(CacheTestCase):
    def test_the_index_path_never_parses_the_graph(self):
        """Catches: start-up or a query reading the whole graph although the index is
        present. The graph is corrupt, so any parse of it fails the test."""
        root = build(self.tmp / "s")
        (root / GRAPH).write_bytes(b"not a gzip archive")
        loaded = store.open_store(root, self.cache)
        self.addCleanup(loaded.close)
        assert isinstance(loaded.index, IndexHandle)
        self.assertEqual(repo_names(loaded.index), REPOS)
        self.assertEqual(loaded.layer("provenance")["repositories"]["chart-web"]["sha"], "3" * 40)
        # The graph really is unreadable: reading it is what would have failed.
        with self.assertRaises(OSError):
            loaded.layer("graph")

    def test_without_an_index_the_graph_layer_is_read(self):
        """Catches: a fallback store that cannot reach the graph it falls back to."""
        root = build(self.tmp / "s", index="none")
        loaded = store.open_store(root, self.cache)
        self.addCleanup(loaded.close)
        self.assertIsInstance(loaded.index, Fallback)
        expected = json.loads(gzip.decompress((root / GRAPH).read_bytes()))
        self.assertEqual(loaded.layer("graph"), expected)
        first = loaded.layer("graph")
        self.assertIs(loaded.layer("graph"), first)

    def test_an_absent_layer_reads_as_none(self):
        """Catches: an absent layer raising, or reading a path the manifest says is
        not there (DESIGN.md 9.4: `context` is absent)."""
        loaded = store.open_store(build(self.tmp / "s"), self.cache)
        self.addCleanup(loaded.close)
        self.assertIsNone(loaded.layer("context"))
        self.assertIsNone(loaded.layer("no-such-layer"))

    def test_a_directory_layer_is_refused_by_layer(self):
        """Catches: `layer` treating a directory as a file - reading nothing and
        returning None for a layer the manifest says is present."""
        loaded = store.open_store(build(self.tmp / "s"), self.cache)
        self.addCleanup(loaded.close)
        with self.assertRaisesRegex(ValueError, "relationships is a directory"):
            loaded.layer("relationships")

    def test_the_store_tree_is_byte_identical_after_start_up_and_queries(self):
        """Catches: any write into the store - a decompressed index, a journal or
        `-shm` beside a plain index, a touched file - in each of the index modes."""
        clock = lambda: datetime(2026, 10, 20, tzinfo=timezone.utc)  # noqa: E731
        for mode in ("gz", "plain", "none"):
            with self.subTest(index=mode):
                root = build(self.tmp / mode, index=mode)
                before = snapshot(root)
                session = store.Session(lambda r=root: store.open_store(r, self.cache), clock)
                loaded, _ = session.current()
                self.addCleanup(loaded.close)
                if isinstance(loaded.index, IndexHandle):
                    self.assertEqual(repo_names(loaded.index), REPOS)
                loaded.layer("graph")
                loaded.layer("provenance")
                session.current()
                self.assertEqual(snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
