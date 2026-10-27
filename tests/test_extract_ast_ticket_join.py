"""A node `extract-ast` wrote must reach its tickets (#512).

Break this catches: `extract-ast` handing the extractor the store as its root, so a node
came out as `repositories/<repo>/<path>` with no `repo` while the intent index is keyed
`{repo: {repo-relative path: entry}}` and the explorer reads it as
`intent[node["repo"]][node["source_file"]]`. The two key spaces share nothing, so every
node the documented AST route produced lost its tickets, silently: the graph, the index
and every count were valid.

Measured end to end on a two-repository store with real `graphify`, `intent`,
`extract-ast`, `merge-layers` and `explorer`: that route printed "the file-to-ticket join
matched nothing" (0 of 4 candidates); the per-repository route plus `merge-graphs`
printed "4 of 4 (100.0%) carry ticket evidence".

The extractor is stubbed at its boundary, but spells `source_file` and ids the way
graphify does: relative to the `cache_root` it is given, so the root the stage chooses
decides the result. The index is written by hand in the shape `intent` emits, and the
expected values are literals, not computed by the code under test.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import settings_isolation  # noqa: F401  (path setup and settings isolation)
from test_extract_ast_declared import _DeclaredStoreCase  # noqa: E402

from knowledgestore import build_explorer, merge_layers  # noqa: E402

INDEX = {"repo-a": {"src/service.py": {"tickets": {"ABC-1": 1, "ABC-2": 1}}}}


class _RootRelative:
    """One function node per file, spelled relative to `cache_root` as graphify spells it."""

    def __call__(self, files, cache_root=None):
        nodes = []
        for path in files:
            relative = Path(path).relative_to(cache_root).as_posix()
            stem = relative.removesuffix(".py").replace("/", "_")
            nodes.append(
                {
                    "id": f"{stem}_handle",
                    "label": "handle()",
                    "file_type": "code",
                    "source_file": relative,
                }
            )
        return {"nodes": nodes, "edges": [], "failed_sources": []}


class ExtractAstTicketJoin(_DeclaredStoreCase, unittest.TestCase):
    def test_a_node_extract_ast_wrote_carries_its_files_tickets(self):
        clone = self._clone("repo-a", "src/service.py")
        self._declare("repo-a")
        detect = self._detect([clone / "src" / "service.py"])
        code, out, err = self._run(_RootRelative(), ["--detect", str(detect)])
        self.assertEqual(code, 0, out + err)
        merged, _ = merge_layers.merge(self._layer(), {"nodes": [], "edges": []})
        (node,) = merged["nodes"]
        self.assertEqual(node["id"], "repo-a::src_service_handle")
        self.assertEqual(node["source_file"], "src/service.py")
        self.assertEqual(node["repo"], "repo-a")
        self.assertEqual(
            sorted(build_explorer.node_tickets(node, "function", INDEX)), ["ABC-1", "ABC-2"]
        )


if __name__ == "__main__":
    unittest.main()
