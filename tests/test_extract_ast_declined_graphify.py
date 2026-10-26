"""What the vanished-file guard reads from the real graphify, pinned against it (#492).

The guard in `extract-ast` names a file "declined by graphify" when graphify lists
it in `skipped_sources`, a key graphify added in 0.9.94. Two facts about the real
extractor carry that, and a stub cannot establish either:

- a JSON Schema file using `$schema` and `definitions` is declined: no node, and
  the file listed in `skipped_sources` rather than `failed_sources`. Under 0.9.75
  the same file yielded nodes, which is the transition the guard exists for;
- `skipped_sources` spells the path as the absolute path the extractor was
  handed. The guard keys its record by store-relative path, so a spelling it did
  not convert would never match and every declined file would read "VANISHED".

Skipped below 0.9.94, which has no such key. Run under a 0.9.94 environment these
exercise the real stage, with no stub anywhere.
"""

from __future__ import annotations

import importlib.metadata
import io
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout

import settings_isolation  # noqa: F401  (path setup and settings isolation)
from test_extract_ast_declared import _DeclaredStoreCase  # noqa: E402

from knowledgestore import config, extract_ast  # noqa: E402

SCHEMA = """{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Order",
  "type": "object",
  "properties": {
    "id": {"type": "string"},
    "lines": {"type": "array", "items": {"$ref": "#/definitions/OrderLine"}}
  },
  "definitions": {
    "OrderLine": {
      "type": "object",
      "properties": {"sku": {"type": "string"}, "quantity": {"type": "integer"}}
    }
  }
}
"""
CODE = "def total(lines):\n    return sum(line.quantity for line in lines)\n"


def _graphify_version() -> "tuple[int, ...] | None":
    try:
        text = importlib.metadata.version("graphifyy")
    except importlib.metadata.PackageNotFoundError:
        return None
    try:
        return tuple(int(part) for part in text.split(".")[:3])
    except ValueError:
        return None


_VERSION = _graphify_version()
needs_skipped_sources = unittest.skipUnless(
    _VERSION is not None and _VERSION >= (0, 9, 94),
    "needs graphify 0.9.94 or later, the first to report skipped_sources",
)


@needs_skipped_sources
class DeclinedByGraphifyTest(_DeclaredStoreCase, unittest.TestCase):
    def _store(self):
        clone = self._clone("orders-svc", "app.py", "schemas/order.schema.json")
        (clone / "app.py").write_text(CODE, encoding="utf-8")
        (clone / "schemas" / "order.schema.json").write_text(SCHEMA, encoding="utf-8")
        self._declare("orders-svc")
        return clone, self._detect([clone / "app.py", clone / "schemas" / "order.schema.json"])

    def test_graphify_declines_a_definitions_schema_and_names_it_by_absolute_path(self):
        """Catches graphify changing either fact the guard's wording depends on."""
        from graphify.extract import extract

        clone, _ = self._store()
        schema = clone / "schemas" / "order.schema.json"
        result = extract([clone / "app.py", schema], cache_root=self.root)
        self.assertEqual(result.get("skipped_sources"), [str(schema)])
        self.assertEqual(result.get("failed_sources"), [])
        self.assertNotIn(
            "repositories/orders-svc/schemas/order.schema.json",
            {node.get("source_file") for node in result["nodes"]},
        )

    def test_the_stage_names_the_declined_file_and_fails(self):
        """Catches the guard mis-spelling graphify's path: the file would read VANISHED.

        The record stands in for an earlier graphify that read the schema; the count
        is invented, and is what the refusal must quote back.
        """
        _, detect = self._store()
        record = self.root / "graphify-out" / ".graphify_ast_file_counts.json"
        record.write_text(
            json.dumps({"repositories/orders-svc/schemas/order.schema.json": 7}),
            encoding="utf-8",
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = extract_ast.main(["--detect", str(detect)])
        out, err = out.getvalue(), err.getvalue()
        self.assertEqual(code, 1, out + err)
        line = next(line for line in err.splitlines() if "order.schema.json" in line)
        self.assertIn("declined by graphify", line)
        self.assertIn("7 node(s) last run, 0 now", line)


try:
    import graphify.extract  # noqa: F401

    HAS_GRAPHIFY = True
except ImportError:  # pragma: no cover - depends on the environment
    HAS_GRAPHIFY = False


@unittest.skipUnless(HAS_GRAPHIFY, "needs graphify, the `ast` extra")
class SymlinkedStoreRootTest(_DeclaredStoreCase, unittest.TestCase):
    def setUp(self):
        """A store kept under a directory named `repositories`, and a symlink beside it.

        `store_paths` names `~/repositories/<store>` as an ordinary place to keep
        stores. There, a path spelled through the link and graphify's `../` spelling
        of the same file truncate at different `repositories/` markers.
        """
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        # Resolved, so the store's link is the only one in the path: the system temp
        # directory is itself a link on some platforms, and that case is the lossy one
        # pinned below rather than the recoverable one pinned here.
        parent = Path(holder.name).resolve() / "repositories"
        real = parent / "real-store"
        (real / "graphify-out").mkdir(parents=True)
        config.configure(root=real)
        self.root = config.ROOT
        self.link = parent / "link-store"
        self.link.symlink_to(self.root)

    def test_the_per_file_record_matches_content_set_keys_under_a_symlinked_root(self):
        """Catches the record keying on graphify's spelling instead of the content set's.

        Observed in graphify 0.9.80 and 0.9.94: with the store root reached through a
        symlink, a file spelled through the link comes back with `source_file`
        `../<link>/repositories/...`, relative to the resolved root. Keyed as it
        arrives, it matches no content-set path, every file counts 0, and a guard
        holding only zeros can never fire - which the stage refuses as an instrument
        mismatch. So a passing run that counted the file is the evidence the keys met.
        """
        clone = self._clone("orders-svc", "app.py")
        (clone / "app.py").write_text(CODE, encoding="utf-8")
        self._declare("orders-svc")
        detect = self._detect([self.link / "repositories" / "orders-svc" / "app.py"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = extract_ast.main(["--detect", str(detect)])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        record = json.loads(
            (self.root / "graphify-out" / ".graphify_ast_file_counts.json").read_text("utf-8")
        )
        self.assertEqual(len(record), 1, record)
        self.assertGreater(next(iter(record.values())), 0, "the join missed: the file counted 0")


_TEMP = Path(tempfile.gettempdir())


@unittest.skipUnless(
    HAS_GRAPHIFY and _TEMP.resolve() != _TEMP,
    "needs graphify, and a system temp directory reached through a symlink (as on macOS)",
)
class LossySpellingTest(_DeclaredStoreCase, unittest.TestCase):
    def test_a_spelling_graphify_cannot_round_trip_refuses_rather_than_record_zeros(self):
        """Catches the guard writing zeros it would then trust: the reviewer's failure.

        Observed in graphify 0.9.80 and 0.9.94: with the store's link sitting under a
        temp directory that is itself a link (`/var` to `/private/var`), graphify
        spells `source_file` as the bare file name, `app.py`. No path can be rebuilt
        from that. The stage must refuse as an instrument mismatch and write no
        record, because a record of zeros would read as a clean baseline.
        """
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        link = Path(holder.name) / "repositories" / "link-store"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.root)
        clone = self._clone("orders-svc", "app.py")
        (clone / "app.py").write_text(CODE, encoding="utf-8")
        self._declare("orders-svc")
        detect = self._detect([link / "repositories" / "orders-svc" / "app.py"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = extract_ast.main(["--detect", str(detect)])
        self.assertEqual(code, 1, out.getvalue() + err.getvalue())
        self.assertIn("instrument mismatch", err.getvalue())
        self.assertFalse((self.root / "graphify-out" / ".graphify_ast_file_counts.json").exists())


if __name__ == "__main__":
    unittest.main()
