"""Writes through `io` must not accept a path that climbs out of its directory.

`io.write_json` and `io.gzip_text` are reached with paths assembled from `--root`
and other CLI arguments, so whatever builds those arguments decides where the
process writes. Reported by SonarCloud as `pythonsecurity:S8707` on
`write_json`'s `write_text` call.

What each test protects is named on it. Note what these deliberately do *not*
assert: that writes are confined to the store root. They are not, by design —
`config.configure()` sets each output path independently, so an output directory
outside the root is supported. A root-confinement guard was written first and
failed 48 tests; a guard allowing every directory the configuration declares
still failed 4, because this module's own unit tests write to bare temporary
directories. Those failures are the evidence that confinement belongs at the
stage boundary, where the boundary is known, and not in a low-level writer.
"""

from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledgestore import io as pio  # noqa: E402


class WriteTargetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_json_refuses_an_upward_path(self):
        """Breaks if a `..` component reaches the filesystem through `write_json`.

        The escape this rejects: a caller-assembled path that leaves the directory
        it names. Asserted by the file not existing afterwards as well as by the
        raise, because a guard that raises after writing has not prevented
        anything.
        """
        # Climbs out of `nested/` but lands back inside this test's own
        # directory. An earlier version escaped into the shared system temp
        # parent and asserted the file was absent there - which failed once the
        # mutation run, with the guard disabled, genuinely wrote it. Asserting
        # on a directory other tests share is order-dependent by construction.
        escaping = self.dir / "nested" / ".." / "escaped.json"

        with self.assertRaises(ValueError) as caught:
            pio.write_json(escaping, {"k": 1})

        self.assertIn("traverses upward", str(caught.exception))
        self.assertFalse((self.dir / "escaped.json").exists())

    def test_gzip_text_refuses_an_upward_path(self):
        """Breaks if the gzip writer is left unguarded when the JSON one is fixed.

        Two writers, one guard: the half-wired case is what makes a check read as
        done. `write_gzip_json` goes through `gzip_text`, so this covers both.
        """
        escaping = self.dir / "nested" / ".." / "escaped.json.gz"

        with self.assertRaises(ValueError):
            pio.write_gzip_json(escaping, {"k": 1})

        self.assertFalse((self.dir / "escaped.json.gz").exists())

    def test_the_check_is_lexical_not_resolved(self):
        """Breaks if the guard resolves the path before inspecting it.

        `realpath` collapses `..`, so a resolved path never contains one and the
        check would pass everything while looking correct — a guard that cannot
        fail. This asserts the rejection happens even where the collapsed target
        would have been an innocuous location inside the same directory.
        """
        # Collapses to `self.dir / "inside.json"`, which is allowed as a plain path.
        round_trip = self.dir / "sub" / ".." / "inside.json"

        with self.assertRaises(ValueError):
            pio.write_json(round_trip, {"k": 1})

        pio.write_json(self.dir / "inside.json", {"k": 1})
        self.assertEqual(json.loads((self.dir / "inside.json").read_text()), {"k": 1})

    def test_ordinary_paths_still_write_both_formats(self):
        """Breaks if the guard rejects legitimate targets.

        A security check that blocks normal use gets removed rather than fixed, so
        this pins the case that must keep working — including a nested directory
        the writer has to create.
        """
        plain = self.dir / "nested" / "out.json"
        pio.write_json(plain, {"a": [1, 2]})
        self.assertEqual(json.loads(plain.read_text(encoding="utf-8")), {"a": [1, 2]})

        packed = self.dir / "nested" / "out.json.gz"
        pio.write_gzip_json(packed, {"b": 3})
        with gzip.open(packed, "rt", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"b": 3})

    def test_the_guard_reports_which_path_it_refused(self):
        """Breaks if the message omits the path.

        An operator hitting this needs to know which of several configured paths
        was rejected; a bare refusal sends them reading code.
        """
        escaping = self.dir / "nested" / ".." / "escaped.json"

        with self.assertRaises(ValueError) as caught:
            pio.write_json(escaping, {})

        self.assertIn("escaped.json", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
