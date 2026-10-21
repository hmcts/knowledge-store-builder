"""The `ast` extra must carry the grammars graphify needs for HCL and SQL.

Without `tree-sitter-hcl` and `tree-sitter-sql`, graphify reads `.tf`, `.tfvars`,
`.hcl` and `.sql` files as nothing: no error, no node, and a layer that merges
cleanly and is short. These tests run the real stage over a real parser, so a
grammar that is lost from the extra - or from the environment - fails here
instead of shipping an empty layer.
"""

from __future__ import annotations

import unittest

import settings_isolation  # noqa: F401  (path setup and settings isolation)
from test_extract_ast import _StoreCase  # noqa: E402

from knowledgestore import extract_ast  # noqa: E402

try:
    import graphify.extract  # noqa: F401
    import tree_sitter  # noqa: F401

    HAS_GRAPHIFY = True
except ImportError:  # pragma: no cover - depends on the environment
    HAS_GRAPHIFY = False

needs_graphify = unittest.skipUnless(
    HAS_GRAPHIFY,
    "needs graphify, the `ast` extra (CI installs it with its terraform and sql extras)",
)

TERRAFORM = 'resource "aws_s3_bucket" "logs" {\n  bucket = "logs"\n}\n'
SQL = "CREATE TABLE accounts (id INT PRIMARY KEY, name TEXT);\n"


@needs_graphify
class GrammarsTest(_StoreCase, unittest.TestCase):
    def _extract(self, *names: str) -> set[str]:
        """Run the real stage over one repository holding `names`; the layer's source files."""
        files = []
        for name in names:
            path = self._repo_file("repo-a", name)
            path.write_text(TERRAFORM if name.endswith(".tf") else SQL, encoding="utf-8")
            files.append(path)
        detect = self._detect(files)
        code, out, err = self._run_real(["--detect", str(detect)])
        self.assertEqual(code, 0, out + err)
        return {str(node.get("source_file")) for node in self._layer()["nodes"]}

    def _run_real(self, argv):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = extract_ast.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_terraform_yields_nodes(self):
        """Catches `tree-sitter-hcl` missing: a `.tf` file then extracts to no nodes."""
        sources = self._extract("main.tf")
        self.assertIn("repositories/repo-a/main.tf", sources)

    def test_sql_yields_nodes(self):
        """Catches `tree-sitter-sql` missing: a `.sql` file then extracts to no nodes."""
        sources = self._extract("schema.sql")
        self.assertIn("repositories/repo-a/schema.sql", sources)


if __name__ == "__main__":
    unittest.main()
