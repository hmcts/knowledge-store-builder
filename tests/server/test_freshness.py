"""Freshness (spec, Freshness paragraph): the manifest `digest` decides a reload.

Each test builds the synthetic store, starts a session on it, then changes the
store on disk the way a rebuild or a pull would.
"""

import gzip
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fixture_store.build import INDEX_ARCHIVE, INDEX_PLAIN, MANIFEST, build
from knowledgestore.server import store
from knowledgestore.server.index import IndexHandle
from knowledgestore.server.store import Notice

# DESIGN.md 9.2: at this instant the store is 4 whole days old.
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


class FreshnessTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.cache = self.tmp / "cache"
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.tmp / "s"


def new_session(case: FreshnessTestCase) -> store.Session:
    session = store.Session(lambda: store.open_store(case.root, case.cache), lambda: NOW)
    case.addCleanup(lambda: session.current()[0].close())
    return session


def digest_on_disk(case: FreshnessTestCase) -> str:
    return json.loads((case.root / MANIFEST).read_text(encoding="utf-8"))["digest"]


class ReloadTest(FreshnessTestCase):
    def test_a_changed_digest_reloads_and_says_so(self):
        """Catches: serving the store loaded at start-up after a rebuild changed it,
        or reloading without saying so in the result."""
        build(self.root)
        session = new_session(self)
        first, notice = session.current()
        self.assertIsNone(notice)
        old_digest = first.manifest.digest
        old_connection = first.index.connection if isinstance(first.index, IndexHandle) else None

        # A rebuild that changes the index layer: the archive goes, a plain file comes.
        (self.root / INDEX_ARCHIVE).unlink()
        build(self.root, index="plain")
        self.assertNotEqual(digest_on_disk(self), old_digest)

        second, notice = session.current()
        self.assertEqual(notice, Notice("reloaded: store digest changed"))
        self.assertIsNot(second, first)
        self.assertEqual(second.manifest.digest, digest_on_disk(self))
        assert isinstance(second.index, IndexHandle)
        self.assertEqual(second.index.path, self.root / INDEX_PLAIN)
        # The replaced store's connection is closed, not leaked.
        assert old_connection is not None
        with self.assertRaises(sqlite3.ProgrammingError):
            old_connection.execute("SELECT 1")

        third, notice = session.current()
        self.assertIs(third, second)
        self.assertIsNone(notice)

    def test_a_truncated_manifest_defers_the_reload_and_says_so(self):
        """Catches: failing the call (or dropping the store) when a pull is half
        written, instead of answering from the store already loaded."""
        build(self.root)
        session = new_session(self)
        first, _ = session.current()
        path = self.root / MANIFEST
        whole = path.read_bytes()
        path.write_bytes(whole[: len(whole) // 2])

        for _ in range(2):
            got, notice = session.current()
            self.assertIs(got, first)
            self.assertEqual(notice, Notice("reload deferred: manifest unreadable"))
        # The loaded store still answers.
        assert isinstance(first.index, IndexHandle)
        # DESIGN.md 1: the twelve repositories; the index does not list the empty name.
        self.assertEqual(
            first.index.connection.execute("SELECT count(*) FROM repos").fetchone()[0], 12
        )

    def test_an_identical_rebuild_does_not_reload(self):
        """Catches: reloading on file times rather than the digest, so a clone or an
        identical rebuild reloads every store on every call."""
        build(self.root)
        session = new_session(self)
        first, _ = session.current()
        manifest_path = self.root / MANIFEST
        os.utime(manifest_path, ns=(1_000_000_000, 1_000_000_000))
        build(self.root)
        self.assertNotEqual(manifest_path.stat().st_mtime_ns, 1_000_000_000)
        got, notice = session.current()
        self.assertIs(got, first)
        self.assertIsNone(notice)


class NoDigestTest(FreshnessTestCase):
    def strip_digest(self) -> Path:
        path = self.root / MANIFEST
        document = json.loads(path.read_text(encoding="utf-8"))
        del document["digest"]
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_without_a_digest_a_changed_manifest_still_reloads(self):
        """Catches: two empty digests comparing equal, so a store whose manifest
        carries none is never reloaded, whatever changes."""
        build(self.root)
        path = self.strip_digest()
        session = new_session(self)
        first, _ = session.current()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["sources_as_of"] = "2026-10-19T00:00:00+00:00"
        path.write_text(json.dumps(document), encoding="utf-8")
        second, notice = session.current()
        self.assertEqual(notice, Notice("reloaded: store digest changed"))
        self.assertEqual(second.manifest.sources_as_of, "2026-10-19T00:00:00+00:00")

    def test_without_a_digest_an_untouched_manifest_does_not_reload(self):
        """Catches: reloading on every call when there is no digest to compare."""
        build(self.root)
        path = self.strip_digest()
        session = new_session(self)
        first, _ = session.current()
        path.write_bytes(path.read_bytes())  # rewritten, same bytes, new mtime
        got, notice = session.current()
        self.assertIs(got, first)
        self.assertIsNone(notice)


class ArchiveBeforeManifestTest(FreshnessTestCase):
    def test_an_archive_replaced_under_an_unchanged_manifest_reloads(self):
        """Catches: trusting the digest alone when a pull wrote the manifest before
        the archive - the server would keep the old index for good. The new index
        renames a repository, so serving the old one shows."""
        build(self.root)
        session = new_session(self)
        first, _ = session.current()
        archive = self.root / INDEX_ARCHIVE
        database = self.tmp / "new.sqlite"
        database.write_bytes(gzip.decompress(archive.read_bytes()))
        with sqlite3.connect(database) as db:
            db.execute("UPDATE repos SET name = 'renamed-since' WHERE name = 'chart-web'")
        db.close()
        archive.write_bytes(gzip.compress(database.read_bytes()))
        second, notice = session.current()
        self.assertEqual(notice, Notice("reloaded: index archive changed"))
        assert isinstance(second.index, IndexHandle)
        names = [r[0] for r in second.index.connection.execute("SELECT name FROM repos")]
        self.assertIn("renamed-since", names)
        third, notice = session.current()
        self.assertIs(third, second)
        self.assertIsNone(notice)

    def test_an_archive_touched_but_unchanged_does_not_reload(self):
        """Catches: reloading on the archive's mtime alone, so a checkout that only
        touches the file throws the loaded index away."""
        build(self.root)
        session = new_session(self)
        first, _ = session.current()
        archive = self.root / INDEX_ARCHIVE
        os.utime(archive, ns=(1_000_000_000, 1_000_000_000))
        for _ in range(2):
            got, notice = session.current()
            self.assertIs(got, first)
            self.assertIsNone(notice)


class StaleDaysTest(FreshnessTestCase):
    def test_a_clock_without_an_offset_gives_none(self):
        """Catches: subtracting a naive clock reading from an aware instant, which
        raises TypeError and fails the call."""
        build(self.root)
        naive = datetime(2026, 10, 20, 12, 0)
        session = store.Session(lambda: store.open_store(self.root, self.cache), lambda: naive)
        self.addCleanup(lambda: session.current()[0].close())
        self.assertIsNone(session.stale_days())

    def test_stale_days_is_whole_days_since_sources_as_of(self):
        """Catches: comparing `sources_as_of` as text or dropping its offset (DESIGN.md
        9.2: 2026-10-16 01:30 UTC to 2026-10-20 12:00 UTC is 4 whole days), or
        rounding up a part day."""
        build(self.root)
        self.assertEqual(new_session(self).stale_days(), 4)
        # 2026-10-20 00:30 UTC is 3 days 23 hours after the instant; read without
        # its -02:00 offset, the store would look 4 days 1 hour old.
        at = datetime(2026, 10, 20, 0, 30, tzinfo=timezone.utc)
        session = store.Session(lambda: store.open_store(self.root, self.cache), lambda: at)
        self.addCleanup(lambda: session.current()[0].close())
        self.assertEqual(session.stale_days(), 3)

    def test_stale_days_is_none_without_a_placeable_sources_as_of(self):
        """Catches: inventing an age for a store that records none, or failing the
        call on a value that is not an instant with an offset."""
        for value in (None, "not a date", "2026-10-16T01:30:00"):
            with self.subTest(sources_as_of=value):
                build(self.root)
                path = self.root / MANIFEST
                document = json.loads(path.read_text(encoding="utf-8"))
                document["sources_as_of"] = value
                path.write_text(json.dumps(document), encoding="utf-8")
                self.assertIsNone(new_session(self).stale_days())


if __name__ == "__main__":
    unittest.main()
