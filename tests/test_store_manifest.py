"""The store manifest says what a store holds, measured from the files on disk.

Every test builds a real store layout in a temporary directory - real files at
the paths the stages write, a real `knowledge/telemetry.json` in the shape
`telemetry.record` writes, and a real git repository for the identity - and
asserts on the manifest `compute` returns. Expected values are written out by
hand. Each test names the break it catches.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated
from knowledgestore import config, store_manifest
from knowledgestore.git_env import git_env

ALL_LAYERS = (
    "content_files",
    "context",
    "deep_dives",
    "deployments",
    "explorer",
    "graph",
    "history",
    "index",
    "intent",
    "labels",
    "provenance",
    "relationships",
    "repository_manifest",
    "semantic",
    "summaries",
    "ticket_descriptions",
    "ticket_tracker",
    "topics",
)


def _touch(root: Path, rel: str, text: str = "{}") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _telemetry(root: Path, measurements) -> None:
    _touch(root, "knowledge/telemetry.json", json.dumps({"measurements": measurements}))


def _sha(data: bytes) -> str:
    """The digest form the manifest uses, derived here without the code under test."""
    return hashlib.sha256(data).hexdigest()[:16]


def _git_repo(root: Path, origin: str | None) -> None:
    """A real git repository at `root`, with `origin` set when one is given."""
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=git_env())
    if origin is not None:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", origin],
            check=True,
            env=git_env(),
        )


def _present(manifest: dict) -> dict:
    return {name: layer["present"] for name, layer in manifest["layers"].items()}


class _Store(SettingsIsolated):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def compute(self) -> dict:
        config.configure(STORE_ID="example-store")
        return store_manifest.compute(self.root)


class AnEmptyStore(_Store):
    def test_every_layer_is_absent_with_no_digest(self):
        """Breaks if a layer is reported present without its file, or an absent
        layer carries a digest: a fresh `discover` has built nothing, and the
        manifest must say so."""
        manifest = self.compute()
        self.assertEqual(sorted(manifest["layers"]), list(ALL_LAYERS))
        expected: dict[str, bool | None] = {name: False for name in ALL_LAYERS}
        expected["deployments"] = None  # nothing measured, so unknown rather than absent
        self.assertEqual(_present(self.compute()), expected)
        digests = {name: layer["digest"] for name, layer in manifest["layers"].items()}
        self.assertEqual(digests, {name: None for name in ALL_LAYERS})
        self.assertIsNone(manifest["sources_as_of"])
        self.assertEqual(manifest["extra_layers"], {})
        self.assertNotIn("built", manifest)

    def test_the_top_level_carries_the_format_identity_and_library_version(self):
        """Breaks if the format version, the store identity or the library version
        goes missing or changes shape - the fields a reader checks first."""
        from knowledgestore import __version__

        config.configure(GITHUB_ORG="example-org")
        manifest = self.compute()
        self.assertEqual(manifest["format"], {"major": 1, "minor": 0})
        self.assertEqual(manifest["store"], {"id": "example-store", "github_org": "example-org"})
        self.assertEqual(manifest["library_version"], __version__)


class WhichLayersArePresent(_Store):
    def test_a_subset_of_layers_reports_exactly_that_subset(self):
        """Breaks if a layer reads another layer's path, a directory layer is
        tested as a file, or a present flag is inverted."""
        _touch(self.root, "graphify-out/graph.json.gz", "")
        _touch(self.root, "knowledge/provenance.json")
        _touch(self.root, "knowledge/git-history/example-repo.ndjson", "")
        _touch(self.root, "knowledge/topics/briefs.json")
        _touch(self.root, "knowledge/intent/file-tickets.json.gz", "")
        expected: dict[str, bool | None] = {name: False for name in ALL_LAYERS}
        expected.update(graph=True, provenance=True, history=True, topics=True, intent=True)
        # Deployments live inside the graph; with no telemetry nothing says whether
        # the stage put any there, so the answer is unknown, not absent.
        expected["deployments"] = None
        self.assertEqual(_present(self.compute()), expected)

    def test_every_layer_reads_its_own_path(self):
        """Breaks if any one layer's path is wrong: each file is created alone and
        only its own layer may turn present."""
        paths = {
            "content_files": "knowledge/corpus/content-files.txt",
            "context": "knowledge_context.md",
            "deep_dives": "knowledge/deep-dives/dives.json",
            "explorer": "graphify-out/explorer.html",
            "graph": "graphify-out/graph.json",
            "history": "knowledge/git-history/example-repo.ndjson",
            "index": "knowledge/index/store.sqlite",
            "intent": "knowledge/intent/file-tickets.json.gz",
            "labels": "graphify-out/.graphify_labels.json",
            "provenance": "knowledge/provenance.json",
            "relationships": "knowledge/relationships/by-repo/example-repo.json",
            "repository_manifest": "knowledge/repository-manifest.md",
            "semantic": "knowledge/semantic/token-neighbours.json.gz",
            "summaries": "knowledge/summaries/communities.json",
            "ticket_descriptions": "knowledge/intent/ticket-descriptions.json.gz",
            "ticket_tracker": "knowledge/intent/ticket-tracker.json.gz",
            "topics": "knowledge/topics/briefs.json",
        }
        for name, rel in paths.items():
            with self.subTest(layer=name), tempfile.TemporaryDirectory() as raw:
                self.root = Path(raw)
                _touch(self.root, rel)
                present = [n for n, p in _present(self.compute()).items() if p]
                self.assertEqual(present, [name])

    def test_the_content_file_list_is_a_present_digested_layer(self):
        """Breaks if the committed path list a reader greps is left out of the
        manifest: it would be a store artefact no reader is told about."""
        data = b"docs/a.md\ndocs/b.md\n"
        _touch(self.root, "knowledge/corpus/content-files.txt", data.decode())
        layer = self.compute()["layers"]["content_files"]
        self.assertEqual(layer["paths"], ["knowledge/corpus/content-files.txt"])
        self.assertTrue(layer["present"])
        self.assertEqual(layer["digest"], _sha(data))

    def test_an_empty_directory_is_not_a_present_layer(self):
        """Breaks if a directory layer counts as present because the directory
        exists: `mkdir` alone has not built history."""
        (self.root / "knowledge" / "git-history").mkdir(parents=True)
        self.assertFalse(_present(self.compute())["history"])

    def test_directory_layers_are_listed_with_a_trailing_slash(self):
        """Breaks if a directory layer's path is indistinguishable from a file's."""
        layers = self.compute()["layers"]
        self.assertEqual(layers["history"]["paths"], ["knowledge/git-history/"])
        self.assertEqual(layers["relationships"]["paths"], ["knowledge/relationships/by-repo/"])


class CompressedAndPlainVariants(_Store):
    def test_only_the_plain_graph_counts_and_is_listed_first(self):
        """Breaks if only the `.gz` counts as the graph, or the listed path is one
        that is not there."""
        _touch(self.root, "graphify-out/graph.json")
        graph = self.compute()["layers"]["graph"]
        self.assertTrue(graph["present"])
        self.assertEqual(graph["paths"], ["graphify-out/graph.json", "graphify-out/graph.json.gz"])

    def test_only_the_compressed_graph_counts_and_is_listed_first(self):
        """Breaks if only the plain file counts: every store commits the `.gz` and
        gitignores the plain file, so a clone holds the `.gz` alone."""
        _touch(self.root, "graphify-out/graph.json.gz", "")
        graph = self.compute()["layers"]["graph"]
        self.assertTrue(graph["present"])
        self.assertEqual(graph["paths"], ["graphify-out/graph.json.gz", "graphify-out/graph.json"])

    def test_with_neither_the_first_candidate_is_listed_and_the_layer_is_absent(self):
        """Breaks if an absent layer lists no path, or claims to be present."""
        graph = self.compute()["layers"]["graph"]
        self.assertFalse(graph["present"])
        self.assertEqual(graph["paths"], ["graphify-out/graph.json.gz", "graphify-out/graph.json"])

    def test_only_the_compressed_index_counts_and_is_listed_first(self):
        """Breaks if only the plain index counts: on a large estate the plain file
        is over GitHub's 100 MB limit, so a store commits the `.gz` alone."""
        _touch(self.root, "knowledge/index/store.sqlite.gz", "")
        index = self.compute()["layers"]["index"]
        self.assertTrue(index["present"])
        self.assertEqual(
            index["paths"], ["knowledge/index/store.sqlite.gz", "knowledge/index/store.sqlite"]
        )


class StoreIdentity(SettingsIsolated):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def identity(self, store_id: str | None) -> tuple[str, str]:
        config.configure(STORE_ID=store_id or "")
        return store_manifest.store_identity(self.root)

    def test_the_configured_id_names_the_store_first(self):
        """Breaks if `config.STORE_ID` (`KSB_STORE_ID`) is ignored, or loses to the
        origin remote: the override exists for the clone whose remote name is not
        the store's name."""
        _git_repo(self.root, "https://github.com/example-org/a-fork-name.git")
        self.assertEqual(self.identity("example-store"), ("example-store", ""))

    def test_an_https_origin_names_the_store(self):
        """Breaks if the `.git` suffix is kept or the wrong path segment is taken."""
        _git_repo(self.root, "https://github.com/example-org/example-store.git")
        self.assertEqual(self.identity(None)[0], "example-store")

    def test_an_https_origin_without_the_suffix_names_the_store(self):
        """Breaks if the suffix is removed by length rather than by match, which
        would cut a name that never had one."""
        _git_repo(self.root, "https://github.com/example-org/example-store")
        self.assertEqual(self.identity(None)[0], "example-store")

    def test_a_root_below_the_repository_top_is_refused(self):
        """Breaks if a store nested inside another repository takes that
        repository's origin as its name: the id would name a different store."""
        _git_repo(self.root, "https://github.com/example-org/a-parent-repo.git")
        nested = self.root / "nested-store"
        nested.mkdir()
        config.configure(STORE_ID="")
        with self.assertRaises(store_manifest.UsageError) as caught:
            store_manifest.store_identity(nested)
        self.assertIn("KSB_STORE_ID", str(caught.exception))
        self.assertIn("not the top", str(caught.exception))

    def test_a_nested_root_is_named_when_the_id_is_configured(self):
        """Breaks if the top-level check runs even when the id is configured, which
        would leave a nested store no way to be named."""
        _git_repo(self.root, "https://github.com/example-org/a-parent-repo.git")
        nested = self.root / "nested-store"
        nested.mkdir()
        config.configure(STORE_ID="example-store")
        self.assertEqual(store_manifest.store_identity(nested)[0], "example-store")

    def test_an_ssh_origin_names_the_store(self):
        """Breaks if only `/` separates segments: the scp-like ssh form puts a `:`
        between the host and the org."""
        _git_repo(self.root, "git@github.com:example-org/example-store.git")
        self.assertEqual(self.identity(None)[0], "example-store")

    def test_a_single_segment_ssh_origin_names_the_store(self):
        """Breaks if the `:` is not a separator: with no org in the path, the
        whole `git@host:repo` would be read as the name."""
        _git_repo(self.root, "git@github.com:example-store")
        self.assertEqual(self.identity(None)[0], "example-store")

    def test_with_neither_source_the_refusal_names_both(self):
        """Breaks if an undeterminable id is guessed (the directory name) or the
        refusal does not say how to fix it."""
        _git_repo(self.root, None)
        with self.assertRaises(store_manifest.UsageError) as caught:
            self.identity(None)
        self.assertIn("KSB_STORE_ID", str(caught.exception))
        self.assertIn("origin", str(caught.exception))

    def test_outside_a_git_repository_the_refusal_names_both(self):
        """Breaks if git's failure escapes as a traceback rather than a refusal."""
        with self.assertRaises(store_manifest.UsageError) as caught:
            self.identity(None)
        self.assertIn("KSB_STORE_ID", str(caught.exception))
        self.assertIn("origin", str(caught.exception))

    def test_the_org_comes_from_config_and_is_never_required(self):
        """Breaks if the org is read from anywhere but `config.GITHUB_ORG`, or an
        unset org refuses."""
        self.assertEqual(self.identity("example-store"), ("example-store", ""))
        config.configure(GITHUB_ORG="example-org")
        self.assertEqual(self.identity("example-store"), ("example-store", "example-org"))


class TelemetryFlags(_Store):
    def flags(self) -> tuple:
        layers = self.compute()["layers"]
        return (
            layers["graph"]["source_urls"],
            layers["intent"]["reach"],
            layers["deployments"]["present"],
        )

    def test_without_telemetry_every_flag_is_unknown(self):
        """Breaks if a flag nothing measured is reported as false: `null` says
        unknown, `false` says measured and absent, and a reader acts differently."""
        self.assertEqual(self.flags(), (None, None, None))

    def test_a_malformed_telemetry_file_is_unknown_not_a_failure(self):
        """Breaks if a hand-edited or merge-damaged telemetry file stops the
        manifest being computed, or is read as false."""
        _touch(self.root, "knowledge/telemetry.json", "{not json")
        self.assertEqual(self.flags(), (None, None, None))

    def test_a_missing_key_is_unknown(self):
        """Breaks if one recorded stage makes another stage's flag false."""
        _telemetry(self.root, {"explorer.nodes": 10})
        self.assertEqual(self.flags(), (None, None, None))

    def test_recorded_measurements_set_each_flag_true(self):
        """Breaks if a flag reads the wrong key, or inverts its condition."""
        _telemetry(
            self.root,
            {
                "source_urls.linked": 7,
                "intent.repositories_without_reach": 0,
                "deployments.pairs": 3,
            },
        )
        self.assertEqual(self.flags(), (True, True, True))

    def test_recorded_measurements_set_each_flag_false(self):
        """Breaks if a flag is true whenever its key exists: a store with history
        predating reach, no linked node or no deployment pair must say false."""
        _telemetry(
            self.root,
            {
                "source_urls.linked": 0,
                "intent.repositories_without_reach": 2,
                "deployments.pairs": 0,
            },
        )
        self.assertEqual(self.flags(), (False, False, False))


class ExtraLayers(_Store):
    def declare(self, layers) -> None:
        _touch(self.root, "config/store-extra-layers.json", json.dumps(layers))

    def test_no_declaration_gives_no_extra_layers(self):
        """Breaks if a store without the config file is refused or invents entries."""
        self.assertEqual(store_manifest.extra_layers(self.root), {})

    def test_declared_layers_are_listed_with_their_presence(self):
        """Breaks if a declared layer pointing at a missing file is dropped or
        reported present, or if `kind` and the description do not travel."""
        self.declare(
            {
                "journeys": {
                    "path": "docs/journeys/",
                    "description": "Written user journeys.",
                    "kind": "answers",
                },
                "registry": {"path": "knowledge/registry.json", "description": "A registry."},
            }
        )
        _touch(self.root, "docs/journeys/apply.md", "# Apply")
        journeys_digest = _sha(b"apply.md\0" + _sha(b"# Apply").encode())
        self.assertEqual(
            self.compute()["extra_layers"],
            {
                "journeys": {
                    "path": "docs/journeys/",
                    "description": "Written user journeys.",
                    "kind": "answers",
                    "present": True,
                    "digest": journeys_digest,
                },
                "registry": {
                    "path": "knowledge/registry.json",
                    "description": "A registry.",
                    "present": False,
                    "digest": None,
                },
            },
        )

    def test_an_absolute_path_is_refused(self):
        """Breaks if a declared layer can point outside the store by naming an
        absolute path, which no other clone would hold."""
        self.declare({"outside": {"path": "/etc/hosts", "description": "x"}})
        with self.assertRaises(store_manifest.UsageError) as caught:
            store_manifest.extra_layers(self.root)
        self.assertIn("outside", str(caught.exception))

    def test_an_absolute_path_inside_the_store_is_refused(self):
        """Breaks if only containment is checked: an absolute path to this clone's
        own file resolves inside it here and names nothing in any other clone."""
        self.declare({"mine": {"path": str(self.root / "docs" / "x.md"), "description": "x"}})
        with self.assertRaises(store_manifest.UsageError) as caught:
            store_manifest.extra_layers(self.root)
        self.assertIn("mine", str(caught.exception))

    def test_a_symlink_out_of_the_store_is_refused(self):
        """Breaks if containment is checked on the spelled path rather than the
        resolved one: a link inside the store can name any file outside it."""
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        _touch(Path(outside.name), "secret.txt", "x")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "link").symlink_to(outside.name, target_is_directory=True)
        self.declare({"linked": {"path": "docs/link/secret.txt", "description": "x"}})
        with self.assertRaises(store_manifest.UsageError) as caught:
            store_manifest.extra_layers(self.root)
        self.assertIn("linked", str(caught.exception))

    def test_the_store_root_itself_is_refused(self):
        """Breaks if a layer may name the whole store: `.` resolves to the root,
        which is inside nothing, and would digest every file the store holds."""
        self.declare({"everything": {"path": ".", "description": "x"}})
        with self.assertRaises(store_manifest.UsageError) as caught:
            store_manifest.extra_layers(self.root)
        self.assertIn("everything", str(caught.exception))

    def test_a_name_the_library_uses_is_refused(self):
        """Breaks if an extra layer may take a library layer's name: the store digest
        is keyed by name, so the extra layer would replace the graph's line in it."""
        self.declare({"graph": {"path": "docs/x.md", "description": "x"}})
        with self.assertRaises(store_manifest.UsageError) as caught:
            store_manifest.extra_layers(self.root)
        self.assertIn("graph", str(caught.exception))

    def test_an_empty_declared_directory_is_not_present(self):
        """Breaks if an extra layer counts an empty directory as present, which the
        library's own directory layers do not."""
        (self.root / "docs" / "journeys").mkdir(parents=True)
        self.declare({"journeys": {"path": "docs/journeys/", "description": "x"}})
        self.assertFalse(store_manifest.extra_layers(self.root)["journeys"]["present"])

    def test_a_path_climbing_out_of_the_store_is_refused(self):
        """Breaks if containment is checked lexically on the first segment only:
        `knowledge/../../x` starts inside and ends outside."""
        self.declare({"climb": {"path": "knowledge/../../secrets.txt", "description": "x"}})
        with self.assertRaises(store_manifest.UsageError) as caught:
            store_manifest.extra_layers(self.root)
        self.assertIn("climb", str(caught.exception))

    def test_a_path_that_climbs_and_returns_is_accepted(self):
        """Breaks if every `..` is refused rather than every escape."""
        self.declare({"back": {"path": "knowledge/../docs/x.md", "description": "x"}})
        self.assertFalse(store_manifest.extra_layers(self.root)["back"]["present"])

    def test_equivalent_spellings_of_a_path_are_recorded_one_way(self):
        """Breaks if a path is written as declared: `./answers` and
        `answers/../answers` would name one layer in three spellings, and a
        directory would lack the trailing slash library layers carry."""
        _touch(self.root, "answers/a.md", "# A")
        self.declare(
            {
                "dotted": {"path": "./answers", "description": "x"},
                "climbed": {"path": "answers/../answers", "description": "x"},
                "plain": {"path": "answers/", "description": "x"},
                "file": {"path": "./knowledge/./r.json", "description": "x"},
            }
        )
        found = store_manifest.extra_layers(self.root)
        recorded = {name: found[name]["path"] for name in found}
        self.assertEqual(
            recorded,
            {
                "dotted": "answers/",
                "climbed": "answers/",
                "plain": "answers/",
                "file": "knowledge/r.json",
            },
        )

    def test_a_malformed_declaration_is_refused(self):
        """Breaks if a declaration without a path, with an unknown kind, or that is
        not JSON is skipped silently - the store would lose a layer unnoticed."""
        for declared in (
            "[1, 2]",
            "{not json",
            json.dumps({"x": {"description": "no path"}}),
            json.dumps({"x": {"path": "a.md"}}),
            json.dumps({"x": {"path": "a.md", "description": "d", "kind": "answer"}}),
        ):
            with self.subTest(declared=declared):
                _touch(self.root, "config/store-extra-layers.json", declared)
                with self.assertRaises(store_manifest.UsageError):
                    store_manifest.extra_layers(self.root)


class Determinism(_Store):
    def test_two_computes_over_one_tree_are_byte_identical(self):
        """Breaks if anything time- or order-dependent enters the manifest, which
        would make every rebuild of an unchanged store a diff."""
        _touch(self.root, "graphify-out/graph.json.gz", "")
        _touch(self.root, "knowledge/summaries/communities.json")
        _telemetry(self.root, {"intent.repositories_without_reach": 0})
        first = json.dumps(self.compute(), indent=1, sort_keys=True)
        second = json.dumps(self.compute(), indent=1, sort_keys=True)
        self.assertEqual(first, second)

    def test_a_file_layer_digest_is_the_sha256_of_its_bytes(self):
        """Breaks if a layer digest hashes anything but the file's content, or
        a directory layer's digest leaves out its file names."""
        _touch(self.root, "knowledge/provenance.json", '{"repositories": {}}')
        _touch(self.root, "knowledge/git-history/a.ndjson", "one")
        _touch(self.root, "knowledge/git-history/b.ndjson", "two")
        layers = self.compute()["layers"]
        self.assertEqual(layers["provenance"]["digest"], _sha(b'{"repositories": {}}'))
        lines = sorted([f"a.ndjson\0{_sha(b'one')}", f"b.ndjson\0{_sha(b'two')}"])
        self.assertEqual(layers["history"]["digest"], _sha("\n".join(lines).encode()))


class Digest(_Store):
    """A store's digest names its content, so a reader can tell a rebuild that
    changed something from one that did not, across clones."""

    def setUp(self):
        super().setUp()
        _touch(self.root, "graphify-out/graph.json.gz", "graph")
        _touch(self.root, "knowledge/summaries/communities.json", '{"1": "a"}')
        _touch(self.root, "knowledge/git-history/example-repo.ndjson", "history")

    def digest(self) -> str:
        return self.compute()["digest"]

    def test_moving_every_modification_time_leaves_the_digest_unchanged(self):
        """Breaks if the digest reads a modification time: a fresh clone and an
        identical rebuild move every mtime, and the committed manifest would churn."""
        before = self.digest()
        for path in self.root.rglob("*"):
            if path.is_file():
                os.utime(path, (1_700_000_000, 1_700_000_000))
        self.assertEqual(self.digest(), before)

    def test_one_changed_byte_changes_the_digest(self):
        """Breaks if the digest reads a file's size or name rather than its bytes:
        the edit below keeps the size."""
        before = self.digest()
        _touch(self.root, "knowledge/summaries/communities.json", '{"1": "b"}')
        self.assertNotEqual(self.digest(), before)

    def test_a_changed_file_inside_a_directory_layer_changes_the_digest(self):
        """Breaks if a directory layer's digest stops at the directory."""
        before = self.digest()
        _touch(self.root, "knowledge/git-history/example-repo.ndjson", "histORY")
        self.assertNotEqual(self.digest(), before)

    def test_a_renamed_file_inside_a_directory_layer_changes_the_digest(self):
        """Breaks if a directory digest hashes contents without their names."""
        before = self.digest()
        history = self.root / "knowledge" / "git-history"
        (history / "example-repo.ndjson").rename(history / "other-repo.ndjson")
        self.assertNotEqual(self.digest(), before)

    def test_removing_a_layer_changes_the_digest(self):
        """Breaks if an absent layer contributes nothing distinguishable."""
        before = self.digest()
        (self.root / "knowledge" / "summaries" / "communities.json").unlink()
        self.assertNotEqual(self.digest(), before)

    def test_an_extra_layer_changing_changes_the_digest(self):
        """Breaks if the store digest leaves out the store's own layers."""
        _touch(
            self.root,
            "config/store-extra-layers.json",
            json.dumps({"registry": {"path": "knowledge/registry.json", "description": "x"}}),
        )
        _touch(self.root, "knowledge/registry.json", "one")
        before = self.digest()
        _touch(self.root, "knowledge/registry.json", "two")
        self.assertNotEqual(self.digest(), before)

    def test_the_store_digest_is_derived_from_the_layer_lines(self):
        """Breaks if the store digest's construction drifts from what a second
        reader can recompute from the manifest alone."""
        manifest = self.compute()
        lines = sorted(
            f"{name}\0{layer['digest'] or '-'}" for name, layer in manifest["layers"].items()
        )
        self.assertEqual(manifest["digest"], _sha("\n".join(lines).encode()))


class Symlinks(_Store):
    """A link a layer holds must stay inside the store: the manifest is committed
    and read from another machine, and a digest over a file outside the store
    names content no clone carries."""

    def setUp(self):
        super().setUp()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        self.outside = Path(outside.name)
        _touch(self.outside, "secret.txt", "outside")

    def test_a_file_layer_linking_outside_the_store_is_refused(self):
        """Breaks if a file layer that is a link out of the store is hashed as its
        target: the digest would describe a file no other clone holds."""
        (self.root / "knowledge").mkdir()
        (self.root / "knowledge" / "provenance.json").symlink_to(self.outside / "secret.txt")
        with self.assertRaises(store_manifest.UsageError) as caught:
            self.compute()
        self.assertIn("knowledge/provenance.json", str(caught.exception))

    def test_a_directory_entry_linking_outside_the_store_is_refused(self):
        """Breaks if an entry of a directory layer is hashed through a link that
        leaves the store."""
        _touch(self.root, "knowledge/git-history/example-repo.ndjson", "history")
        (self.root / "knowledge" / "git-history" / "stray.ndjson").symlink_to(
            self.outside / "secret.txt"
        )
        with self.assertRaises(store_manifest.UsageError) as caught:
            self.compute()
        self.assertIn("stray.ndjson", str(caught.exception))

    def test_a_directory_link_leaving_the_store_is_refused(self):
        """Breaks if a linked directory inside a layer is skipped silently rather
        than refused: it would look like part of the layer and be none of it."""
        _touch(self.root, "knowledge/git-history/example-repo.ndjson", "history")
        (self.root / "knowledge" / "git-history" / "elsewhere").symlink_to(
            self.outside, target_is_directory=True
        )
        with self.assertRaises(store_manifest.UsageError):
            self.compute()

    def test_a_link_staying_inside_the_store_is_hashed_as_its_target(self):
        """Breaks if a link within the store is refused, or digested as its own
        text rather than the bytes it points at: a store may share one file."""
        _touch(self.root, "knowledge/real.json", "shared")
        (self.root / "knowledge" / "provenance.json").symlink_to(self.root / "knowledge/real.json")
        _touch(self.root, "knowledge/git-history/a.ndjson", "history")
        (self.root / "knowledge" / "git-history" / "b.ndjson").symlink_to(
            self.root / "knowledge/real.json"
        )
        layers = self.compute()["layers"]
        self.assertEqual(layers["provenance"]["digest"], _sha(b"shared"))
        direct = _sha(
            "\n".join([f"a.ndjson\0{_sha(b'history')}", f"b.ndjson\0{_sha(b'shared')}"]).encode()
        )
        self.assertEqual(layers["history"]["digest"], direct)


class WhatACloneReceives(_Store):
    """The manifest describes what a clone of the store gets, so a layer whose
    files git would not commit is local to the builder and not offered to askers.
    Every test uses a real `git init` store and a real `.gitignore`."""

    def setUp(self):
        super().setUp()
        _git_repo(self.root, "https://example.com/example-org/example-store.git")
        _touch(self.root, ".gitignore", "knowledge/git-history/\ngraphify-out/graph.json\n")

    def layers(self) -> dict:
        return self.compute()["layers"]

    def test_an_ignored_directory_layer_is_local_only(self):
        """Breaks if a gitignored directory layer is offered to askers: `history`
        is never committed, so a clone would be told it can read files it lacks."""
        _touch(self.root, "knowledge/git-history/example-repo.ndjson", "history")
        history = self.layers()["history"]
        self.assertFalse(history["present"])
        self.assertTrue(history["local_only"])
        self.assertIsNone(history["digest"])
        self.assertEqual(history["paths"], ["knowledge/git-history/"])

    def test_a_plain_graph_whose_file_is_ignored_is_local_only(self):
        """Breaks if an ignored plain `graph.json` with no archive counts as the
        graph: no clone receives it."""
        _touch(self.root, "graphify-out/graph.json", "plain")
        graph = self.layers()["graph"]
        self.assertFalse(graph["present"])
        self.assertTrue(graph["local_only"])
        self.assertIsNone(graph["digest"])

    def test_a_committed_archive_beside_an_ignored_plain_file_stays_present(self):
        """Breaks if the ignored plain file hides the `.gz` that clones do get, or
        the digest is taken over the file that is not committed."""
        _touch(self.root, "graphify-out/graph.json", "plain")
        _touch(self.root, "graphify-out/graph.json.gz", "archive")
        graph = self.layers()["graph"]
        self.assertTrue(graph["present"])
        self.assertNotIn("local_only", graph)
        self.assertEqual(graph["paths"][0], "graphify-out/graph.json.gz")
        self.assertEqual(graph["digest"], _sha(b"archive"))

    def test_a_directory_digest_covers_only_the_files_a_clone_receives(self):
        """Breaks if an ignored file inside a layer that is otherwise committed
        changes its digest: builder and clone would then disagree."""
        _touch(self.root, ".gitignore", "docs/journeys/draft.md\n")
        _touch(self.root, "docs/journeys/apply.md", "# Apply")
        _touch(self.root, "docs/journeys/draft.md", "# Draft")
        _touch(
            self.root,
            "config/store-extra-layers.json",
            json.dumps({"journeys": {"path": "docs/journeys/", "description": "x"}}),
        )
        journeys = self.compute()["extra_layers"]["journeys"]
        self.assertTrue(journeys["present"])
        self.assertEqual(journeys["digest"], _sha(b"apply.md\0" + _sha(b"# Apply").encode()))

    def test_an_ignored_file_with_a_quote_in_its_name_is_still_ignored(self):
        """Breaks if a name holding a quote is mis-read when git lists the ignored
        files: the ignored file would be counted into the digest."""
        _touch(self.root, ".gitignore", 'docs/journeys/odd"name.md\n')
        _touch(self.root, "docs/journeys/apply.md", "# Apply")
        _touch(self.root, 'docs/journeys/odd"name.md', "# Odd")
        _touch(
            self.root,
            "config/store-extra-layers.json",
            json.dumps({"journeys": {"path": "docs/journeys/", "description": "x"}}),
        )
        journeys = self.compute()["extra_layers"]["journeys"]
        self.assertEqual(journeys["digest"], _sha(b"apply.md\0" + _sha(b"# Apply").encode()))

    def journeys(self) -> dict:
        _touch(
            self.root,
            "config/store-extra-layers.json",
            json.dumps({"journeys": {"path": "docs/journeys/", "description": "x"}}),
        )
        return self.compute()["extra_layers"]["journeys"]

    def test_unusual_file_names_that_are_ignored_are_excluded(self):
        """Breaks if an ignored file is counted because its name defeats the
        parsing of git's list: a carriage return, a bell, U+2028 and U+0085 are all
        legal in a file name and each splits or quotes in a line-based reader."""
        _touch(self.root, ".gitignore", "docs/journeys/*\n!docs/journeys/apply.md\n")
        _touch(self.root, "docs/journeys/apply.md", "# Apply")
        for name in ("cr\rx.md", "bell\x07.md", "line\u2028sep.md", "next\u0085line.md"):
            _touch(self.root, f"docs/journeys/{name}", "# Odd")
        journeys = self.journeys()
        self.assertEqual(journeys["digest"], _sha(b"apply.md\0" + _sha(b"# Apply").encode()))

    def test_a_force_added_file_that_a_rule_ignores_counts_as_committed(self):
        """Breaks if a tracked file is dropped because a rule matches it: git
        commits it, so a clone receives it."""
        _touch(self.root, "knowledge/git-history/example-repo.ndjson", "history")
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(self.root)]
        subprocess.run(
            [*git, "add", "-f", "knowledge/git-history/example-repo.ndjson"],
            check=True,
            env=git_env(),
        )
        history = self.layers()["history"]
        self.assertTrue(history["present"])
        self.assertNotIn("local_only", history)
        self.assertEqual(
            history["digest"], _sha(b"example-repo.ndjson\0" + _sha(b"history").encode())
        )

    def test_an_ignored_file_far_down_a_long_listing_is_still_excluded(self):
        """Breaks if only the first part of a large directory is checked: 250
        files, one ignored near the end."""
        _touch(self.root, ".gitignore", "docs/journeys/page-249.md\n")
        names = [f"page-{number:03d}.md" for number in range(250)]
        for name in names:
            _touch(self.root, f"docs/journeys/{name}", name)
        lines = sorted(f"{n}\0{_sha(n.encode())}" for n in names if n != "page-249.md")
        self.assertEqual(self.journeys()["digest"], _sha("\n".join(lines).encode()))

    def test_a_layer_under_a_symlinked_directory_is_never_given_a_wrong_digest(self):
        """Breaks if a layer below a linked directory is hashed: git lists nothing
        there and says nothing, so the ignored file would be counted and a clone
        would disagree."""
        _touch(self.root, ".gitignore", "real/git-history/\n")
        _touch(self.root, "real/git-history/example-repo.ndjson", "history")
        (self.root / "knowledge").symlink_to(self.root / "real", target_is_directory=True)
        with self.assertRaises(store_manifest.UsageError) as caught:
            self.layers()
        self.assertIn("knowledge/git-history", str(caught.exception))
        self.assertIn("link to a directory", str(caught.exception))

    def test_a_git_error_inside_a_repository_is_a_refusal_naming_the_layer(self):
        """Breaks if a git failure is taken as "nothing is ignored": the manifest
        would offer files a clone may not receive and say nothing."""
        _touch(self.root, "knowledge/provenance.json")
        real = store_manifest.run_git

        def failing(arguments):
            if "ls-files" in arguments:
                raise subprocess.CalledProcessError(128, "git", stderr="fatal: example failure")
            return real(arguments)

        with self.assertRaises(store_manifest.UsageError) as caught:
            store_manifest.compute(self.root, run=failing)
        self.assertIn("knowledge/provenance.json", str(caught.exception))
        self.assertIn("example failure", str(caught.exception))

    def test_an_ignored_extra_layer_is_local_only(self):
        """Breaks if the rule stops at the library's layers: a store's own layer
        that git ignores is as unavailable to a clone."""
        _touch(self.root, ".gitignore", "knowledge/registry.json\n")
        _touch(self.root, "knowledge/registry.json", "one")
        _touch(
            self.root,
            "config/store-extra-layers.json",
            json.dumps({"registry": {"path": "knowledge/registry.json", "description": "x"}}),
        )
        registry = self.compute()["extra_layers"]["registry"]
        self.assertFalse(registry["present"])
        self.assertTrue(registry["local_only"])
        self.assertIsNone(registry["digest"])

    def test_a_local_only_layer_counts_as_absent_in_the_store_digest(self):
        """Breaks if a layer clones lack still moves the store digest: it must
        contribute `-`, exactly as if the files were never built."""
        _touch(self.root, "knowledge/summaries/communities.json", "{}")
        without = self.compute()["digest"]
        _touch(self.root, "knowledge/git-history/example-repo.ndjson", "history")
        self.assertEqual(self.compute()["digest"], without)

    def test_a_clone_computes_the_builders_store_digest(self):
        """Breaks if builder and clone disagree on the store digest: the one value
        a reader compares across machines. The clone is a real `git clone`."""
        _touch(self.root, "graphify-out/graph.json", "plain")
        _touch(self.root, "graphify-out/graph.json.gz", "archive")
        _touch(self.root, "knowledge/git-history/example-repo.ndjson", "history")
        _touch(self.root, "knowledge/summaries/communities.json", '{"1": "a"}')
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(self.root)]
        subprocess.run([*git, "add", "."], check=True, env=git_env())
        subprocess.run([*git, "commit", "-q", "-m", "store"], check=True, env=git_env())
        builder = self.compute()
        clones = tempfile.TemporaryDirectory()
        self.addCleanup(clones.cleanup)
        clone = Path(clones.name) / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(self.root), str(clone)], check=True, env=git_env()
        )
        self.assertFalse((clone / "knowledge" / "git-history").exists())
        copy = store_manifest.compute(clone)
        self.assertEqual(copy["digest"], builder["digest"])
        self.assertEqual(copy["layers"]["graph"], builder["layers"]["graph"])


class SourcesAsOf(_Store):
    def provenance(self, repositories, external=None) -> None:
        document = {"repositories": repositories}
        if external is not None:
            document["external"] = external
        _touch(self.root, "knowledge/provenance.json", json.dumps(document))

    def test_the_newest_commit_date_is_reported_as_recorded(self):
        """Breaks if dates are compared as strings: the first date below sorts
        last as text but is the earlier instant, five hours east of UTC."""
        self.provenance(
            {
                "a": {"sha": "1", "committed": "2026-01-02T03:00:00+05:00"},
                "b": {"sha": "2", "committed": "2026-01-01T23:00:00+00:00"},
                "c": {"sha": "3", "committed": "2025-06-01T00:00:00+00:00"},
            }
        )
        self.assertEqual(self.compute()["sources_as_of"], "2026-01-01T23:00:00+00:00")

    def test_fetch_only_repositories_are_not_sources(self):
        """Breaks if `external` counts: a fetch-only repository is not in the
        graph, so its date says nothing about what the store describes."""
        self.provenance(
            {"a": {"sha": "1", "committed": "2026-01-01T00:00:00+00:00"}},
            external={"x": {"sha": "9", "committed": "2027-01-01T00:00:00+00:00"}},
        )
        self.assertEqual(self.compute()["sources_as_of"], "2026-01-01T00:00:00+00:00")

    def test_without_provenance_it_is_null(self):
        """Breaks if a store that recorded no source invents a date."""
        self.assertIsNone(self.compute()["sources_as_of"])

    def test_an_unparseable_date_is_skipped(self):
        """Breaks if one blank or damaged `committed` stops the manifest."""
        self.provenance(
            {
                "a": {"sha": "1", "committed": ""},
                "b": {"sha": "2", "committed": "2026-01-01T00:00:00+00:00"},
            }
        )
        self.assertEqual(self.compute()["sources_as_of"], "2026-01-01T00:00:00+00:00")


def _unaccounted(namespace, root: Path, layers, not_a_layer) -> list[str]:
    """Config names of the paths under `root` that are neither a layer nor excused.

    The matching rule: a config path is a layer's when its store-relative form
    equals one of that layer's candidate paths. A `dir` layer is therefore matched
    by its directory, and a file inside a directory layer is not matched by it -
    such a file is a separate artefact and needs its own decision. A name is
    taken whatever it ends in: any `Path` attribute counts, so `EXTERNAL_CONFIG`
    is held as well as every `*_PATH` and `*_DIR`.
    """
    claimed = {rel for layer in layers for rel in layer.paths}
    unaccounted = []
    for name, value in sorted(vars(namespace).items()):
        if not isinstance(value, Path) or value == root or root not in value.parents:
            continue
        if value.relative_to(root).as_posix() not in claimed and name not in not_a_layer:
            unaccounted.append(name)
    return unaccounted


class EveryArtefactIsAccountedFor(unittest.TestCase):
    # When #457 (`INDEX_PATH`) and #455 (the relationships directory, the
    # relationship-kinds path and its config path) merge, their config names fail
    # `test_every_config_path_is_a_layer_or_named_as_not_one` until each is
    # accounted for. `LAYERS` already holds `index` and `relationships` by literal
    # path, so the merger maps `INDEX_PATH` and the relationships directory to those
    # layers by making config and the layer agree on one path, and lists the
    # relationship kinds files here in `NOT_A_LAYER` or as a layer.

    def test_every_config_path_is_a_layer_or_named_as_not_one(self):
        """Breaks if a stage gains an artefact path in config that is neither a
        manifest layer nor named in `NOT_A_LAYER`: a reader would never be told
        the store holds it, and nobody would have decided that it should not."""
        missing = _unaccounted(
            config, config.ROOT, store_manifest.LAYERS, store_manifest.NOT_A_LAYER
        )
        self.assertEqual(missing, [], f"not a layer and not in NOT_A_LAYER: {missing}")

    def test_the_excusal_list_holds_no_stale_or_contradictory_entry(self):
        """Breaks if `NOT_A_LAYER` names a config path that no longer exists, or
        one that is also a layer, or gives no reason: a list that only grows
        stops saying what the library writes."""
        for name, reason in store_manifest.NOT_A_LAYER.items():
            with self.subTest(name=name):
                value = getattr(config, name, None)
                self.assertIsInstance(value, Path, f"{name} is not a config path")
                self.assertTrue(reason.strip(), f"{name} has no reason")
                self.assertEqual(
                    _unaccounted(
                        types.SimpleNamespace(**{name: value}),
                        config.ROOT,
                        store_manifest.LAYERS,
                        {},
                    ),
                    [name],
                    f"{name} is both a layer's path and in NOT_A_LAYER",
                )

    def test_the_check_tells_a_layer_from_an_excused_path_from_a_stray_one(self):
        """The comparison above is covered by the mutation entry 'store manifest:
        a layer dropped from LAYERS' (tests/mutation_gate.py), and this proves the
        comparison discriminates in the same run: on a synthetic config it must
        name the stray path and only it. A comparison that returned nothing for
        every input would pass the real check vacuously."""
        root = Path("/example/store")
        layers = (
            store_manifest.Layer("a_file", ("knowledge/a.json.gz", "knowledge/a.json")),
            store_manifest.Layer("a_dir", ("knowledge/dir",), "dir"),
        )
        synthetic = types.SimpleNamespace(
            ROOT=root,
            A_PATH=root / "knowledge" / "a.json",  # the layer's second candidate
            A_DIR=root / "knowledge" / "dir",  # a directory layer
            INSIDE_PATH=root / "knowledge" / "dir" / "x.json",  # inside one: still needs a decision
            EXCUSED_PATH=root / "knowledge" / "e.json",
            STRAY_PATH=root / "knowledge" / "stray.json",
            OUTSIDE_PATH=Path("/elsewhere/z.json"),  # not under the root: out of scope
            NOT_A_PATH="knowledge/stray2.json",  # not a Path at all
        )
        self.assertEqual(
            _unaccounted(synthetic, root, layers, {"EXCUSED_PATH": "an input"}),
            ["INSIDE_PATH", "STRAY_PATH"],
        )


if __name__ == "__main__":
    unittest.main()
