"""`extract-ast` writes the shape the per-repository route writes (#512).

Break this catches: `extract-ast` and `graphify update .` inside each clone followed by
`graphify merge-graphs` are two routes to one graph, and every consumer reads the second
one's shape - `<repo>::<id>` node ids, a repository-relative `source_file`, `repo` set.
A change that moves `extract-ast` off that shape (store-rooted ids, a store-relative
`source_file`, `repo` unset) breaks the file-to-ticket join for every node and makes
switching between the routes a re-keying of the whole graph. No stub can say what the
other route writes, so both run for real on an invented two-repository store and the
code nodes are compared: ids, `source_file`, `repo`, and the edge endpoints.

Skipped without the `graphify` command and the `ast` extra.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout

import settings_isolation  # noqa: F401  (path setup and settings isolation)
from test_extract_ast_declared import _DeclaredStoreCase  # noqa: E402

from knowledgestore import extract_ast  # noqa: E402

try:
    import graphify.extract  # noqa: F401
    import tree_sitter  # noqa: F401

    HAS_GRAPHIFY = shutil.which("graphify") is not None
except ImportError:  # pragma: no cover - depends on the environment
    HAS_GRAPHIFY = False

SERVICE = """from src.util import helper


class Service:
    def handle_{tag}(self):
        return helper()
"""
UTIL = "def helper():\n    return 1\n"
CHECK = "from src.service import Service\n\n\ndef test_it():\n    Service().handle_a()\n"
RENDER = "export function render() { return 1 }\n"


def _code(graph: dict) -> "tuple[dict, set]":
    """Code nodes by id, as (source_file, repo), and the endpoint pairs of the edges between them.

    Relations are not compared: `update` collapses edges sharing endpoints to the strongest
    relation, which is graphify's graph build and not the shape under test."""
    nodes = {
        n["id"]: (n.get("source_file"), n.get("repo"))
        for n in graph["nodes"]
        if n.get("file_type") == "code"
    }
    edges = {
        (e["source"], e["target"])
        for e in graph.get("links", graph.get("edges", []))
        if e["source"] in nodes and e["target"] in nodes
    }
    return nodes, edges


@unittest.skipUnless(HAS_GRAPHIFY, "needs the graphify command and the `ast` extra")
class ExtractAstParityTest(_DeclaredStoreCase, unittest.TestCase):
    def _store(self):
        files = {
            "repo-a": {
                "src/service.py": SERVICE.format(tag="a"),
                "src/util.py": UTIL,
                "tests/test_service.py": CHECK,
                "web/app.js": RENDER,
            },
            "repo-b": {"src/service.py": SERVICE.format(tag="b"), "src/util.py": UTIL},
        }
        detected = []
        for name, content in files.items():
            clone = self._clone(name, *content)
            for relative, text in content.items():
                (clone / relative).write_text(text, encoding="utf-8")
                detected.append(clone / relative)
        self._declare(*files)
        return sorted(detected)

    def _per_repository_route(self):
        graphs = []
        for name in ("repo-a", "repo-b"):
            clone = self.root / "repositories" / name
            subprocess.run(["graphify", "update", "."], cwd=clone, check=True, capture_output=True)
            graphs.append(str(clone / "graphify-out" / "graph.json"))
        merged = self.root / "merged-per-repository.json"
        subprocess.run(
            ["graphify", "merge-graphs", *graphs, "--out", str(merged)],
            check=True,
            capture_output=True,
        )
        return json.loads(merged.read_text(encoding="utf-8"))

    def test_extract_ast_matches_the_per_repository_route_on_code_nodes(self):
        detected = self._store()
        per_repository = self._per_repository_route()
        detect = self._detect(detected)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = extract_ast.main(["--detect", str(detect)])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        ours_nodes, ours_edges = _code(self._layer())
        theirs_nodes, theirs_edges = _code(per_repository)

        self.assertEqual(len(theirs_nodes), 14, "the per-repository route changed under us")
        self.assertEqual(ours_nodes, theirs_nodes)
        self.assertEqual(ours_edges, theirs_edges)
        self.assertEqual(ours_nodes["repo-a::src_service_service"], ("src/service.py", "repo-a"))


if __name__ == "__main__":
    unittest.main()
