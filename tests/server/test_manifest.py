"""The version rules at start-up (spec, Start-up step 2), on the synthetic store.

Expected values are the literals in fixture_store/DESIGN.md section 9.
"""

import json
import tempfile
import unittest
from pathlib import Path

from fixture_store.build import MANIFEST, build
from knowledgestore.server import manifest
from knowledgestore.server.manifest import Refused


def edit_manifest(root: Path, change) -> None:
    path = root / MANIFEST
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(json.dumps(document), encoding="utf-8")


class ManifestTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()


class LoadTest(ManifestTestCase):
    def test_the_fixture_manifest_is_read_field_by_field(self):
        """Catches: a field read from the wrong key (store.id from the directory
        name, sources_as_of re-formatted), or a known layer reported as unread."""
        got = manifest.load(build(self.tmp / "s", index="none"))
        self.assertEqual((got.major, got.minor), (1, 0))
        self.assertEqual(got.store_id, "example-store")
        self.assertEqual(got.github_org, "example-org")
        self.assertEqual(got.library_version, "0.0.0+fixture")
        self.assertEqual(got.sources_as_of, "2026-10-15T23:30:00-02:00")
        self.assertRegex(got.digest, r"^[0-9a-f]{16}$")
        self.assertEqual(got.unread, [])
        self.assertIs(got.layers["graph"]["present"], True)
        self.assertIsNone(got.layers["deployments"]["present"])
        self.assertEqual(list(got.extra_layers), ["faq"])
        self.assertEqual(got.extra_layers["faq"]["kind"], "answers")

    def test_a_newer_minor_loads_and_names_its_unknown_layer_as_unread(self):
        """Catches: refusing a store only because a newer library added a layer,
        or loading it and saying nothing about the layer it cannot read."""
        extra = {"holograms": {"paths": ["knowledge/holograms/"], "present": True, "digest": None}}
        got = manifest.load(build(self.tmp / "s", index="none", manifest_minor=1, extra=extra))
        self.assertEqual(got.minor, 1)
        self.assertEqual(got.unread, ["holograms"])
        self.assertIn("holograms", got.layers)

    def test_unread_is_in_name_order(self):
        """Catches: unread following dict order, so two builds of one store list
        the same layers differently (deterministic output)."""
        root = build(self.tmp / "s", index="none", manifest_minor=1)
        absent = {"paths": [], "present": True, "digest": None}
        # Written in this order by the edit, so the file itself is out of name order.
        edit_manifest(root, lambda d: d["layers"].update(zebra=absent, aardvark=absent))
        got = manifest.load(root)
        self.assertEqual(got.unread, ["aardvark", "zebra"])

    def test_an_absent_unknown_layer_is_not_unread_but_an_unknown_state_is(self):
        """Catches: reporting a layer as present-but-unread when the manifest says it
        is absent (present false), or dropping one whose state is unknown (null)."""
        root = build(self.tmp / "s", index="none", manifest_minor=1)
        entry = lambda present: {"paths": [], "present": present, "digest": None}  # noqa: E731
        edit_manifest(
            root,
            lambda d: d["layers"].update(gone=entry(False), here=entry(True), unsure=entry(None)),
        )
        self.assertEqual(manifest.load(root).unread, ["here", "unsure"])


class RefusalTest(ManifestTestCase):
    def test_major_two_is_refused_naming_what_was_found_and_what_is_supported(self):
        """Catches: reading a format whose meaning may have changed, or refusing
        without saying which version was found and which are supported."""
        root = build(self.tmp / "s", index="none")
        edit_manifest(root, lambda d: d["format"].update(major=2))
        with self.assertRaises(Refused) as caught:
            manifest.load(root)
        self.assertEqual(caught.exception.supported, (1,))
        self.assertIn("major version 2", str(caught.exception))
        self.assertIn("supported format major versions: 1", str(caught.exception))

    def test_major_zero_is_refused_too(self):
        """Catches: a `major >= 1` or `major <= 1` test in place of membership in
        SUPPORTED_MAJORS."""
        root = build(self.tmp / "s", index="none")
        edit_manifest(root, lambda d: d["format"].update(major=0))
        with self.assertRaises(Refused):
            manifest.load(root)

    def test_a_missing_manifest_is_refused_naming_the_path(self):
        """Catches: a bare FileNotFoundError traceback, or a store that starts
        with no manifest and so cannot say what it holds."""
        root = build(self.tmp / "s", index="none")
        (root / MANIFEST).unlink()
        with self.assertRaises(Refused) as caught:
            manifest.load(root)
        self.assertIn("knowledge/store-manifest.json", str(caught.exception))
        self.assertIn(str(root), str(caught.exception))

    def test_a_manifest_that_is_not_json_is_refused(self):
        """Catches: a half-written manifest crashing start-up with a JSON
        traceback rather than a refusal that says what was found."""
        root = build(self.tmp / "s", index="none")
        (root / MANIFEST).write_text("{not json", encoding="utf-8")
        with self.assertRaises(Refused) as caught:
            manifest.load(root)
        self.assertIn("cannot be read as JSON", str(caught.exception))

    def test_a_manifest_without_a_format_is_refused(self):
        """Catches: a manifest with no format.major being treated as a supported
        version by default."""
        root = build(self.tmp / "s", index="none")
        edit_manifest(root, lambda d: d.pop("format"))
        with self.assertRaises(Refused):
            manifest.load(root)


if __name__ == "__main__":
    unittest.main()
