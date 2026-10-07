"""Every gzip artefact this library writes must be byte-identical between runs.

`io.gzip_text` exists because Python's own writer embeds the current time and
the output filename in the header, so a rebuild rewrote committed artefacts
whose content had not changed. Its docstring says so. The guarantee is only as
good as its last caller, and one caller had stopped using it:
`import_ticket_titles` wrote `knowledge/intent/ticket-titles.json.gz` through
`gzip.open(..., "wt")`, so that file churned on every run in every consuming
store while `docs/how-it-works.md` claimed every writer was deterministic.

The break this catches has two halves.

1. **The bytes.** Writing the same content twice, a second apart, must produce
   the same bytes. That is the product guarantee, and it is asserted against the
   real helper rather than against a description of it.
2. **The callers.** A behavioural test on the helper cannot see a module that
   bypasses it, which is exactly how this one was missed. So the source is read
   by AST for a gzip write that does not go through `io.gzip_text`, and the one
   legitimate site - `io` itself, which implements the helper - is named rather
   than inferred.

The second half asserts its own discriminating power in the same run, over
forged module sources rather than the tree: a module that writes through
`gzip.open`, one that only reads through it, one that writes through the helper,
and one that touches neither. Without the last three the detector could be
answering "does this module mention gzip", which is a neighbouring question with
the same answer today.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from knowledgestore import io as kio  # noqa: E402

SRC = ROOT / "src" / "knowledgestore"

# `io` implements the deterministic writer, so it is the one module allowed to
# reach the gzip machinery directly. Named rather than inferred: a second module
# acquiring that privilege should have to be argued for here.
IMPLEMENTS_THE_WRITER = frozenset({"io"})

WRITE_MODES = ("w", "a", "x")


def _is_gzip_write(node: ast.AST) -> bool:
    """A `gzip.open` call in a write mode, or a bare `gzip.GzipFile`.

    A mode that is not a literal is treated as a write: an unreadable call
    should fail loudly and be classified by hand rather than pass silently.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return False
    if func.value.id != "gzip":
        return False
    if func.attr == "GzipFile":
        return True
    if func.attr != "open":
        return False
    mode = next((a for a in node.args[1:2]), None) or next(
        (k.value for k in node.keywords if k.arg == "mode"), None
    )
    if mode is None:
        return False  # gzip.open defaults to "rb"
    if not isinstance(mode, ast.Constant) or not isinstance(mode.value, str):
        return True
    return mode.value[:1] in WRITE_MODES


def gzip_writers(source: str) -> bool:
    """True when this module source writes gzip without the helper."""
    return any(_is_gzip_write(node) for node in ast.walk(ast.parse(source)))


class TheBytesDoNotChangeBetweenRuns(unittest.TestCase):
    """Half one: the guarantee itself, asserted on the real helper."""

    def test_the_helper_writes_the_same_bytes_a_second_apart(self):
        payload = '{"TICKET-1": "a title"}'
        directory = Path(tempfile.mkdtemp())
        digests = []
        for name in ("first.json.gz", "second.json.gz"):
            with kio.gzip_text(directory / name) as out:
                out.write(payload)
            digests.append(hashlib.sha256((directory / name).read_bytes()).hexdigest())
            time.sleep(1.1)  # cross a whole second, which is the header's resolution
        self.assertEqual(digests[0], digests[1])

    def test_the_control_shows_a_second_is_long_enough_to_matter(self):
        """Without this the test above could pass because nothing varies at all."""
        payload = '{"TICKET-1": "a title"}'
        directory = Path(tempfile.mkdtemp())
        digests = []
        for name in ("first.json.gz", "second.json.gz"):
            with gzip.open(directory / name, "wt", encoding="utf-8") as out:
                out.write(payload)
            digests.append(hashlib.sha256((directory / name).read_bytes()).hexdigest())
            time.sleep(1.1)
        self.assertNotEqual(digests[0], digests[1])


class NoModuleBypassesTheHelper(unittest.TestCase):
    """Half two: the callers, which the behavioural test cannot see."""

    def test_no_module_writes_gzip_without_the_helper(self):
        offenders = sorted(
            path.stem
            for path in SRC.glob("*.py")
            if path.stem not in IMPLEMENTS_THE_WRITER
            and gzip_writers(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(
            offenders,
            [],
            f"{offenders} write gzip directly; use io.gzip_text so the bytes are stable",
        )

    def test_the_scan_reads_the_tree_it_claims_to(self):
        """The precondition, so the assertion above cannot pass vacuously."""
        self.assertGreater(len(list(SRC.glob("*.py"))), 20)


class TheScanCanStillTell(unittest.TestCase):
    """Its discriminating power, over forged sources rather than the tree."""

    def test_it_flags_a_write_through_gzip_open(self):
        self.assertTrue(gzip_writers('import gzip\nwith gzip.open(p, "wt") as f: f.write(x)\n'))

    def test_it_flags_a_bare_gzipfile(self):
        self.assertTrue(gzip_writers("import gzip\ng = gzip.GzipFile(fileobj=raw, mode='wb')\n"))

    def test_it_ignores_a_read(self):
        self.assertFalse(gzip_writers('import gzip\nwith gzip.open(p, "rt") as f: f.read()\n'))

    def test_it_ignores_a_default_mode_open_which_reads(self):
        self.assertFalse(gzip_writers("import gzip\nwith gzip.open(p) as f: f.read()\n"))

    def test_it_ignores_a_write_through_the_helper(self):
        self.assertFalse(gzip_writers("from . import io\nwith io.gzip_text(p) as f: f.write(x)\n"))

    def test_it_ignores_a_module_touching_neither(self):
        self.assertFalse(gzip_writers("import json\njson.dump(x, open(p, 'w'))\n"))


if __name__ == "__main__":
    unittest.main()
