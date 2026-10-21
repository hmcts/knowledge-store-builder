"""The CI pins of graphifyy and the `ast` extra's floor must not drift apart."""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


class GraphifyPin(unittest.TestCase):
    def test_every_ci_pin_is_one_version_at_or_above_the_extra_floor(self):
        """Catches a floor raised for a security fix while CI keeps testing the old version.

        CI pins graphifyy exactly so a run proves the same thing each time. The
        cost is that the pin can fall below the floor the extra promises users:
        CI would stay green on a version the package no longer supports.
        """
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        declared = re.search(r"^ast\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(declared, "pyproject.toml declares no `ast` extra")
        floor = re.search(r"graphifyy>=(\d+(?:\.\d+)*)", declared.group(1))
        self.assertIsNotNone(floor, "the `ast` extra must state a graphifyy floor")

        pins: dict[str, str] = {}
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            for match in re.finditer(
                r"graphifyy==(\d+(?:\.\d+)*)", workflow.read_text(encoding="utf-8")
            ):
                pins[workflow.name] = pins.get(workflow.name, "") + match.group(1) + " "
        self.assertTrue(pins, "CI must pin the graphifyy version it installs")
        versions = {v for joined in pins.values() for v in joined.split()}
        self.assertEqual(len(versions), 1, f"CI pins disagree: {pins}")
        self.assertGreaterEqual(_version(versions.pop()), _version(floor.group(1)))


if __name__ == "__main__":
    unittest.main()
