"""The contract with the library: `store_info` checks each single-file layer's
digest against the file the manifest names, in a copy of a store
knowledge-store-builder v0.19.1 wrote, read with the query index and without it.

The library digests a file as the first 16 hex digits of its sha256
(`store_manifest._file_digest`), for library layers and extra layers alike. A
directory layer's digest is taken over the files a git clone receives, which the
server cannot reproduce, so those three layers (`history`, `minimum_standard`,
`relationships`) are named unchecked rather than passed.

Each test works on a temporary copy of the committed store: the committed store is
library output and is never changed in place. The expected layer names are read
from the v0.19.1 manifest by hand: `answers` is the extra layer at
`knowledge/answers.json`, `topics` the library layer at
`knowledge/topics/briefs.json`, and `graph` the one at
`graphify-out/graph.json.gz`, the first of its two paths.
"""

import gzip
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from knowledgestore.server import digests, store, tools_graph
from library_store import harness
from library_store.harness import Contract

UNCHECKED = ["history", "minimum_standard", "relationships"]
FIELDS = ("digest_mismatch", "digest_missing", "digest_unchecked")
# The files one check hashes, in either mode: v0.19.1's 14 present single-file
# layers other than `index` (graph, labels, provenance, repository_manifest,
# context, content_files, intent, ticket_descriptions, ticket_tracker, summaries,
# topics, deep_dives, semantic, explorer) and the 2 extra layers. The index
# archive is not hashed: with the index its digest comes from the open handle,
# and the store without it records no index.
ONE_PASS = 16


class DigestCopy(Contract):
    """Each test copies `STORE` and serves the copy."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        copy = Path(tmp.name).resolve() / "store"
        shutil.copytree(self.STORE, copy, symlinks=True)
        # The harness opens `self.STORE`, so it serves the copy from here on.
        self.STORE = copy
        super().setUp()

    def reopened(self) -> store.Session:
        """A session that opens the copy as it is now, closed at cleanup."""
        cache = self.STORE.parent / "cache"
        opened: list[store.Store] = []

        def factory() -> store.Store:
            loaded = store.open_store(self.STORE, cache)
            opened.append(loaded)
            return loaded

        self.addCleanup(lambda: [loaded.close() for loaded in opened])
        return store.Session(factory, lambda: harness.NOW)

    def checked(self, session: store.Session) -> dict:
        info = tools_graph.store_info(session)
        return {field: info[field] for field in FIELDS}


class DigestContract(DigestCopy):
    """Run over the store with its index and the one without."""

    def test_the_library_store_matches_its_manifest(self):
        """Catches: a digest scheme that differs from the library's (the whole
        sha256, another hash, or a read that decompresses an archive first), an
        extra layer hashed at the wrong path, or a directory layer checked by a
        scheme it cannot reproduce rather than named unchecked."""
        self.assertEqual(
            self.checked(self.session),
            {"digest_mismatch": [], "digest_missing": [], "digest_unchecked": UNCHECKED},
        )

    def test_a_changed_layer_file_is_named_and_no_other(self):
        """Catches: a mismatch not reported, reported for a layer whose file did
        not change, extra layers left unchecked, or a layer with two paths
        (`graph`) hashed at the second rather than the one the library chose."""
        for layer, rel in (
            ("answers", "knowledge/answers.json"),
            ("topics", "knowledge/topics/briefs.json"),
            ("graph", "graphify-out/graph.json.gz"),
        ):
            with self.subTest(layer=layer):
                path = self.STORE / rel
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                try:
                    found = self.checked(self.reopened())
                finally:
                    path.write_bytes(original)
                self.assertEqual(
                    found,
                    {
                        "digest_mismatch": [layer],
                        "digest_missing": [],
                        "digest_unchecked": UNCHECKED,
                    },
                )

    def test_a_deleted_layer_file_is_missing_not_mismatched(self):
        """Catches: a declared file absent from disk reported as a mismatch, left
        out altogether, or raising out of `store_info`."""
        (self.STORE / "knowledge/topics/briefs.json").unlink()
        self.assertEqual(
            self.checked(self.reopened()),
            {"digest_mismatch": [], "digest_missing": ["topics"], "digest_unchecked": UNCHECKED},
        )

    def test_the_fields_name_layers_and_never_a_path(self):
        """Catches: a mismatched or missing layer reported by its path, which
        names the store's layout and, made absolute, the host's."""
        (self.STORE / "knowledge/answers.json").write_bytes(b"{}")
        (self.STORE / "knowledge/topics/briefs.json").unlink()
        found = self.checked(self.reopened())
        self.assertEqual(found["digest_mismatch"], ["answers"])
        self.assertEqual(found["digest_missing"], ["topics"])
        for field in FIELDS:
            for name in found[field]:
                self.assertNotIn("/", name, field)
                self.assertNotIn(str(self.STORE), name, field)

    def test_digests_are_computed_once_per_loaded_store_and_again_on_reload(self):
        """Catches: hashing at open, which slows start-up and puts a reload's
        hashing inside another asker's call; every `store_info` call hashing the
        store again; a reload keeping the answer computed for the store it
        replaced; or the index archive hashed again when the open index handle
        already holds its sha256."""
        with mock.patch.object(digests, "file_digest", wraps=digests.file_digest) as hashed:
            session = self.reopened()
            self.assertEqual(hashed.call_count, 0, "hashed at open")
            tools_graph.store_info(session)
            self.assertEqual(hashed.call_count, ONE_PASS)
            tools_graph.store_info(session)
            self.assertEqual(hashed.call_count, ONE_PASS)
            hashed_paths = [call.args[0].name for call in hashed.call_args_list]
            self.assertNotIn("store.sqlite.gz", hashed_paths)

            # A pull that changes a layer file and the manifest's own digest.
            (self.STORE / "knowledge/answers.json").write_bytes(b"{}")
            manifest = self.STORE / "knowledge/store-manifest.json"
            document = json.loads(manifest.read_text("utf-8"))
            document["digest"] = "0000000000000000"
            manifest.write_text(json.dumps(document), "utf-8")
            self.assertEqual(self.checked(session)["digest_mismatch"], ["answers"])
            self.assertEqual(hashed.call_count, 2 * ONE_PASS)

    def test_a_layer_file_replaced_by_a_directory_is_missing(self):
        """Catches: a directory where the manifest declares a file hashed as a
        directory, named unchecked or mismatched, or raising out of
        `store_info`."""
        path = self.STORE / "knowledge/topics/briefs.json"
        path.unlink()
        path.mkdir()
        (path / "inside.json").write_text("{}", "utf-8")
        self.assertEqual(
            self.checked(self.reopened()),
            {"digest_mismatch": [], "digest_missing": ["topics"], "digest_unchecked": UNCHECKED},
        )


DigestWithIndex, DigestWithoutIndex = harness.both_modes(DigestContract)


class DigestIndexArchive(DigestCopy, harness.LibraryStoreCase):
    """Over the store with its index only: the store without it has no archive,
    so this has no `WithoutIndex` twin."""

    STORE = harness.OUT
    INDEXED = True

    def test_a_changed_index_archive_is_named(self):
        """Catches: the index archive's digest taken from the open handle in a
        form other than the library's (the whole sha256, or the decompressed
        index's), so a changed archive goes unseen or an unchanged one is named."""
        archive = self.STORE / "knowledge/index/store.sqlite.gz"
        # The same index, compressed with another gzip timestamp: a valid archive
        # the server opens, whose bytes differ from the ones the library digested.
        archive.write_bytes(gzip.compress(gzip.decompress(archive.read_bytes()), mtime=1))
        self.assertEqual(
            self.checked(self.reopened()),
            {"digest_mismatch": ["index"], "digest_missing": [], "digest_unchecked": UNCHECKED},
        )


class FileDigestTest(unittest.TestCase):
    def test_a_file_over_one_chunk_is_digested_whole(self):
        """Catches: a digest of only the first 1 MiB chunk, or of the last, so a
        change past the first chunk of a large layer file goes unseen."""
        content = bytes(range(256)) * (3 * 4096) + b"tail!"  # 3 MiB and five bytes
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "layer.bin"
            path.write_bytes(content)
            self.assertEqual(digests.file_digest(path), hashlib.sha256(content).hexdigest()[:16])


if __name__ == "__main__":
    unittest.main()
