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
        value, _ = json_schema.load_with_lines("\ufeff" + fixtures.PERSON)
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

    def test_declared_id_with_defs_only(self):
        """Breaks if `$defs` is not counted among the defining keywords."""
        doc = {"$id": "https://schemas.example/x.json", "$defs": {}}
        self.assertEqual(json_schema.recognise(doc), "declared-id")

    def test_absolute_id_without_defining_keyword_is_not_recognised(self):
        """Breaks if an absolute `id` alone, with no sub-schema container, counts."""
        self.assertIsNone(
            json_schema.recognise({"id": "https://schemas.example/x.json", "name": "n"})
        )

    def test_custom_schema_with_unknown_type_is_not_recognised(self):
        """Breaks if any `type` string, not a JSON type name, counts as a schema keyword."""
        self.assertIsNone(json_schema.recognise({"$schema": "urn:example:x", "type": "foo"}))

    def test_non_string_schema_is_not_recognised(self):
        """Breaks if a non-string `$schema` crashes or is taken as a declaration."""
        self.assertIsNone(json_schema.recognise({"$schema": 4, "type": "object"}))

    def test_list_valued_type_counts_as_schema_keyword(self):
        """Breaks if `type: ["object", "null"]` is not read as a schema type."""
        doc = {"$schema": "urn:example:x", "type": ["object", "null"]}
        self.assertEqual(json_schema.recognise(doc), "schema-keywords")


class WalkTest(unittest.TestCase):
    def walked(self, text, name):
        value, lines = json_schema.load_with_lines(text)
        return json_schema.walk(value, lines, name)

    def test_widget_created_entities(self):
        """Breaks if keyword scaffolding becomes an entity, or a field is lost."""
        w = self.walked(fixtures.WIDGET_CREATED, "widget-created.json")
        self.assertEqual(
            [(e.pointer, e.kind, e.label, e.line, e.parent) for e in w.entities],
            [
                ("", "schema", "WidgetCreated", 1, None),
                ("/properties/widgetId", "field", "widgetId", 7, ""),
                ("/properties/owner", "field", "owner", 8, ""),
                ("/properties/parts", "field", "parts", 9, ""),
                ("/definitions/Part", "definition", "Part", 13, ""),
                ("/definitions/Part/properties/sku", "field", "sku", 15, "/definitions/Part"),
            ],
        )

    def test_widget_created_refs(self):
        """Breaks if a ref loses its owner, context, line or base, or the wrong ones are kept."""
        w = self.walked(fixtures.WIDGET_CREATED, "widget-created.json")
        self.assertEqual(
            [(r.owner, r.ref, r.relation, r.context, r.line) for r in w.refs],
            [
                ("/properties/owner", "http://schemas.example/common/person.json",
                 "references", "properties/owner", 8),
                ("/properties/parts", "#/definitions/Part", "references", "properties/parts/items", 9),
            ],
        )  # fmt: skip
        self.assertEqual(
            {r.base for r in w.refs}, {"http://schemas.example/widgets/widget-created.json"}
        )

    def test_metadata_is_bounded_and_named(self):
        """Breaks if a metadata key is dropped, a keyword outside the list is copied, or required is wrong."""
        w = self.walked(fixtures.WIDGET_CREATED, "widget-created.json")
        by = {e.pointer: e for e in w.entities}
        self.assertEqual(
            by["/properties/widgetId"].metadata,
            {"type": "string", "required": True, "description": "The widget."},
        )
        self.assertEqual(by["/properties/owner"].metadata["required"], False)
        self.assertEqual(by["/definitions/Part/properties/sku"].metadata["enum"], ["a", "b"])
        self.assertEqual(
            by[""].metadata["schema_id"], "http://schemas.example/widgets/widget-created.json"
        )
        self.assertEqual(by[""].metadata["draft"], "http://json-schema.org/draft-04/schema#")

    def test_description_and_enum_are_cut(self):
        """Breaks if a long description or a long enum is copied whole into a node."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "properties": {"a": {"description": "x" * 600, "enum": list(range(30))}},
        }
        meta = json_schema.walk(doc, {}, "x.json").entities[1].metadata
        self.assertEqual(len(meta["description"]), 500)
        self.assertEqual(meta["enum"], list(range(20)))
        self.assertEqual(meta["enum_count"], 30)

    def test_untitled_root_is_labelled_with_the_file_name(self):
        """Breaks if a root without a title gets an empty or missing label."""
        w = self.walked(fixtures.PERSON, "person.json")
        self.assertEqual(w.entities[0].label, "person.json")

    def test_draft04_id_and_2020_dollar_id_are_both_declared(self):
        """Breaks if only `$id`, or only `id`, is read as the declared id."""
        self.assertEqual(
            self.walked(fixtures.PERSON, "person.json").declared,
            [("http://schemas.example/common/person.json", "")],
        )
        self.assertEqual(
            self.walked(fixtures.BASE_2020, "base.json").declared,
            [("https://schemas.example/events/base.json", "")],
        )

    def test_id_is_not_read_under_a_draft07_dialect(self):
        """Breaks if `id` is read where the dialect is draft-07, whose id keyword is `$id`."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "id": "http://schemas.example/x.json",
        }
        self.assertEqual(json_schema.walk(doc, {}, "x.json").declared, [])

    def test_id_is_read_when_the_dialect_is_unknown_and_dollar_id_absent(self):
        """Breaks if a schema with no `$schema` and only `id` is left undeclared."""
        doc = {"id": "http://schemas.example/x.json", "properties": {}}
        self.assertEqual(
            json_schema.walk(doc, {}, "x.json").declared, [("http://schemas.example/x.json", "")]
        )

    def test_nested_absolute_id_sets_the_base_beneath_it(self):
        """Breaks if a nested id is not declared or does not become the base for refs under it."""
        doc = {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "id": "http://schemas.example/a.json",
            "definitions": {
                "B": {
                    "id": "http://schemas.example/sub/b.json",
                    "properties": {"x": {"$ref": "c.json"}},
                },
                "C": {"$ref": "d.json"},
            },
        }
        w = json_schema.walk(doc, {}, "a.json")
        self.assertEqual(
            w.declared,
            [
                ("http://schemas.example/a.json", ""),
                ("http://schemas.example/sub/b.json", "/definitions/B"),
            ],
        )
        self.assertEqual([(r.owner, r.base) for r in w.refs],
                         [("/definitions/B/properties/x", "http://schemas.example/sub/b.json"),
                          ("/definitions/C", "http://schemas.example/a.json")])  # fmt: skip

    def test_hash_name_id_is_an_anchor(self):
        """Breaks if a draft-04 `id: "#name"` is read as an absolute id or ignored."""
        doc = {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "definitions": {"A": {"id": "#alpha"}},
        }
        w = json_schema.walk(doc, {}, "x.json")
        self.assertEqual((w.declared, w.anchors), ([], {"alpha": "/definitions/A"}))

    def test_all_of_ref_extends_and_inline_properties_join_the_owner(self):
        """Breaks if an allOf member's ref is a plain reference, or its properties get a wrong parent or via."""
        w = self.walked(fixtures.ORDER_PLACED_2020, "order-placed.json")
        self.assertEqual(
            [(r.owner, r.ref, r.relation, r.context) for r in w.refs],
            [("", "base.json#/$defs/Envelope", "extends", "allOf/0")],
        )
        field = [e for e in w.entities if e.kind == "field"][0]
        self.assertEqual(
            (field.pointer, field.parent, field.metadata["via"]),
            ("/allOf/1/properties/orderId", "", "allOf"),
        )

    def test_jsonschema2pojo_extends_is_an_extends_ref(self):
        """Breaks if `extends` is ignored or recorded as a plain reference."""
        w = self.walked(fixtures.POJO_CHILD, "child.json")
        self.assertEqual(
            [(r.owner, r.ref, r.relation, r.context) for r in w.refs],
            [("", "person.json", "extends", "extends")],
        )

    def test_extends_as_a_string_is_an_extends_ref(self):
        """Breaks if the string form of `extends` is dropped."""
        doc = {"$schema": "http://json-schema.org/draft-03/schema#", "extends": "person.json"}
        w = json_schema.walk(doc, {}, "x.json")
        self.assertEqual(
            [(r.ref, r.relation, r.context) for r in w.refs],
            [("person.json", "extends", "extends")],
        )

    def test_dynamic_refs_are_recorded(self):
        """Breaks if `$dynamicRef` and `$recursiveRef` vanish instead of being counted later."""
        doc = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "properties": {"a": {"$dynamicRef": "#meta"}, "b": {"$recursiveRef": "#"}},
        }
        w = json_schema.walk(doc, {}, "x.json")
        self.assertEqual(
            [(r.owner, r.ref, r.relation) for r in w.refs],
            [("/properties/a", "#meta", "references"), ("/properties/b", "#", "references")],
        )

    def test_defs_and_anchor_are_read(self):
        """Breaks if `$defs` entries or `$anchor` names are not read."""
        doc = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {"A": {"$anchor": "alpha", "type": "string"}},
        }
        w = json_schema.walk(doc, {}, "x.json")
        self.assertEqual([e.pointer for e in w.entities], ["", "/$defs/A"])
        self.assertEqual(w.anchors, {"alpha": "/$defs/A"})

    def test_pointer_tokens_are_escaped(self):
        """Breaks if `/` or `~` in a property name corrupts the pointer."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "properties": {"a/b": {}, "c~d": {}},
        }
        w = json_schema.walk(doc, {}, "x.json")
        self.assertEqual(
            [e.pointer for e in w.entities][1:], ["/properties/a~1b", "/properties/c~0d"]
        )

    def test_depth_limit_truncates_and_says_so(self):
        """Breaks if a deep file is walked without bound, or truncated silently."""
        doc = {"$schema": "http://json-schema.org/draft-07/schema#"}
        node = doc
        for _ in range(40):
            node["properties"] = {"n": {}}
            node = node["properties"]["n"]
        w = json_schema.walk(doc, {}, "deep.json")
        self.assertTrue(w.truncated)
        self.assertEqual(len([e for e in w.entities if e.kind == "field"]), 32)

    def test_shallow_file_is_not_truncated(self):
        """Breaks if truncated is set for a file within the depth limit."""
        self.assertFalse(self.walked(fixtures.WIDGET_CREATED, "w.json").truncated)

    def test_pattern_and_additional_properties_make_no_nodes_but_keep_refs(self):
        """Breaks if pattern keys become nodes or their refs are lost."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "patternProperties": {"^x-": {"$ref": "#/definitions/X"}},
            "additionalProperties": {"$ref": "#/definitions/Y"},
        }
        w = json_schema.walk(doc, {}, "x.json")
        self.assertEqual([e.kind for e in w.entities], ["schema"])
        self.assertEqual(
            sorted(r.context for r in w.refs), ["additionalProperties", "patternProperties/^x-"]
        )

    def test_normalise_uri(self):
        """Breaks if the empty fragment or a trailing # survives, or the path is lower-cased."""
        self.assertEqual(
            json_schema.normalise_uri("HTTP://Schemas.Example/Ab.json#"),
            "http://schemas.example/Ab.json",
        )
        self.assertEqual(
            json_schema.normalise_uri("http://x.example/a.json#/definitions/A"),
            "http://x.example/a.json#/definitions/A",
        )


if __name__ == "__main__":
    unittest.main()
