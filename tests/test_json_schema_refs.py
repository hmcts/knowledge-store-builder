"""Tests for knowledgestore/json_schema_refs.py - the id index, `$ref` resolution, the tie rule."""

from __future__ import annotations

import unittest

import json_schema_fixtures as fixtures
from knowledgestore import json_schema, json_schema_refs as refs

NO_FORKS: frozenset[str] = frozenset()

ANCHORED = (
    '{"$schema": "https://json-schema.org/draft/2020-12/schema",'
    ' "$id": "https://schemas.example/anchored.json",'
    ' "$defs": {"Name": {"$anchor": "name", "type": "string"}}}'
)

AWKWARD_KEYS = (
    '{"$schema": "http://json-schema.org/draft-04/schema#",'
    ' "id": "http://schemas.example/awkward.json",'
    ' "definitions": {"a/b": {"type": "string"}, "my def": {"type": "string"}}}'
)


def owned(repo: str, rel: str, text: str) -> refs.Owned:
    document, lines = json_schema.load_with_lines(text)
    basis = json_schema.recognise(document)
    assert basis is not None
    return refs.Owned(repo, rel, basis, json_schema.walk(document, lines, rel))


def make_ref(value: object, keyword: str = "$ref", base: str | None = None) -> json_schema.Ref:
    return json_schema.Ref("", value, "references", "x", 1, base, keyword)


class ResolveTest(unittest.TestCase):
    def widget(self):
        return owned("widgets", "schemas/widget-created.json", fixtures.WIDGET_CREATED)

    def test_local_pointer_lands_on_the_definition(self):
        """Breaks if a `#/...` ref is looked up in the id index instead of its own document."""
        w = self.widget()
        r = refs.resolve(refs.Index.build([w]), w, w.walked.refs[1], NO_FORKS)
        self.assertEqual(
            (r.how, r.target),
            (
                "local",
                refs.Target(
                    "widgets",
                    "schemas/widget-created.json",
                    "/definitions/Part",
                    "/definitions/Part",
                ),
            ),
        )

    def test_absolute_id_resolves_across_repositories(self):
        """Breaks if resolution is per repository: the cross-repository edge is the value."""
        w = self.widget()
        p = owned("common", "person.json", fixtures.PERSON)
        r = refs.resolve(refs.Index.build([w, p]), w, w.walked.refs[0], NO_FORKS)
        self.assertEqual(
            (r.how, r.rule, r.target.repo, r.target.rel, r.target.pointer),
            ("by-id", "only", "common", "person.json", ""),
        )

    def test_relative_ref_joins_the_declared_base(self):
        """Breaks if a relative ref ignores the declared base and is read as a path."""
        b = owned("events", "events/base.json", fixtures.BASE_2020)
        o = owned("events", "events/order-placed.json", fixtures.ORDER_PLACED_2020)
        r = refs.resolve(refs.Index.build([b, o]), o, o.walked.refs[0], NO_FORKS)
        self.assertEqual(
            (r.how, r.target.rel, r.target.pointer),
            ("relative-id", "events/base.json", "/$defs/Envelope"),
        )

    def test_relative_ref_without_a_base_resolves_by_path(self):
        """Breaks if a base-less relative ref is left unresolved instead of tried as a path."""
        c = owned("common", "child.json", fixtures.POJO_CHILD)
        p = owned("common", "person.json", fixtures.PERSON)
        r = refs.resolve(refs.Index.build([c, p]), c, c.walked.refs[0], NO_FORKS)
        self.assertEqual((r.how, r.target.rel), ("relative-path", "person.json"))

    def test_relative_path_is_taken_from_the_referring_files_directory(self):
        """Breaks if `../` is not normalised, or the path is taken from the repository root."""
        c = owned("common", "dir/child.json", fixtures.POJO_CHILD)
        p = owned("common", "person.json", fixtures.PERSON)
        r = refs.resolve(refs.Index.build([c, p]), c, make_ref("../person.json"), NO_FORKS)
        self.assertEqual((r.how, r.target.rel), ("relative-path", "person.json"))

    def test_relative_path_cannot_leave_the_repository(self):
        """Breaks if `../` past the root reaches a file of another repository or the filesystem."""
        c = owned("common", "child.json", fixtures.POJO_CHILD)
        p = owned("other", "person.json", fixtures.PERSON)
        idx = refs.Index.build([c, p])
        r = refs.resolve(idx, c, make_ref("../person.json"), NO_FORKS)
        self.assertEqual((r.how, r.target), ("unrecognised-target", None))

    def test_relative_path_to_another_repository_is_not_followed(self):
        """Breaks if a path ref looks the file up estate-wide instead of in the referring repository."""
        c = owned("common", "child.json", fixtures.POJO_CHILD)
        p = owned("other", "person.json", fixtures.PERSON)
        r = refs.resolve(refs.Index.build([c, p]), c, c.walked.refs[0], NO_FORKS)
        self.assertEqual(r.how, "unrecognised-target")

    def test_pointer_into_a_non_node_lands_on_the_enclosing_node(self):
        """Breaks if a pointer to a non-entity is unresolved, or loses the full pointer."""
        w = self.widget()
        r = refs.resolve(refs.Index.build([w]), w, make_ref("#/properties/parts/items"), NO_FORKS)
        self.assertEqual(
            (r.target.pointer, r.target.target_pointer),
            ("/properties/parts", "/properties/parts/items"),
        )

    def test_enclosing_node_is_the_nearest_not_the_root(self):
        """Breaks if the landing uses the first entity prefix (the root) rather than the longest."""
        w = self.widget()
        r = refs.resolve(
            refs.Index.build([w]), w, make_ref("#/definitions/Part/properties/sku/enum"), NO_FORKS
        )
        self.assertEqual(r.target.pointer, "/definitions/Part/properties/sku")

    def test_enclosing_node_matches_whole_tokens_not_string_prefixes(self):
        """Breaks if `/properties/parts` is taken to enclose `/properties/partsX/...`."""
        text = (
            '{"$schema": "http://json-schema.org/draft-04/schema#", "properties":'
            ' {"parts": {"type": "string"}, "partsX": {"properties": {"y": {"type": "string"}}}}}'
        )
        w = owned("widgets", "s.json", text)
        r = refs.resolve(refs.Index.build([w]), w, make_ref("#/properties/partsX/not"), NO_FORKS)
        self.assertEqual(r.how, "no-such-pointer")
        r = refs.resolve(
            refs.Index.build([w]), w, make_ref("#/properties/partsX/properties"), NO_FORKS
        )
        self.assertEqual(r.target.pointer, "/properties/partsX")

    def test_hash_alone_is_the_root(self):
        """Breaks if `#` is read as a plain-name fragment or as malformed."""
        w = self.widget()
        r = refs.resolve(refs.Index.build([w]), w, make_ref("#"), NO_FORKS)
        self.assertEqual((r.how, r.target.pointer, r.target.target_pointer), ("local", "", ""))

    def test_plain_name_fragment_resolves_through_the_anchor(self):
        """Breaks if `#name` is walked as a pointer or the anchor table is not consulted."""
        a = owned("events", "anchored.json", ANCHORED)
        idx = refs.Index.build([a])
        r = refs.resolve(idx, a, make_ref("#name"), NO_FORKS)
        self.assertEqual((r.how, r.target.pointer), ("anchor", "/$defs/Name"))
        r = refs.resolve(idx, a, make_ref("#nope"), NO_FORKS)
        self.assertEqual((r.how, r.target), ("no-such-pointer", None))

    def test_percent_encoded_fragment_is_decoded_before_the_walk(self):
        """Breaks if `%20` is looked up literally and a valid pointer reads as missing."""
        a = owned("widgets", "awkward.json", AWKWARD_KEYS)
        r = refs.resolve(refs.Index.build([a]), a, make_ref("#/definitions/my%20def"), NO_FORKS)
        self.assertEqual(
            (r.how, r.target.pointer, r.target.target_pointer),
            ("local", "/definitions/my def", "/definitions/my def"),
        )

    def test_tilde_escapes_are_undone_and_the_pointer_is_re_escaped(self):
        """Breaks if `~1` is not read as `/`, or the recorded pointer is not canonical."""
        a = owned("widgets", "awkward.json", AWKWARD_KEYS)
        r = refs.resolve(refs.Index.build([a]), a, make_ref("#/definitions/a~1b"), NO_FORKS)
        self.assertEqual(
            (r.how, r.target.pointer, r.target.target_pointer),
            ("local", "/definitions/a~1b", "/definitions/a~1b"),
        )

    def test_array_tokens_are_indices(self):
        """Breaks if a pointer cannot step through an array, or an out-of-range index resolves."""
        o = owned("events", "events/order-placed.json", fixtures.ORDER_PLACED_2020)
        idx = refs.Index.build([o])
        r = refs.resolve(idx, o, make_ref("#/allOf/1/properties/orderId"), NO_FORKS)
        self.assertEqual((r.how, r.target.pointer), ("local", "/allOf/1/properties/orderId"))
        r = refs.resolve(idx, o, make_ref("#/allOf/5"), NO_FORKS)
        self.assertEqual(r.how, "no-such-pointer")

    def test_self_reference_is_returned_resolved(self):
        """Breaks if a direct self-reference is dropped here, hiding it from Task 4's count."""
        w = self.widget()
        ref = json_schema.Ref("/definitions/Part", "#/definitions/Part", "references", "x", 1, None)
        r = refs.resolve(refs.Index.build([w]), w, ref, NO_FORKS)
        self.assertEqual((r.how, r.target.pointer == ref.owner), ("local", True))

    def test_unresolved_reasons(self):
        """Breaks if an unresolved reference is given the wrong reason, or raises."""
        w = self.widget()
        idx = refs.Index.build([w])
        cases = {
            "http://json-schema.org/draft-04/schema#": "meta-schema",
            "https://json-schema.org/draft/2020-12/schema": "meta-schema",
            "https://elsewhere.example/thing.json": "external",
            "#/definitions/Nope": "no-such-pointer",
            "missing.json": "unrecognised-target",
            42: "malformed",
            "http://[bad": "malformed",
        }
        for value, reason in cases.items():
            with self.subTest(ref=value):
                self.assertEqual(refs.resolve(idx, w, make_ref(value), NO_FORKS).how, reason)

    def test_relative_ref_to_a_meta_schema_is_a_meta_schema(self):
        """Breaks if the meta-schema check only looks at absolute refs, not at the joined URI."""
        w = self.widget()
        r = refs.resolve(
            refs.Index.build([w]),
            w,
            make_ref("draft/2020-12/schema", base="https://json-schema.org/x/"),
            NO_FORKS,
        )
        self.assertEqual(r.how, "meta-schema")

    def test_dynamic_ref_is_malformed_not_resolved(self):
        """Breaks if `$dynamicRef` is resolved statically as though it were `$ref`."""
        w = self.widget()
        for keyword in ("$dynamicRef", "$recursiveRef"):
            with self.subTest(keyword=keyword):
                r = refs.resolve(
                    refs.Index.build([w]), w, make_ref("#/definitions/Part", keyword), NO_FORKS
                )
                self.assertEqual((r.how, r.target), ("malformed", None))

    def test_fragment_on_an_absolute_id_is_resolved_in_the_found_document(self):
        """Breaks if the fragment of an absolute ref is walked in the referring document."""
        w = self.widget()
        c = owned("common", "common.json", fixtures.PERSON)
        ref = make_ref("http://schemas.example/common/person.json#/properties/name")
        r = refs.resolve(refs.Index.build([w, c]), w, ref, NO_FORKS)
        self.assertEqual(
            (r.how, r.target.repo, r.target.pointer), ("by-id", "common", "/properties/name")
        )


class IndexTest(unittest.TestCase):
    def test_one_file_declaring_an_id_twice_is_one_candidate(self):
        """Breaks if a nested re-declaration of an id makes a file its own rival (false ambiguity)."""
        text = (
            '{"$schema": "http://json-schema.org/draft-04/schema#",'
            ' "id": "http://schemas.example/dup.json", "definitions": {"A":'
            ' {"id": "http://schemas.example/dup.json", "type": "string"}}}'
        )
        d = owned("widgets", "dup.json", text)
        c = owned("common", "caller.json", fixtures.POJO_CHILD)
        ref = make_ref("http://schemas.example/dup.json")
        r = refs.resolve(refs.Index.build([c, d]), c, ref, NO_FORKS)
        self.assertEqual((r.how, r.rule, r.target.pointer), ("by-id", "only", ""))

    def test_scheme_and_host_case_do_not_matter(self):
        """Breaks if ids are compared un-normalised."""
        w = owned("widgets", "schemas/widget-created.json", fixtures.WIDGET_CREATED)
        p = owned("common", "person.json", fixtures.PERSON)
        ref = make_ref("HTTP://Schemas.Example/common/person.json#")
        r = refs.resolve(refs.Index.build([w, p]), w, ref, NO_FORKS)
        self.assertEqual((r.how, r.target.repo), ("by-id", "common"))


class TieRuleTest(unittest.TestCase):
    COMMON = ("common", "person.json", "")
    FORK = ("widgets-fork", "person.json", "")
    VENDOR = ("widgets", "vendor/person.json", "")

    def test_same_repository_copy_wins(self):
        """Breaks if a copy next to the referrer loses to an outside owner."""
        self.assertEqual(
            refs.choose(
                [self.COMMON, self.FORK, self.VENDOR],
                "widgets",
                "schemas/widget-created.json",
                NO_FORKS,
            ),
            (self.VENDOR, "same-repository"),
        )

    def test_declared_fork_is_passed_over(self):
        """Breaks if a declared fork is ignored and a copy is treated as an owner."""
        self.assertEqual(
            refs.choose([self.COMMON, self.FORK], "widgets", "s.json", frozenset({"widgets-fork"})),
            (self.COMMON, "not-a-fork"),
        )

    def test_only_forks_and_exactly_one(self):
        """Breaks if a lone candidate is rejected because its repository is a fork."""
        self.assertEqual(
            refs.choose([self.FORK], "widgets", "s.json", frozenset({"widgets-fork"})),
            (self.FORK, "only"),
        )

    def test_two_owners_and_no_rule_is_ambiguous_not_name_order(self):
        """Breaks if a tie falls back to the first candidate by name."""
        self.assertEqual(
            refs.choose([self.COMMON, self.FORK], "widgets", "s.json", NO_FORKS),
            (None, "ambiguous"),
        )

    def test_all_forks_is_ambiguous(self):
        """Breaks if the dropped `only-fork` rule returns and picks among forks by name."""
        forks = frozenset({"common", "widgets-fork"})
        self.assertEqual(
            refs.choose([self.COMMON, self.FORK], "widgets", "s.json", forks), (None, "ambiguous")
        )

    def test_the_file_declaring_the_id_itself_wins(self):
        """Breaks if a file that declares the id is not preferred to the copies elsewhere."""
        self.assertEqual(
            refs.choose([self.COMMON, self.FORK], "widgets-fork", "person.json", NO_FORKS),
            (self.FORK, "self"),
        )

    def test_choose_does_not_depend_on_input_order(self):
        """Breaks if the rule reads candidates in the caller's order (CLAUDE.md 1.3)."""
        a = refs.choose([self.COMMON, self.FORK, self.VENDOR], "x", "y.json", NO_FORKS)
        b = refs.choose([self.VENDOR, self.FORK, self.COMMON], "x", "y.json", NO_FORKS)
        self.assertEqual((a, b), ((None, "ambiguous"), (None, "ambiguous")))

    def test_ambiguous_resolution_lists_candidates_sorted(self):
        """Breaks if candidates are listed in input order or without the `repo:path` form."""
        w = owned("widgets", "schemas/widget-created.json", fixtures.WIDGET_CREATED)
        a = owned("common", "person.json", fixtures.PERSON)
        b = owned("widgets-fork", "person.json", fixtures.PERSON)
        r = refs.resolve(refs.Index.build([b, w, a]), w, w.walked.refs[0], NO_FORKS)
        self.assertEqual(
            (r.how, r.target, r.candidates),
            ("ambiguous", None, ("common:person.json", "widgets-fork:person.json")),
        )

    def test_resolve_passes_forks_to_the_rule(self):
        """Breaks if resolve does not hand the fork set to choose."""
        w = owned("widgets", "schemas/widget-created.json", fixtures.WIDGET_CREATED)
        a = owned("common", "person.json", fixtures.PERSON)
        b = owned("widgets-fork", "person.json", fixtures.PERSON)
        r = refs.resolve(
            refs.Index.build([b, w, a]), w, w.walked.refs[0], frozenset({"widgets-fork"})
        )
        self.assertEqual((r.how, r.rule, r.target.repo), ("by-id", "not-a-fork", "common"))

    def test_resolve_prefers_the_referring_file_itself(self):
        """Breaks if resolve ignores `self` and reports a false ambiguity for a copied schema."""
        w = owned("widgets", "schemas/widget-created.json", fixtures.WIDGET_CREATED)
        copy = owned("widgets-fork", "schemas/widget-created.json", fixtures.WIDGET_CREATED)
        ref = make_ref("http://schemas.example/widgets/widget-created.json")
        r = refs.resolve(refs.Index.build([copy, w]), w, ref, NO_FORKS)
        self.assertEqual((r.how, r.rule, r.target.repo), ("by-id", "self", "widgets"))


if __name__ == "__main__":
    unittest.main()
