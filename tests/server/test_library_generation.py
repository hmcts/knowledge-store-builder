"""The generated store is what it claims to be: the pinned library release, a
clustered graph, and every layer the generator promises.

These read `library_store/store/` as an artefact, not through the server: they
check the generation itself, so a store regenerated wrongly fails here before
any tool test can describe the wrong thing. Expected values are literals from
the generator's inputs and the frozen environment it runs in.
"""

import gzip
import json
import re
import sqlite3
import tempfile
import unittest

from pathlib import Path

from library_store.generate import NO_INDEX, OUT, PARTIAL, REQUIREMENTS, RETIRED
from library_store.harness import LIBRARY_LAYERS

MANIFEST = "knowledge/store-manifest.json"


def _manifest(store: Path = OUT) -> dict:
    return json.loads((store / MANIFEST).read_text("utf-8"))


class LibraryGenerationTest(unittest.TestCase):
    def test_manifest_is_the_pinned_release_and_format(self):
        """Catches: the store regenerated from another library release, or at
        another manifest format, without the record moving - every contract test
        would then describe a release nobody pinned."""
        manifest = _manifest()
        self.assertEqual(manifest["library_version"], "0.19.1")
        self.assertEqual(manifest["format"], {"major": 1, "minor": 4})

    def test_every_node_has_a_community(self):
        """Catches: a re-clustering that did not persist (the documented
        `cluster-only` failure), leaving nodes with no community, so `community`
        and every summary keyed by community id read a partial graph."""
        with gzip.open(OUT / "graphify-out/graph.json.gz", "rt", encoding="utf-8") as stream:
            nodes = json.load(stream)["nodes"]
        self.assertTrue(nodes)
        missing = [node["id"] for node in nodes if not isinstance(node.get("community"), int)]
        self.assertEqual(missing, [])

    def test_the_partitioner_is_recorded(self):
        """Catches: a store clustered in an environment whose partitioner differs
        from the one the frozen requirements resolve - the communities would not
        reproduce, and nothing would say why."""
        header = REQUIREMENTS.read_text("utf-8")
        named = re.search(r"^# partitioner: (\w+)$", header, re.MULTILINE)
        assert named is not None, "requirements.txt names no partitioner"
        record = json.loads((OUT / "graphify-out/clustering-inputs.json").read_text("utf-8"))
        self.assertEqual(record["partitioner"], named.group(1))
        self.assertEqual(named.group(1), "louvain")

    def test_every_library_layer_is_present(self):
        """Catches: a library stage that exits 0 and writes nothing (fetch-tickets
        with no tracker settings, a merge that skipped every entry), committed as
        a store missing a layer the tool tests then silently skip; and a manifest
        that stops naming a layer at all."""
        manifest = _manifest()
        layers = manifest["layers"]
        # The same names as `EXPECTED` in library_store/pipeline.py, which refuses
        # to finish a build without them: v0.19.1's 18 layers plus `deployments`.
        # Keep the two in step.
        self.assertEqual(sorted(layers), sorted(LIBRARY_LAYERS))
        for name in LIBRARY_LAYERS:
            with self.subTest(layer=name):
                self.assertIs(layers[name]["present"], True)
        for name in ("ownership_notes", "answers"):
            with self.subTest(extra_layer=name):
                self.assertIs(manifest["extra_layers"][name]["present"], True)

    def test_no_index_store_is_the_same_store_without_its_index(self):
        """Catches: the no-index store built from other inputs, or with another
        stage skipped, so the graph-fallback tests compare against a store that
        differs from `store/` in more than the index."""
        full, bare = _manifest(), _manifest(NO_INDEX)
        self.assertIs(full["layers"]["index"]["present"], True)
        self.assertIs(bare["layers"]["index"]["present"], False)
        self.assertEqual(sorted(bare["layers"]), sorted(full["layers"]))
        for name, layer in full["layers"].items():
            if name == "index":
                continue
            with self.subTest(layer=name):
                self.assertIs(bare["layers"][name]["present"], layer["present"])
        self.assertEqual(
            {name: layer["present"] for name, layer in bare["extra_layers"].items()},
            {name: layer["present"] for name, layer in full["extra_layers"].items()},
        )


GRAPH = "graphify-out/graph.json.gz"
INDEX = "knowledge/index/store.sqlite.gz"


def _graph_nodes(store: Path, repo: str) -> int:
    """The committed graph's nodes whose `repo` is `repo`."""
    with gzip.open(store / GRAPH, "rt", encoding="utf-8") as stream:
        return sum(1 for node in json.load(stream)["nodes"] if node.get("repo") == repo)


def _index_rows(store: Path, repo: str) -> tuple[list[str] | None, int]:
    """The committed index's `files` rows for `repo` (None when `repos` does not
    list it) and the nodes on them."""
    with tempfile.TemporaryDirectory() as tmp:
        plain = Path(tmp) / "store.sqlite"
        with gzip.open(store / INDEX, "rb") as packed:
            plain.write_bytes(packed.read())
        connection = sqlite3.connect(plain)
        try:
            listed = connection.execute("SELECT k FROM repos WHERE name = ?", (repo,)).fetchone()
            if listed is None:
                return None, 0
            files = [
                path
                for (path,) in connection.execute(
                    "SELECT path FROM files WHERE repo = ? ORDER BY path", listed
                )
            ]
            (nodes,) = connection.execute(
                "SELECT count(*) FROM nodes JOIN files ON nodes.file = files.k WHERE files.repo = ?",
                listed,
            ).fetchone()
        finally:
            connection.close()
    return files, nodes


class PartialStoreInputTest(unittest.TestCase):
    def test_the_retired_repository_has_ticket_rows_and_no_node(self):
        """Catches: the partial store regenerated without its retired repository
        reaching the ticket layer - `--retire` not run, or its history pruned -
        or with a clone graphify extracted, so the server tests over it no
        longer read a repository only the ticket layer names. The index's
        `repos` lists each `RETIRED` repository for its tickets alone: its file
        rows are the files its commits landed, with no node on them, and the
        graph read whole holds none. alpha-lib, read the same way, has nodes,
        so a zero is not a blind read."""
        for name, commits in RETIRED.items():
            landed = sorted({path for _, _, files in commits for path in files})
            with self.subTest(repo=name):
                self.assertEqual(_index_rows(PARTIAL.store, name), (landed, 0))
                self.assertEqual(_graph_nodes(PARTIAL.store, name), 0)
                self.assertEqual(_graph_nodes(PARTIAL.no_index, name), 0)
        self.assertGreater(_index_rows(PARTIAL.store, "alpha-lib")[1], 0)
        self.assertGreater(_graph_nodes(PARTIAL.store, "alpha-lib"), 0)
        self.assertGreater(_graph_nodes(PARTIAL.no_index, "alpha-lib"), 0)


if __name__ == "__main__":
    unittest.main()
