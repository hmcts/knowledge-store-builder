"""The packages stage: cross-repository npm edges, grounded in committed files."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config  # noqa: E402
from knowledgestore import build_package_edges as packages  # noqa: E402


def _store(tmp: Path) -> None:
    """A provider publishing @acme/core from projects/core, and a consumer app."""
    provider = tmp / "repositories" / "corelib"
    (provider / ".git").mkdir(parents=True)
    (provider / "projects" / "core").mkdir(parents=True)
    (provider / "projects" / "core" / "package.json").write_text(
        json.dumps({"name": "@acme/core", "version": "0.0.0-PLACEHOLDER"}), encoding="utf-8"
    )
    (provider / "package.json").write_text(json.dumps({"name": "corelib.workspace"}))

    app = tmp / "repositories" / "app"
    (app / ".git").mkdir(parents=True)
    (app / "src").mkdir(parents=True)
    (app / "package.json").write_text(
        json.dumps({"name": "app", "dependencies": {"@acme/core": "^1.0.0"}}), encoding="utf-8"
    )
    (app / "src" / "main.ts").write_text("import { Widget } from '@acme/core';\n", encoding="utf-8")
    (app / "src" / "other.ts").write_text("export const x = 1;\n", encoding="utf-8")

    (tmp / "graphify-out").mkdir()
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {"id": "app::0", "label": "MainThing", "repo": "app", "source_file": "src/main.ts"},
            {"id": "app::9", "label": "AlsoMain", "repo": "app", "source_file": "src/main.ts"},
            {
                "id": "corelib::4",
                "label": "Widget",
                "repo": "corelib",
                "source_file": "projects/core/src/widget.ts",
            },
        ],
        "links": [],
    }
    (tmp / "graphify-out" / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    config.configure(root=str(tmp))


class PackageEdgesTest(SettingsIsolated):
    def test_shared_package_becomes_a_cited_node_with_cited_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store(Path(tmp))
            self.assertEqual(packages.main(), 0)
            graph = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))

        pkg = [n for n in graph["nodes"] if n.get("metadata", {}).get("kind") == "package"]
        self.assertEqual(len(pkg), 1)
        node = pkg[0]
        self.assertEqual(node["label"], "@acme/core")
        self.assertEqual(node["repo"], "corelib", "the package belongs to its provider")
        self.assertEqual(
            node["source_file"],
            "projects/core/package.json",
            "the node cites the manifest that declares the package",
        )
        self.assertEqual(node["metadata"]["consumers"], [{"repo": "app", "import_sites": 1}])

        imports = [e for e in graph["links"] if e.get("relation") == "imports_package"]
        self.assertEqual(len(imports), 1)
        self.assertEqual(
            imports[0]["source"], "app::0", "deterministic representative: lowest node id"
        )
        self.assertEqual(imports[0]["target"], node["id"])
        self.assertEqual(imports[0]["source_file"], "src/main.ts", "the edge cites the import")
        self.assertEqual(imports[0]["confidence"], "EXTRACTED")

        provided = [e for e in graph["links"] if e.get("relation") == "provided_by"]
        self.assertEqual(len(provided), 1)
        self.assertEqual(provided[0]["target"], "corelib::4")

    def test_rerun_replaces_the_layer_rather_than_duplicating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store(Path(tmp))
            self.assertEqual(packages.main(), 0)
            once = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
            self.assertEqual(packages.main(), 0)
            twice = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(once["nodes"]), len(twice["nodes"]))
        self.assertEqual(len(once["links"]), len(twice["links"]))

    def test_a_repo_owning_the_package_gets_no_self_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _store(root)
            # the provider also lists its own package (workspace convention)
            provider_manifest = root / "repositories" / "corelib" / "package.json"
            provider_manifest.write_text(
                json.dumps({"name": "corelib.workspace", "dependencies": {"@acme/core": "*"}}),
                encoding="utf-8",
            )
            self.assertEqual(packages.main(), 0)
            graph = json.loads(config.GRAPH_PATH.read_text(encoding="utf-8"))
        pkg = [n for n in graph["nodes"] if n.get("metadata", {}).get("kind") == "package"][0]
        self.assertEqual(
            [c["repo"] for c in pkg["metadata"]["consumers"]],
            ["app"],
            "self-consumption is not a cross-repository relationship",
        )

    def test_gz_artefact_is_rewritten_alongside(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store(Path(tmp))
            self.assertEqual(packages.main(), 0)
            gz = config.GRAPH_PATH.with_suffix(".json.gz")
            self.assertTrue(gz.exists(), "the committed form must never lag the working form")


if __name__ == "__main__":
    unittest.main()
