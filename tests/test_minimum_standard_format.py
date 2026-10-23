"""The minimum-standard layer's shared contract: its stamp, its points and its states.

Every stage and reader of the layer takes these from one module, so a point renamed
or a state added there is a change to every store's committed evidence.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import config, minimum_standard, store_manifest  # noqa: E402


class TheStamp(unittest.TestCase):
    def test_the_stamp_names_the_layer_and_version(self):
        """Breaks if the stamp's layer name or version drifts from the declared
        `minimum_standard` 1.0: readers recognise the layer by exactly this."""
        self.assertEqual(
            minimum_standard.format_stamp(),
            {"layer": "minimum_standard", "major": 1, "minor": 0},
        )

    def test_the_points_are_the_seven_in_order(self):
        """Breaks if a point is renamed, dropped or reordered: every committed
        record and every reader keys on these names."""
        self.assertEqual(
            minimum_standard.POINTS,
            (
                "owner",
                "security_contact",
                "no_secrets",
                "secure_by_design",
                "hygiene",
                "patching",
                "unmaintained",
            ),
        )
        self.assertEqual(minimum_standard.STATES, ("present", "absent", "not_determinable"))

    def test_format_problem_reports_a_missing_stamp_another_layer_and_another_major(self):
        """Breaks if a body this release cannot read is accepted, or one it can read
        is refused."""
        good = {"format": minimum_standard.format_stamp()}
        self.assertEqual(minimum_standard.format_problem(good), "")
        for body in (
            {},
            [],
            {"format": "minimum_standard"},
            {"format": {"layer": "relationships", "major": 1}},
        ):
            with self.subTest(body=body):
                self.assertIn("minimum_standard", minimum_standard.format_problem(body))
        for major in (2, True):
            with self.subTest(major=major):
                body = {"format": {"layer": "minimum_standard", "major": major}}
                self.assertIn("major", minimum_standard.format_problem(body))


class APoint(unittest.TestCase):
    def test_no_secrets_can_never_be_present(self):
        """Breaks if a pattern scan can claim `no_secrets` present: a scan cannot
        show that there are none, and that claim is the layer's worst failure."""
        with self.assertRaises(ValueError):
            minimum_standard.point("no_secrets", "present")
        for state in ("absent", "not_determinable"):
            self.assertEqual(minimum_standard.point("no_secrets", state)["state"], state)

    def test_an_unknown_state_is_refused(self):
        """Breaks if a fourth state, or a typo, is written into the layer: readers
        branch on exactly three."""
        with self.assertRaises(ValueError):
            minimum_standard.point("owner", "unknown")

    def test_an_unknown_key_is_refused(self):
        """Breaks if a point outside the seven is written under the layer's stamp."""
        with self.assertRaises(ValueError):
            minimum_standard.point("budget", "present")

    def test_a_point_carries_its_evidence_and_reason(self):
        """Breaks if the evidence or reason is dropped or shared between points."""
        first = minimum_standard.point("owner", "present", evidence=[{"path": "CODEOWNERS"}])
        second = minimum_standard.point("owner", "not_determinable", reason="no access")
        self.assertEqual(first["evidence"], [{"path": "CODEOWNERS"}])
        self.assertEqual((second["evidence"], second["reason"]), ([], "no access"))
        self.assertNotIn("reason", first)


class TheManifest(SettingsIsolated):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def layer(self, summary: str | None) -> dict:
        directory = self.root / "knowledge" / "minimum-standard"
        (directory / "by-repo").mkdir(parents=True)
        (directory / "by-repo" / "repo-a.json").write_text("{}", encoding="utf-8")
        if summary is not None:
            (directory / "summary.json").write_text(summary, encoding="utf-8")
        config.configure(STORE_ID="example-store")
        return store_manifest.compute(self.root)["layers"]["minimum_standard"]

    def test_the_manifest_lists_the_layer_and_recognises_it_by_stamp(self):
        """Breaks if the layer is not listed, or a stamped summary is not recognised,
        or an unstamped one is called recognised."""
        stamped = json.dumps({"format": minimum_standard.format_stamp()})
        layer = self.layer(stamped)
        self.assertEqual((layer["present"], layer["recognised"]), (True, True))
        self.assertEqual(layer["paths"], ["knowledge/minimum-standard/"])

    def test_an_unstamped_or_foreign_summary_is_present_and_not_recognised(self):
        """Breaks if a summary without this layer's stamp at a known major reads as
        recognised: a reader would trust a shape nothing vouches for."""
        for summary in (
            None,
            "{}",
            "<<<<<<< not json",
            json.dumps({"format": {"layer": "relationships", "major": 1}}),
            json.dumps({"format": {"layer": "minimum_standard", "major": 2}}),
            json.dumps({"format": {"layer": "minimum_standard", "major": True}}),
        ):
            with self.subTest(summary=summary), tempfile.TemporaryDirectory() as raw:
                self.root = Path(raw)
                layer = self.layer(summary)
                self.assertEqual((layer["present"], layer["recognised"]), (True, False))

    def test_an_absent_layer_has_nothing_to_recognise(self):
        """Breaks if a store without the layer reports it unrecognised: false says a
        layer was measured and refused, and there is none."""
        config.configure(STORE_ID="example-store")
        layer = store_manifest.compute(self.root)["layers"]["minimum_standard"]
        self.assertEqual((layer["present"], layer["recognised"]), (False, None))


if __name__ == "__main__":
    unittest.main()
