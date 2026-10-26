"""The json-schema stage against a graph graphify really built and merged.

Every other test of the stage writes its graph by hand. This one runs
`graphify update .` in two clones and `graphify merge-graphs` over them, so the
node ids, the `repo` tags and the keyword nodes the stage replaces are the ones
graphify produces rather than ones this repository imagined (CLAUDE.md 2.4).

Skipped without a `graphify` executable on PATH.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import json_schema_fixtures as fixtures  # noqa: E402
from settings_isolation import SettingsIsolated  # noqa: E402

from knowledgestore import config  # noqa: E402
from knowledgestore import extract_json_schema as layer  # noqa: E402

GRAPHIFY = shutil.which("graphify")

WIDGET_FILE = "schemas/widget-created.json"
PLAIN_FILE = "schemas/plain.json"
OWNER_SOURCE = f"widgets::jsonschema:{WIDGET_FILE}#/properties/owner"
PERSON_TARGET = "common::jsonschema:person.json#"


@unittest.skipUnless(GRAPHIFY, "needs the graphify executable on PATH")
class JsonSchemaLayerEndToEndTest(SettingsIsolated):
    def _graphify(self, *args: str, cwd: Path) -> None:
        assert GRAPHIFY
        env = {**os.environ, "GRAPHIFY_NO_AUTO_REFRESH": "1", "PYTHONHASHSEED": "0"}
        done = subprocess.run(
            [GRAPHIFY, *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
        )
        if done.returncode != 0:
            self.fail(
                f"graphify {' '.join(args)} exited {done.returncode}:\n{done.stdout}\n{done.stderr}"
            )

    def _clone(self, name: str, files: dict[str, str]) -> Path:
        clone = self.store / "repositories" / name
        for rel, text in files.items():
            target = clone / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(clone)], check=True)
        self._graphify("update", ".", cwd=clone)
        return clone / "graphify-out" / "graph.json"

    def test_the_layer_replaces_keyword_nodes_and_links_across_repositories(self):
        """Catches the layer disagreeing with graphify's real output.

        Breaks if the stage leaves graphify's keyword nodes beside its own, tags a
        node with a repository `merge-graphs` did not write, or fails to link a
        draft-04 `id` reference across clones.
        """
        from graphify.detect import detect

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name).resolve()
        widgets = self._clone(
            "widgets", {WIDGET_FILE: fixtures.WIDGET_CREATED, PLAIN_FILE: fixtures.PLAIN_DRAFT04}
        )
        common = self._clone("common", {"person.json": fixtures.PERSON})

        out = self.store / "graphify-out"
        out.mkdir()
        (out / ".graphify_detect.json").write_text(
            json.dumps(detect(self.store, gitignore=False)), encoding="utf-8"
        )
        self._graphify(
            "merge-graphs", str(widgets), str(common), "--out", str(out / "graph.json"),
            cwd=self.store,
        )  # fmt: skip

        graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
        # Read before the stage runs: it removes these schemas' ast nodes, and their
        # repo tags are what merge-graphs wrote.
        graphify_repos = {n["repo"] for n in graph["nodes"] if n.get("_origin") == "ast"}
        before = [
            n for n in graph["nodes"]
            if n.get("_origin") == "ast" and n.get("source_file", "").endswith(PLAIN_FILE)
        ]  # fmt: skip
        self.assertTrue(
            before,
            "graphify built no keyword nodes for the plain schema; the removal check below would be vacuous",
        )

        config.configure(root=str(self.store))
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = layer.main([])
        self.assertEqual(code, 0, printed.getvalue())

        graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
        nodes = graph["nodes"]
        edges = graph.get("links", graph.get("edges", []))
        owner_edges = [
            e for e in edges
            if e["source"] == OWNER_SOURCE and e["target"] == PERSON_TARGET
        ]  # fmt: skip
        self.assertEqual([e["relation"] for e in owner_edges], ["references"])

        leftovers = [
            n["id"] for n in nodes
            if n.get("_origin") == "ast"
            and any(
                n.get("source_file", "").endswith(rel) for rel in (WIDGET_FILE, PLAIN_FILE)
            )
        ]  # fmt: skip
        self.assertEqual(leftovers, [])

        layer_nodes = [n for n in nodes if n.get("_origin") == "json-schema"]
        self.assertTrue(layer_nodes)
        self.assertLessEqual({n["repo"] for n in layer_nodes}, graphify_repos)


if __name__ == "__main__":
    unittest.main()
