"""Tests for knowledgestore/json_schema.py - parsing with line numbers and recognition."""

from __future__ import annotations

import contextlib
import json
import sys
import time
import unittest

import json_schema_fixtures as fixtures
import tool_config_fixtures as tool_configs
from knowledgestore import json_schema


@contextlib.contextmanager
def python_default_recursion_limit():
    """Run at Python's default recursion limit, as the stage's own process does.

    graphify's extractor raises the process-wide limit to 10,000 and leaves it
    there, so a test that ran it earlier in the suite would otherwise decide how
    deep the pure-Python scanner can parse here.
    """
    before = sys.getrecursionlimit()
    sys.setrecursionlimit(1_000)
    try:
        yield
    finally:
        sys.setrecursionlimit(before)


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

    def test_lines_hold_across_a_large_document_in_linear_time(self):
        """Breaks if a line is miscounted far into a file, or if each object's line
        is found by rescanning from the start, which made a 1.4 MB file take 8 s.

        The layout puts property `p<i>` on line 4 + i: line 1 is `{`, line 2 the
        `$schema`, line 3 opens `properties`. About 2.6 MB; linear work takes well
        under a second, and the bound is generous so a slow runner cannot flake it.
        """
        count = 60_000
        text = (
            '{\n"$schema": "http://json-schema.org/draft-07/schema#",\n"properties": {\n'
            + ",\n".join(f'"p{i}": {{"type": "string", "title": "t"}}' for i in range(count))
            + "\n}\n}\n"
        )
        started = time.monotonic()
        value, lines = json_schema.load_with_lines(text)
        elapsed = time.monotonic() - started
        properties = value["properties"]
        self.assertEqual(lines[id(value)], 1)
        self.assertEqual(lines[id(properties)], 3)
        for i in (0, 1, 2, 9_999, 30_000, count - 1):
            self.assertEqual(lines[id(properties[f"p{i}"])], 4 + i)
        self.assertLess(elapsed, 5.0)

    def test_deep_nesting_is_a_read_error_not_a_crash(self):
        """Breaks if nesting past the recursion limit raises RecursionError, which
        `read_owned` does not catch, so one such file stops the whole stage."""
        deep = '{"a": ' * 5_000 + "1" + "}" * 5_000
        with python_default_recursion_limit(), self.assertRaises(json_schema.SchemaReadError):
            json_schema.load_with_lines(deep)
        array = "[" * 100_000 + "]" * 100_000
        with self.assertRaises(json_schema.SchemaReadError):
            json_schema.load(array)

    def test_integer_past_the_digit_limit_is_a_read_error_not_a_crash(self):
        """Breaks if an integer over Python's 4,300-digit conversion limit raises a
        plain ValueError out of the parse, which `read_owned` does not catch."""
        text = '{"a": ' + "1" * 5_000 + "}"
        with self.assertRaises(json_schema.SchemaReadError):
            json_schema.load_with_lines(text)
        with self.assertRaises(json_schema.SchemaReadError):
            json_schema.load(text)

    def test_load_accepts_a_bom_and_matches_json_loads(self):
        """Breaks if the fast parse used to decide recognition rejects a BOM, or
        decodes differently from the line-tracking parse it stands in for."""
        self.assertEqual(
            json_schema.load("\ufeff" + fixtures.WIDGET_CREATED),
            json.loads(fixtures.WIDGET_CREATED),
        )
        with self.assertRaises(json_schema.SchemaReadError):
            json_schema.load('{"a": ')


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

    def test_json_schema_org_named_outside_the_host_is_not_a_meta_schema(self):
        """Breaks if the meta-schema basis matches `json-schema.org` anywhere in the text.

        A `$schema` on another host only mentions the site, so it is judged on its
        keywords like any other custom dialect.
        """
        evil = "https://evil.example/json-schema.org/x"
        self.assertEqual(
            json_schema.recognise({"$schema": evil, "properties": {"a": {}}}), "schema-keywords"
        )
        self.assertIsNone(json_schema.recognise({"$schema": evil, "formatter": {}}))

    def test_a_subdomain_of_json_schema_org_is_a_meta_schema(self):
        """Breaks if only the bare host counts, or the host is compared case-sensitively."""
        doc = {"$schema": "https://WWW.Json-Schema.org/draft-07/schema#"}
        self.assertEqual(json_schema.recognise(doc), "meta-schema")

    def test_an_unparseable_schema_uri_is_judged_on_its_keywords(self):
        """Breaks if a `$schema` that cannot be parsed as a URI raises out of recognition."""
        doc = {"$schema": "http://[json-schema.org", "properties": {"a": {}}}
        self.assertEqual(json_schema.recognise(doc), "schema-keywords")

    def test_list_valued_type_counts_as_schema_keyword(self):
        """Breaks if `type: ["object", "null"]` is not read as a schema type."""
        doc = {"$schema": "urn:example:x", "type": ["object", "null"], "items": {}}
        self.assertEqual(json_schema.recognise(doc), "schema-keywords")

    def test_real_tool_configs_declaring_a_schema_are_not_schemas(self):
        """Breaks if a `$schema` naming a tool's own schema, plus a key that shares a
        JSON Schema keyword's name (`extends` above all), makes the config a schema.

        Each is a public format in its real layout. Claiming one removes graphify's
        nodes for it and replaces them with a bare `schema` node.
        """
        for path, text in tool_configs.FILES.items():
            with self.subTest(path=path):
                self.assertIsNone(json_schema.recognise(json.loads(text)))

    def test_a_foreign_schema_needs_a_defining_keyword(self):
        """Breaks if a bare `extends`, `$ref`, `items`, combinator or `type` is enough
        under a `$schema` that does not name json-schema.org."""
        for bare in (
            {"extends": {"$ref": "other.json"}},
            {"extends": [{"$ref": "other.json"}]},
            {"$ref": "other.json"},
            {"items": {"type": "string"}},
            {"allOf": [{"properties": {}}]},
            {"type": "object"},
            {"type": "array"},
        ):
            with self.subTest(bare=bare):
                self.assertIsNone(
                    json_schema.recognise({"$schema": "https://tool.example/c.json", **bare})
                )

    def test_a_foreign_schema_with_a_defining_keyword_is_a_schema(self):
        """Breaks if the narrowing drops schemas written against a custom meta-schema:
        `properties`, `definitions` or `$defs`, or a JSON `type` beside `properties`
        or `items`."""
        for defining in (
            {"properties": {"a": {}}},
            {"definitions": {"A": {}}},
            {"$defs": {"A": {}}},
            {"type": "object", "properties": {"a": {}}},
            {"type": "array", "items": {"type": "string"}},
            {"type": ["object", "null"], "properties": {}},
        ):
            with self.subTest(defining=defining):
                self.assertEqual(
                    json_schema.recognise({"$schema": "urn:example:meta", **defining}),
                    "schema-keywords",
                )

    def test_a_type_that_is_not_a_json_type_does_not_make_items_a_schema(self):
        """Breaks if `type` beside `items` counts without being a JSON type: a
        package manifest's `"type": "module"` and an `items` list are data."""
        doc = {"$schema": "urn:example:x", "type": "module", "items": ["a"]}
        self.assertIsNone(json_schema.recognise(doc))

    def test_the_meta_schema_and_declared_id_bases_do_not_need_a_defining_keyword(self):
        """Breaks if the narrowing leaks past a foreign `$schema`: a document on
        json-schema.org is a schema whatever its root holds, and a declared id keeps
        its own rule."""
        self.assertEqual(
            json_schema.recognise({"$schema": "http://json-schema.org/draft-07/schema#"}),
            "meta-schema",
        )
        self.assertEqual(
            json_schema.recognise(
                {
                    "$schema": "http://json-schema.org/draft-03/schema#",
                    "extends": ["preset:a", "preset:b"],
                }
            ),
            "meta-schema",
        )
        self.assertEqual(
            json_schema.recognise({"$id": "https://schemas.example/x.json", "$defs": {}}),
            "declared-id",
        )


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

    def test_ref_records_the_keyword_it_was_found_under(self):
        """Breaks if `$dynamicRef` and `$recursiveRef` read as plain `$ref`, so resolve treats a dynamic reference as static."""
        w = self.walked(
            '{"properties": {"a": {"$ref": "#/x"}, "b": {"$dynamicRef": "#y"},'
            ' "c": {"$recursiveRef": "#"}}}',
            "k.json",
        )
        self.assertEqual([r.keyword for r in w.refs], ["$ref", "$dynamicRef", "$recursiveRef"])

    def test_widget_created_refs(self):
        """Breaks if a ref loses its owner, context, line or base, or the wrong ones are kept."""
        w = self.walked(fixtures.WIDGET_CREATED, "widget-created.json")
        self.assertEqual(
            [(r.owner, r.value, r.relation, r.context, r.line) for r in w.refs],
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
        self.assertFalse(by["/properties/owner"].metadata["required"])
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
            [(r.owner, r.value, r.relation, r.context) for r in w.refs],
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
            [(r.owner, r.value, r.relation, r.context) for r in w.refs],
            [("", "person.json", "extends", "extends")],
        )

    def test_extends_as_an_object_or_a_list_of_objects_is_read(self):
        """Breaks if the narrowing of `extends` drops the schema forms: an object, and
        each member of a list of objects, each an `extends` ref at its own context."""
        draft03 = "http://json-schema.org/draft-03/schema#"
        w = json_schema.walk({"$schema": draft03, "extends": {"$ref": "a.json"}}, {}, "x.json")
        self.assertEqual(
            [(r.value, r.relation, r.context) for r in w.refs], [("a.json", "extends", "extends")]
        )
        listed = {"$schema": draft03, "extends": [{"$ref": "a.json"}, {"$ref": "b.json"}]}
        w = json_schema.walk(listed, {}, "x.json")
        self.assertEqual(
            [(r.value, r.relation, r.context) for r in w.refs],
            [("a.json", "extends", "extends/0"), ("b.json", "extends", "extends/1")],
        )

    def test_extends_as_a_string_or_a_list_of_strings_is_not_a_reference(self):
        """Breaks if preset names under `extends` are emitted as `extends` refs: they
        reach the unresolved list as `unrecognised-target` whatever the file is."""
        draft03 = "http://json-schema.org/draft-03/schema#"
        cases = (
            ("person.json", []),
            (["preset:a", "preset:b"], []),
            (["a.json", {"$ref": "b.json"}], [("b.json", "extends", "extends/1")]),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                w = json_schema.walk({"$schema": draft03, "extends": value}, {}, "x.json")
                self.assertEqual([(r.value, r.relation, r.context) for r in w.refs], expected)

    def test_dynamic_refs_are_recorded(self):
        """Breaks if `$dynamicRef` and `$recursiveRef` vanish instead of being counted later."""
        doc = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "properties": {"a": {"$dynamicRef": "#meta"}, "b": {"$recursiveRef": "#"}},
        }
        w = json_schema.walk(doc, {}, "x.json")
        self.assertEqual(
            [(r.owner, r.value, r.relation) for r in w.refs],
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

    def test_required_is_inherited_by_fields_inside_a_combinator_member(self):
        """Breaks if a name the enclosing node requires reads as optional on a field in its allOf member."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "required": ["a"],
            "allOf": [{"properties": {"a": {}, "b": {}}}],
        }
        by = {e.pointer: e for e in json_schema.walk(doc, {}, "x.json").entities}
        self.assertTrue(by["/allOf/0/properties/a"].metadata["required"])
        self.assertFalse(by["/allOf/0/properties/b"].metadata["required"])

    def test_required_in_a_sibling_all_of_member_applies(self):
        """Breaks if a `required` array in a sibling allOf member is ignored."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "allOf": [{"properties": {"a": {}}}, {"required": ["a"]}],
        }
        by = {e.pointer: e for e in json_schema.walk(doc, {}, "x.json").entities}
        self.assertTrue(by["/allOf/0/properties/a"].metadata["required"])

    def test_required_in_a_one_of_sibling_does_not_apply(self):
        """Breaks if a oneOf sibling's `required` marks a field optional-by-branch as required."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "oneOf": [{"properties": {"a": {}}}, {"required": ["a"]}],
        }
        by = {e.pointer: e for e in json_schema.walk(doc, {}, "x.json").entities}
        self.assertFalse(by["/oneOf/0/properties/a"].metadata["required"])

    def test_required_does_not_leak_into_a_nested_items_schema(self):
        """Breaks if an outer `required` is applied to same-named fields under `items`."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "required": ["a"],
            "allOf": [{"items": {"properties": {"a": {}}}}],
        }
        by = {e.pointer: e for e in json_schema.walk(doc, {}, "x.json").entities}
        self.assertFalse(by["/allOf/0/items/properties/a"].metadata["required"])

    def test_draft03_required_true_on_the_property(self):
        """Breaks if the draft-03 form `required: true` on a property is not read."""
        doc = {
            "$schema": "http://json-schema.org/draft-03/schema#",
            "properties": {"a": {"required": True}, "b": {"required": False}},
        }
        by = {e.pointer: e for e in json_schema.walk(doc, {}, "x.json").entities}
        self.assertEqual(
            (by["/properties/a"].metadata["required"], by["/properties/b"].metadata["required"]),
            (True, False),
        )

    def test_tuple_items_get_an_indexed_context(self):
        """Breaks if tuple `items` refs lose their position or are not descended."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "items": [{"$ref": "#/definitions/A"}, {"$ref": "#/definitions/B"}],
        }
        w = json_schema.walk(doc, {}, "x.json")
        self.assertEqual(
            [(r.value, r.context, r.relation) for r in w.refs],
            [
                ("#/definitions/A", "items/0", "references"),
                ("#/definitions/B", "items/1", "references"),
            ],
        )

    def test_prefix_items_and_conditionals_keep_their_refs(self):
        """Breaks if a `$ref` under prefixItems, if, then or else is dropped or misfiled as extends."""
        doc = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "prefixItems": [{"$ref": "#/$defs/A"}],
            "if": {"$ref": "#/$defs/B"},
            "then": {"$ref": "#/$defs/C"},
            "else": {"$ref": "#/$defs/D"},
        }
        w = json_schema.walk(doc, {}, "x.json")
        self.assertEqual(
            [(r.value, r.context, r.relation) for r in w.refs],
            [
                ("#/$defs/A", "prefixItems/0", "references"),
                ("#/$defs/B", "if", "references"),
                ("#/$defs/C", "then", "references"),
                ("#/$defs/D", "else", "references"),
            ],
        )
        self.assertEqual([e.kind for e in w.entities], ["schema"])

    def test_other_schema_valued_keywords_keep_their_refs(self):
        """Breaks if contains, additionalItems, propertyNames, unevaluated*, or dependent schemas drop refs."""
        doc = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "contains": {"$ref": "#/$defs/A"},
            "additionalItems": {"$ref": "#/$defs/B"},
            "propertyNames": {"$ref": "#/$defs/C"},
            "unevaluatedItems": {"$ref": "#/$defs/D"},
            "unevaluatedProperties": {"$ref": "#/$defs/E"},
            "dependentSchemas": {"x": {"$ref": "#/$defs/F"}},
            "dependencies": {"y": {"$ref": "#/$defs/G"}, "z": ["w"]},
        }
        w = json_schema.walk(doc, {}, "x.json")
        self.assertEqual(
            [(r.value, r.context) for r in w.refs],
            [
                ("#/$defs/A", "contains"),
                ("#/$defs/B", "additionalItems"),
                ("#/$defs/C", "propertyNames"),
                ("#/$defs/D", "unevaluatedItems"),
                ("#/$defs/E", "unevaluatedProperties"),
                ("#/$defs/F", "dependentSchemas/x"),
                ("#/$defs/G", "dependencies/y"),
            ],
        )

    def shape(self, doc: dict) -> tuple[list, list]:
        w = json_schema.walk(doc, {}, "x.json")
        entities = sorted((e.pointer, e.kind, e.metadata.get("required")) for e in w.entities)
        return entities, sorted((r.owner, r.value, r.relation, r.context) for r in w.refs)

    def test_properties_under_not_are_not_fields(self):
        """Breaks if `not: {properties, required}` turns the forbidden name into a
        field of the schema, marked required, which says the opposite of the schema."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "properties": {"name": {}},
            "not": {
                "properties": {"forbidden": {"$ref": "#/definitions/F"}},
                "required": ["forbidden"],
            },
            "definitions": {"F": {}},
        }
        self.assertEqual(
            self.shape(doc),
            (
                [("", "schema", None), ("/definitions/F", "definition", None),
                 ("/properties/name", "field", False)],
                [("", "#/definitions/F", "references", "not/properties/forbidden")],
            ),
        )  # fmt: skip

    def test_properties_under_a_conditional_are_not_fields(self):
        """Breaks if the `if` test's properties, or a `then` branch's, become fields
        of the schema: they describe a case, not the shape every instance has."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "if": {"properties": {"kind": {"const": "card"}}, "required": ["kind"]},
            "then": {"properties": {"card": {"$ref": "#/definitions/Card"}}},
            "else": {"properties": {"iban": {}}},
            "definitions": {"Card": {}},
        }
        self.assertEqual(
            self.shape(doc),
            (
                [("", "schema", None), ("/definitions/Card", "definition", None)],
                [("", "#/definitions/Card", "references", "then/properties/card")],
            ),
        )

    def test_properties_of_a_map_value_schema_are_not_fields_of_the_map(self):
        """Breaks if `additionalProperties` on a map field contributes the value
        schema's properties as fields: the map's keys are not those names."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "properties": {
                "labels": {
                    "type": "object",
                    "additionalProperties": {
                        "properties": {"text": {"$ref": "#/definitions/Text"}},
                        "required": ["text"],
                    },
                }
            },
            "definitions": {"Text": {}},
        }
        self.assertEqual(
            self.shape(doc),
            (
                [("", "schema", None), ("/definitions/Text", "definition", None),
                 ("/properties/labels", "field", False)],
                [("/properties/labels", "#/definitions/Text", "references",
                  "properties/labels/additionalProperties/properties/text")],
            ),
        )  # fmt: skip

    def test_properties_under_dependencies_are_not_fields(self):
        """Breaks if a dependency schema's properties become fields, required ones
        included: they apply only when the named property is present."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "properties": {"card": {}},
            "dependencies": {
                "card": {
                    "properties": {"billing": {"$ref": "#/definitions/Address"}},
                    "required": ["billing"],
                }
            },
            "definitions": {"Address": {}},
        }
        self.assertEqual(
            self.shape(doc),
            (
                [("", "schema", None), ("/definitions/Address", "definition", None),
                 ("/properties/card", "field", False)],
                [("", "#/definitions/Address", "references",
                  "dependencies/card/properties/billing")],
            ),
        )  # fmt: skip

    def test_every_refs_only_keyword_creates_no_field_and_keeps_its_ref(self):
        """Breaks if any keyword ruled refs-only creates a field beneath it, or loses
        a `$ref` beneath it, or the mode stops at the first level of nesting."""
        for keyword in (
            "not", "if", "then", "else", "contains", "propertyNames",
            "additionalItems", "unevaluatedItems", "unevaluatedProperties",
            "additionalProperties",
        ):  # fmt: skip
            with self.subTest(keyword=keyword):
                doc = {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    keyword: {"items": {"properties": {"a": {"$ref": "#/definitions/A"}}}},
                }
                self.assertEqual(
                    self.shape(doc),
                    (
                        [("", "schema", None)],
                        [("", "#/definitions/A", "references", f"{keyword}/items/properties/a")],
                    ),
                )
        for keyword in ("dependentSchemas", "patternProperties"):
            with self.subTest(keyword=keyword):
                doc = {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    keyword: {"k": {"properties": {"a": {"$ref": "#/definitions/A"}}}},
                }
                self.assertEqual(
                    self.shape(doc),
                    (
                        [("", "schema", None)],
                        [("", "#/definitions/A", "references", f"{keyword}/k/properties/a")],
                    ),
                )

    def test_an_all_of_ref_under_not_is_a_reference_not_an_extension(self):
        """Breaks if `not: {allOf: [{$ref}]}` is written as `extends`: a schema that
        must not match the base does not extend it."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "not": {"allOf": [{"$ref": "#/definitions/Base"}]},
        }
        self.assertEqual(
            self.shape(doc)[1], [("", "#/definitions/Base", "references", "not/allOf/0")]
        )

    def test_a_definition_under_a_refs_only_keyword_is_still_a_definition(self):
        """Breaks if a definition nested under `not` is dropped (definitions are read
        at any depth), or if its own properties are not its fields."""
        doc = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "not": {"definitions": {"N": {"properties": {"x": {}}, "required": ["x"]}}},
        }
        self.assertEqual(
            self.shape(doc)[0],
            [("", "schema", None), ("/not/definitions/N", "definition", None),
             ("/not/definitions/N/properties/x", "field", True)],
        )  # fmt: skip

    def test_tuple_and_combinator_members_still_create_fields(self):
        """Breaks if `prefixItems`, `items` or a combinator is made refs-only: their
        members describe the instance, so their properties are fields."""
        doc = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "prefixItems": [{"properties": {"a": {}}}],
            "items": {"properties": {"b": {}}},
            "anyOf": [{"properties": {"c": {}}}],
        }
        self.assertEqual(
            [p for p, kind, _ in self.shape(doc)[0] if kind == "field"],
            ["/anyOf/0/properties/c", "/items/properties/b", "/prefixItems/0/properties/a"],
        )

    def test_id_is_read_under_an_unknown_schema_uri(self):
        """Breaks if a `$schema` that is not on json-schema.org stops `id` from being read."""
        doc = {
            "$schema": "https://tool.example/dialect.json",
            "id": "http://schemas.example/x.json",
        }
        self.assertEqual(
            json_schema.walk(doc, {}, "x.json").declared, [("http://schemas.example/x.json", "")]
        )

    def test_id_is_read_under_a_schema_uri_that_only_mentions_json_schema_org(self):
        """Breaks if the dialect test reads `json-schema.org` in the path as the host."""
        doc = {
            "$schema": "https://evil.example/json-schema.org/draft-07/schema",
            "id": "http://schemas.example/x.json",
        }
        self.assertEqual(
            json_schema.walk(doc, {}, "x.json").declared, [("http://schemas.example/x.json", "")]
        )

    def test_id_is_read_when_schema_is_not_a_string(self):
        """Breaks if a non-string `$schema` crashes the walk or suppresses `id` (the rule: unknown dialect)."""
        doc = {"$schema": 4, "id": "http://schemas.example/x.json"}
        self.assertEqual(
            json_schema.walk(doc, {}, "x.json").declared, [("http://schemas.example/x.json", "")]
        )

    def test_only_dollar_id_is_declared_when_both_are_present(self):
        """Breaks if `id` overrides or joins `$id` under an unknown dialect."""
        doc = {"id": "http://schemas.example/old.json", "$id": "http://schemas.example/new.json"}
        w = json_schema.walk(doc, {}, "x.json")
        self.assertEqual(w.declared, [("http://schemas.example/new.json", "")])
        self.assertEqual(w.entities[0].metadata["schema_id"], "http://schemas.example/new.json")

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
