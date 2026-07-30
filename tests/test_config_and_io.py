"""Tests for the shared pipeline modules (config, io)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


from knowledgestore import config  # noqa: E402
from knowledgestore import io as pio  # noqa: E402


ROOTED = (
    "GRAPH_PATH",
    "LABELS_PATH",
    "EXPLORER_PATH",
    "INTENT_INDEX_PATH",
    "TICKET_DESCRIPTIONS_PATH",
    "TICKET_TITLES_PATH",
    "SUMMARIES_PATH",
    "SUMMARIES_INPUT_PATH",
    "SYNONYMS_PATH",
    "PROVENANCE_PATH",
    "TOPICS_INPUT_PATH",
    "TOPICS_BRIEFS_PATH",
    "TOPICS_DOCS_DIR",
    "HISTORY_DIR",
    "CONTEXT_PATH",
    "MANIFEST_PATH",
    "FILTERS_PATH",
    "REPOSITORIES_CONFIG",
    "REPOSITORIES_DIR",
    "TOPICS_CONFIG_PATH",
)


class ConfigTest(unittest.TestCase):
    def test_every_path_sits_under_root(self):
        for name in ROOTED:
            self.assertTrue(str(getattr(config, name)).startswith(str(config.ROOT)), name)

    def test_configure_root_repoints_every_derived_path(self):
        original = config.ROOT
        self.addCleanup(config.configure, original)
        with tempfile.TemporaryDirectory() as tmp:
            config.configure(root=tmp)
            for name in ROOTED:
                self.assertTrue(
                    str(getattr(config, name)).startswith(str(Path(tmp).resolve())), name
                )

    def test_configure_overrides_a_single_setting(self):
        original = config.GITHUB_ORG
        self.addCleanup(config.configure, None, GITHUB_ORG=original)
        config.configure(GITHUB_ORG="otherorg")
        self.assertEqual(config.GITHUB_ORG, "otherorg")

    def test_configure_rejects_unknown_settings(self):
        with self.assertRaises(KeyError):
            config.configure(NOT_A_SETTING=1)

    def test_ticket_pattern_matches_and_rejects(self):
        self.assertEqual(
            config.TICKET_PATTERN.findall("DD-1: fix CRC-12016 x"), ["DD-1", "CRC-12016"]
        )
        self.assertEqual(config.TICKET_PATTERN.findall("no tickets here 12-34"), [])


class IoTest(unittest.TestCase):
    def test_json_round_trip_and_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "x.json"
            self.assertEqual(pio.read_json(path, default={"d": 1}), {"d": 1})
            pio.write_json(path, {"a": [1, 2]})
            self.assertEqual(pio.read_json(path), {"a": [1, 2]})

    def test_gzip_json_round_trip_and_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "x.json.gz"
            self.assertIsNone(pio.read_gzip_json(path))
            pio.write_gzip_json(path, {"k": "v"})
            self.assertEqual(pio.read_gzip_json(path), {"k": "v"})

    def test_load_labels_empty_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pio.load_labels(Path(tmp) / "missing.json"), {})


if __name__ == "__main__":
    unittest.main()
