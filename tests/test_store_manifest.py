"""The store manifest says what a store holds, measured from the files on disk.

Every test builds a real store layout in a temporary directory - real files at
the paths the stages write, a real `knowledge/telemetry.json` in the shape
`telemetry.record` writes, and a real git repository for the identity - and
asserts on the manifest `compute` returns. Expected values are written out by
hand. Each test names the break it catches.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from settings_isolation import SettingsIsolated
from knowledgestore import config, store_manifest
from knowledgestore.git_env import git_env

ALL_LAYERS = (
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


@contextlib.contextmanager
def _store_id(value: str | None):
    """`KSB_STORE_ID` set to `value`, or unset, for the duration only."""
    with mock.patch.dict(os.environ):
        os.environ.pop("KSB_STORE_ID", None)
        if value is not None:
            os.environ["KSB_STORE_ID"] = value
        yield


def _git_repo(root: Path, origin: str | None) -> None:
    """A real git repository at `root`, with `origin` set when one is given."""
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=git_env())
    if origin is not None:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", origin],
            check=True,
            env=git_env(),
        )


class _Store(SettingsIsolated):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def compute(self) -> dict:
        with _store_id("example-store"):
            return store_manifest.compute(self.root)

    def present(self) -> dict:
        return {name: layer["present"] for name, layer in self.compute()["layers"].items()}


class AnEmptyStore(_Store):
    def test_every_layer_is_absent_and_built_is_null(self):
        """Breaks if a layer is reported present without its file, or `built` reads a
        clock: a fresh `discover` has built nothing, and the manifest must say so."""
        manifest = self.compute()
        self.assertEqual(sorted(manifest["layers"]), list(ALL_LAYERS))
        expected: dict[str, bool | None] = {name: False for name in ALL_LAYERS}
        expected["deployments"] = None  # nothing measured, so unknown rather than absent
        self.assertEqual(self.present(), expected)
        self.assertIsNone(manifest["built"])
        self.assertEqual(manifest["extra_layers"], {})

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
        self.assertEqual(self.present(), expected)

    def test_every_layer_reads_its_own_path(self):
        """Breaks if any one layer's path is wrong: each file is created alone and
        only its own layer may turn present."""
        paths = {
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
                present = [n for n, p in self.present().items() if p]
                self.assertEqual(present, [name])

    def test_an_empty_directory_is_not_a_present_layer(self):
        """Breaks if a directory layer counts as present because the directory
        exists: `mkdir` alone has not built history."""
        (self.root / "knowledge" / "git-history").mkdir(parents=True)
        self.assertFalse(self.present()["history"])

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
        with _store_id(store_id):
            return store_manifest.store_identity(self.root)

    def test_the_environment_names_the_store_first(self):
        """Breaks if `KSB_STORE_ID` is ignored, or loses to the origin remote: the
        override exists for the clone whose remote name is not the store's name."""
        _git_repo(self.root, "https://github.com/example-org/a-fork-name.git")
        self.assertEqual(self.identity("example-store"), ("example-store", ""))

    def test_an_https_origin_names_the_store(self):
        """Breaks if the `.git` suffix is kept or the wrong path segment is taken."""
        _git_repo(self.root, "https://github.com/example-org/example-store.git")
        self.assertEqual(self.identity(None)[0], "example-store")

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
        self.assertEqual(
            self.compute()["extra_layers"],
            {
                "journeys": {
                    "path": "docs/journeys/",
                    "description": "Written user journeys.",
                    "kind": "answers",
                    "present": True,
                },
                "registry": {
                    "path": "knowledge/registry.json",
                    "description": "A registry.",
                    "present": False,
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

    def test_built_is_the_newest_present_layer_files_modification_time(self):
        """Breaks if `built` reads the clock, takes the oldest file, ignores a file
        inside a directory layer, or counts a file that is not a layer."""
        older = _touch(self.root, "knowledge/provenance.json")
        newest = _touch(self.root, "knowledge/git-history/example-repo.ndjson", "")
        unrelated = _touch(self.root, "knowledge/telemetry.json", '{"measurements": {}}')
        os.utime(older, (1_700_000_000, 1_700_000_000))
        os.utime(newest, (1_760_950_800, 1_760_950_800))
        os.utime(unrelated, (1_800_000_000, 1_800_000_000))
        expected = datetime.datetime(2025, 10, 20, 9, 0, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(expected.timestamp(), 1_760_950_800)
        self.assertEqual(self.compute()["built"], "2025-10-20T09:00:00Z")


if __name__ == "__main__":
    unittest.main()
