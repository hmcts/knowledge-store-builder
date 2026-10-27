"""A node `extract-ast` wrote must reach its tickets (#512).

Break this catches: `extract-ast` writes `source_file` as `repositories/<repo>/<path>`
and no `repo`, while the intent index is keyed `{repo: {repo-relative path: entry}}`
and the explorer reads it as `intent[node["repo"]][node["source_file"]]`. The two
key spaces share nothing, so every node the documented AST route produced loses
its tickets, silently: the graph, the index and every count are valid.

Measured end to end on a two-repository store with real `graphify`, `intent`,
`extract-ast`, `merge-layers` and `explorer`: the extract-ast route printed
"the file-to-ticket join matched nothing" (0 of 4 candidates); the per-repository
route plus `merge-graphs` printed "4 of 4 (100.0%) carry ticket evidence".

The node and index below are written by hand in the shapes those stages emit
(an AST node, then `merge-layers`' output for it; the index as `intent` writes it),
so the expected value does not come from the code under test. This test is
expected to FAIL until the join handles the store-relative form or `extract-ast`
emits the per-repository one.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knowledgestore import build_explorer, merge_layers  # noqa: E402

INDEX = {"repo-a": {"src/service.py": {"tickets": {"ABC-1": 1, "ABC-2": 1}}}}

AST_NODE = {
    "id": "repositories_repo_a_src_service_handle_a",
    "label": "handle_a()",
    "file_type": "code",
    "source_file": "repositories/repo-a/src/service.py",
    "source_location": "L1",
}


class ExtractAstTicketJoin(unittest.TestCase):
    def test_a_node_extract_ast_wrote_carries_its_files_tickets(self):
        merged, _ = merge_layers.merge(
            {"nodes": [AST_NODE], "edges": []}, {"nodes": [], "edges": []}
        )
        (node,) = merged["nodes"]
        self.assertEqual(node["id"], "repo-a::repositories_repo_a_src_service_handle_a")
        self.assertEqual(
            sorted(build_explorer.node_tickets(node, "function", INDEX)), ["ABC-1", "ABC-2"]
        )


if __name__ == "__main__":
    unittest.main()
