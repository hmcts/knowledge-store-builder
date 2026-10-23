"""Tests for knowledgestore/json_schema.py - parsing with line numbers and recognition."""

from __future__ import annotations

import json
import unittest

import json_schema_fixtures as fixtures
from knowledgestore import json_schema


class LoadWithLinesTest(unittest.TestCase):
    def test_lines_are_those_of_each_objects_opening_brace(self):
        """Breaks if positions are taken from the key, the end, or off by one."""
        value, lines = json_schema.load_with_lines(fixtures.WIDGET_CREATED)
        self.assertEqual(lines[id(value)], 1)
        self.assertEqual(lines[id(value["properties"]["widgetId"])], 7)
        self.assertEqual(lines[id(value["definitions"]["Part"])], 13)
        self.assertEqual(lines[id(value["definitions"]["Part"]["properties"]["sku"])], 15)

    def test_value_equals_json_loads(self):
        """Breaks if the line-tracking decoder changes what is decoded."""
        value, _ = json_schema.load_with_lines(fixtures.WIDGET_CREATED)
        self.assertEqual(value, json.loads(fixtures.WIDGET_CREATED))

    def test_malformed_json_raises_schema_read_error(self):
        """Breaks if a decode error escapes as JSONDecodeError and stops the run."""
        with self.assertRaises(json_schema.SchemaReadError):
            json_schema.load_with_lines('{"a": ')

    def test_bom_is_accepted(self):
        """Breaks if a UTF-8 BOM makes a valid file read as malformed."""
        value, _ = json_schema.load_with_lines("﻿" + fixtures.PERSON)
        self.assertEqual(value["id"], "http://schemas.example/common/person.json")


class RecogniseTest(unittest.TestCase):
    def test_draft04_meta_schema(self):
        """Breaks if a draft-04 `$schema` on json-schema.org is not recognised."""
        self.assertEqual(json_schema.recognise(json.loads(fixtures.PERSON)), "meta-schema")

    def test_2020_12_meta_schema(self):
        """Breaks if only the old drafts' `$schema` URLs are accepted."""
        self.assertEqual(json_schema.recognise(json.loads(fixtures.BASE_2020)), "meta-schema")

    def test_non_standard_schema_value_with_schema_keywords(self):
        """Breaks if a custom `$schema` value hides a schema-shaped root."""
        doc = {"$schema": "urn:example:custom-dialect", "type": "object", "properties": {"a": {}}}
        self.assertEqual(json_schema.recognise(doc), "schema-keywords")

    def test_tool_config_pointing_at_its_own_schema_is_not_a_schema(self):
        """Breaks if `$schema` alone is taken as proof: configs carry it too."""
        doc = {"$schema": "https://tool.example/config.json", "formatter": {"indent": 2}}
        self.assertIsNone(json_schema.recognise(doc))

    def test_declared_id_without_schema_keyword(self):
        """Breaks if a schema with an absolute `id` but no `$schema` is missed."""
        doc = {"id": "http://schemas.example/common/money.json", "properties": {"amount": {}}}
        self.assertEqual(json_schema.recognise(doc), "declared-id")

    def test_relative_id_without_schema_is_not_recognised(self):
        """Breaks if any `id` string, not an absolute URI, counts as a declaration."""
        self.assertIsNone(json_schema.recognise({"id": "money", "properties": {}}))

    def test_data_json_and_array_root_are_not_schemas(self):
        """Breaks if ordinary data with an `items` key, or an array root, is claimed."""
        self.assertIsNone(json_schema.recognise({"name": "x", "items": [1, 2]}))
        self.assertIsNone(json_schema.recognise([{"$schema": "http://json-schema.org/schema#"}]))


if __name__ == "__main__":
    unittest.main()
