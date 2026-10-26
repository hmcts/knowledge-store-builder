"""Read JSON Schema documents: parse with line numbers and recognise a schema.

Scope, and the exception to CLAUDE.md section 9. That section says graphify is
a peer CLI and the library does not re-implement extraction. The `json-schema`
stage is a narrow exception, accepted knowingly:

- It reads one format: JSON Schema documents, nothing else. Not JSON
  generally, not YAML schemas, OpenAPI, Avro or Protobuf. A proposal to widen
  it is a new scope decision, not an extension.
- It is a library-side enrichment over graphify's output, of the same kind as
  `gherkin` and `packages`: a stage that reads the merged graph, adds a layer
  it owns under its own `_origin`, and rebuilds that layer from scratch on
  every run.
- It retires when graphify extracts schemas semantically. If graphify ships
  its own schema extraction (definitions, properties and resolved `$ref`s),
  this layer is removed rather than kept in parallel. The tripwire is the
  graphify behaviour test that ships with the stage: when graphify's treatment
  of schema documents changes, it fails, which is the prompt to reassess.

This module is the pure part: no graph, no filesystem.
"""

from __future__ import annotations

import bisect
import json
import json.decoder
import json.scanner
import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from knowledgestore import kinds

JSON_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")

# Root keywords that define a schema (spec row `schema-keywords`).
SCHEMA_KEYWORDS = (
    "properties",
    "definitions",
    "$defs",
    "items",
    "allOf",
    "oneOf",
    "anyOf",
    "$ref",
    "extends",
    "patternProperties",
    "additionalProperties",
    "type",
)

# Root keywords that carry sub-schemas (spec row `declared-id`).
_DEFINING_KEYWORDS = ("properties", "definitions", "$defs")

# The stdlib hooks used below (`JSONObject`, `py_make_scanner`, the decoder's
# `parse_object` and `scan_once`) are real but untyped; reach them as Any.
_decoder_module: Any = json.decoder
_scanner_module: Any = json.scanner

_ABSOLUTE_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


class SchemaReadError(ValueError):
    """The text cannot be decoded: not valid JSON, or beyond what the parser accepts."""


# Valid JSON can still fail to decode: nesting past the recursion limit raises
# RecursionError, and an integer over Python's digit limit a plain ValueError.
# Either is a file this stage cannot read, not a reason to stop the estate.
_DECODE_ERRORS = (ValueError, RecursionError)


def _without_bom(text: str) -> str:
    return text[1:] if text.startswith("\ufeff") else text


def load(text: str) -> object:
    """Decode `text` with the C parser, without line numbers.

    Recognition needs only the value, and most `.json` in a content set is data,
    so a file is parsed with line tracking only once it is known to be a schema.
    """
    try:
        return json.loads(_without_bom(text))
    except _DECODE_ERRORS as exc:
        raise SchemaReadError(str(exc)) from exc


def load_with_lines(text: str) -> tuple[object, dict[int, int]]:
    """Decode `text`; also map `id(obj)` to the 1-based line of each object's `{`."""
    text = _without_bom(text)
    # Newline offsets once, searched per object: counting from the start of the
    # text for every object made the mapping quadratic in the file's size.
    newlines = [match.start() for match in re.finditer("\n", text)]
    starts: dict[int, int] = {}
    decoder: Any = json.JSONDecoder()

    def parse_object(s_and_end, strict, scan_once, object_hook, object_pairs_hook, memo):
        _s, after_brace = s_and_end
        result = _decoder_module.JSONObject(
            s_and_end, strict, scan_once, object_hook, object_pairs_hook, memo
        )
        starts[id(result[0])] = bisect.bisect_left(newlines, after_brace - 1) + 1
        return result

    decoder.parse_object = parse_object
    # The C scanner ignores an overridden parse_object; rebuild with the Python one.
    decoder.scan_once = _scanner_module.py_make_scanner(decoder)
    try:
        value = decoder.decode(text)
    except _DECODE_ERRORS as exc:
        raise SchemaReadError(str(exc)) from exc
    return value, starts


def _is_absolute_uri(value: object) -> bool:
    return isinstance(value, str) and _ABSOLUTE_URI.match(value) is not None


def _has_schema_type(value: object) -> bool:
    if isinstance(value, str):
        return value in JSON_TYPES
    if isinstance(value, list):
        return any(isinstance(v, str) and v in JSON_TYPES for v in value)
    return False


def _has_schema_keyword(document: dict) -> bool:
    for keyword in SCHEMA_KEYWORDS:
        if keyword not in document:
            continue
        if keyword != "type" or _has_schema_type(document["type"]):
            return True
    return False


def recognise(document: object) -> str | None:
    """Return the basis on which `document` is a JSON Schema, or None."""
    if not isinstance(document, dict):
        return None
    if "$schema" in document:
        declared = document["$schema"]
        if not isinstance(declared, str):
            return None
        if "json-schema.org" in declared:
            return "meta-schema"
        return "schema-keywords" if _has_schema_keyword(document) else None
    has_id = _is_absolute_uri(document.get("id")) or _is_absolute_uri(document.get("$id"))
    if has_id and any(k in document for k in _DEFINING_KEYWORDS):
        return "declared-id"
    return None


# --- walking one document -------------------------------------------------

MAX_DEPTH = 32
_DESCRIPTION_LIMIT = 500
_ENUM_LIMIT = 20
_COMBINATORS = ("allOf", "oneOf", "anyOf")
_REF_KEYWORDS = ("$ref", "$dynamicRef", "$recursiveRef")

# Keywords whose subschemas constrain an instance without describing its shape:
# a negation, a condition, a dependency, or the values of keys not named. A
# `properties` beneath one names no field the instance has, so nothing beneath
# creates a field and no `required` is read there; a `$ref` beneath one is
# still a `references` edge from the enclosing node. The mode holds for the
# whole subtree, except inside a definition, which is a named schema in its own
# right (definitions are read at any depth).
_REFS_ONLY = frozenset(
    {
        "not",
        "if",
        "then",
        "else",
        "dependencies",
        "dependentSchemas",
        "additionalProperties",
        "patternProperties",
        "contains",
        "propertyNames",
        "additionalItems",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)


def escape_pointer(token: str) -> str:
    """Apply the RFC 6901 escapes to one reference token."""
    return token.replace("~", "~0").replace("/", "~1")


def _pointer(tokens: tuple) -> str:
    return "".join("/" + escape_pointer(str(t)) for t in tokens)


def normalise_uri(uri: str) -> str:
    """Drop an empty fragment and a trailing `#`; lower-case the scheme and host."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return uri[:-1] if uri.endswith("#") else uri
    userinfo, at, hostport = parts.netloc.rpartition("@")
    netloc = userinfo + at + hostport.lower()
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment))


@dataclass(frozen=True)
class Entity:
    pointer: str
    kind: str
    label: str
    line: int
    parent: str | None
    metadata: dict = field(compare=False, hash=False)


@dataclass(frozen=True)
class Ref:
    owner: str
    ref: object
    relation: str
    context: str
    line: int
    base: str | None
    # The keyword the value was found under; only `$ref` is resolved statically.
    keyword: str = "$ref"


@dataclass
class Walked:
    entities: list[Entity]
    refs: list[Ref]
    declared: list[tuple[str, str]]
    anchors: dict[str, str]
    document: object
    truncated: bool

    @cached_property
    def entity_pointers(self) -> frozenset[str]:
        """Every entity's pointer, so a reference lands without a scan of the file."""
        return frozenset(entity.pointer for entity in self.entities)


@dataclass(frozen=True)
class _EntityInfo:
    kind: str
    label: str
    parent: str | None
    required: bool | None
    via: str | None


@dataclass(frozen=True)
class _Frame:
    node: object
    tokens: tuple
    owner: str  # pointer of the entity the subschema belongs to
    own: tuple  # keyword path from the owner's parent entity to the owner
    rel: tuple  # keyword path from the owner to this subschema
    base: str | None
    depth: int  # entity ancestors of the owner
    via: str | None
    extends: bool
    line: int  # fallback when the node carries none
    entity: _EntityInfo | None
    # Names required by the enclosing node and, for allOf, by sibling members.
    inherited: tuple = ()
    # Beneath a `_REFS_ONLY` keyword: refs are kept, fields are not created.
    refs_only: bool = False


def _reads_id(root_schema: object, keyword: str) -> bool:
    """Whether `keyword` (`$id` or `id`) can declare an id, given the root dialect.

    `id` counts under draft-03/04 and under an unknown dialect (no `$schema`, a
    non-string one, or one not on json-schema.org). Where both keywords are
    present `_declared_id` reads `$id` first, so `id` is only ever the fallback.
    """
    if keyword == "$id":
        return True
    if isinstance(root_schema, str) and ("draft-03" in root_schema or "draft-04" in root_schema):
        return True
    return not isinstance(root_schema, str) or "json-schema.org" not in root_schema


def _declared_id(document: dict, root_schema: object) -> str | None:
    for keyword in ("$id", "id"):
        value = document.get(keyword)
        if isinstance(value, str) and _reads_id(root_schema, keyword):
            return value
    return None


def _metadata(node: object, info: _EntityInfo, root_schema: object, declared: str | None) -> dict:
    meta: dict = {}
    if info.kind == kinds.SCHEMA:
        if isinstance(root_schema, str):
            meta["draft"] = root_schema
        if declared is not None:
            meta["schema_id"] = declared
    if not isinstance(node, dict):
        if info.required is not None:
            meta["required"] = info.required
        return meta
    if isinstance(node.get("title"), str):
        meta["title"] = node["title"]
    if isinstance(node.get("description"), str):
        meta["description"] = node["description"][:_DESCRIPTION_LIMIT]
    kind = node.get("type")
    if isinstance(kind, str) or (isinstance(kind, list) and all(isinstance(k, str) for k in kind)):
        meta["type"] = list(kind) if isinstance(kind, list) else kind
    if isinstance(node.get("format"), str):
        meta["format"] = node["format"]
    if isinstance(node.get("enum"), list):
        meta["enum"] = node["enum"][:_ENUM_LIMIT]
        meta["enum_count"] = len(node["enum"])
    if info.required is not None:
        meta["required"] = info.required
    if info.via is not None:
        meta["via"] = info.via
    return meta


def walk(document: object, lines: dict[int, int], file_name: str) -> Walked:
    """Walk one schema document into entities, `$ref`s, declared ids and anchors."""
    walked = Walked([], [], [], {}, document, False)
    if not isinstance(document, dict):
        return walked
    root_schema = document.get("$schema")
    title = document.get("title")
    root = _EntityInfo(
        kinds.SCHEMA, title if isinstance(title, str) and title else file_name, None, None, None
    )
    stack = [_Frame(document, (), "", (), (), None, 0, None, False, 1, root)]
    while stack:
        frame = stack.pop()
        _visit(frame, lines, root_schema, walked, stack)
    return walked


def _visit(
    frame: _Frame, lines: dict[int, int], root_schema: object, walked: Walked, stack: list[_Frame]
) -> None:
    node = frame.node
    pointer = _pointer(frame.tokens)
    line = lines.get(id(node), frame.line) if isinstance(node, dict) else frame.line
    declared = _declared_id(node, root_schema) if isinstance(node, dict) else None
    if frame.entity is not None:
        info = frame.entity
        walked.entities.append(
            Entity(
                pointer,
                info.kind,
                info.label,
                line,
                info.parent,
                _metadata(node, info, root_schema, declared),
            )
        )
    if not isinstance(node, dict):
        return
    base = _enter_declarations(node, declared, frame.base, pointer, walked)
    context = "/".join(str(t) for t in frame.own + frame.rel)
    relation = "extends" if frame.extends and not frame.refs_only else "references"
    for keyword in _REF_KEYWORDS:
        if keyword in node:
            walked.refs.append(
                Ref(frame.owner, node[keyword], relation, context, line, base, keyword)
            )
    ctx = _Expansion(frame, node, base, line, walked)
    for keyword, value in node.items():
        handler = _HANDLERS.get(keyword)
        if handler is not None:
            handler(ctx, keyword, value)
    stack.extend(reversed(ctx.children))


class _Expansion:
    """The children of one subschema, in document order, and the refs found on the way."""

    def __init__(self, frame: _Frame, node: dict, base: str | None, line: int, walked: Walked):
        self.frame = frame
        self.node = node
        self.base = base
        self.line = line
        self.walked = walked
        self.children: list[_Frame] = []

    def plain(
        self, sub: object, key: tuple, via: str | None, extends: bool = False, inherited: tuple = ()
    ) -> None:
        f = self.frame
        refs_only = f.refs_only or key[0] in _REFS_ONLY
        self.children.append(
            _Frame(sub, f.tokens + key, f.owner, f.own, f.rel + key, self.base,
                   f.depth, via, extends, self.line, None, inherited, refs_only)
        )  # fmt: skip

    def entity(self, sub: object, key: tuple, kind: str, name: str, required: bool | None) -> None:
        f = self.frame
        if f.refs_only and kind == kinds.FIELD:
            self.plain(sub, key, f.via)
            return
        if f.depth + 1 > MAX_DEPTH:
            self.walked.truncated = True
            return
        tokens = f.tokens + key
        via = f.via if kind == kinds.FIELD else None
        info = _EntityInfo(kind, name, f.owner, required, via)
        self.children.append(
            _Frame(sub, tokens, _pointer(tokens), f.rel + key, (), self.base,
                   f.depth + 1, None, False, self.line, info)
        )  # fmt: skip


def _required_names(node: object) -> tuple:
    required = node.get("required") if isinstance(node, dict) else None
    return tuple(n for n in required if isinstance(n, str)) if isinstance(required, list) else ()


def _on_properties(ctx: _Expansion, keyword: str, value: object) -> None:
    if not isinstance(value, dict):
        return
    required = _required_names(ctx.node) + ctx.frame.inherited
    for name, sub in value.items():
        listed = name in required
        draft3 = isinstance(sub, dict) and sub.get("required") is True
        ctx.entity(sub, ("properties", name), kinds.FIELD, name, listed or draft3)


def _on_definitions(ctx: _Expansion, keyword: str, value: object) -> None:
    if isinstance(value, dict):
        for name, sub in value.items():
            ctx.entity(sub, (keyword, name), kinds.DEFINITION, name, None)


def _on_items(ctx: _Expansion, keyword: str, value: object) -> None:
    if isinstance(value, list):
        for i, sub in enumerate(value):
            ctx.plain(sub, ("items", i), ctx.frame.via)
    else:
        ctx.plain(value, ("items",), ctx.frame.via)


def _on_combinator(ctx: _Expansion, keyword: str, value: object) -> None:
    if not isinstance(value, list):
        return
    # A name the enclosing node requires is required of a member's fields. Only
    # allOf members all apply at once, so only there does a sibling's `required`
    # count; a oneOf or anyOf sibling may not be the branch that matches.
    outer = ctx.frame.inherited + _required_names(ctx.node)
    siblings = tuple(n for sub in value for n in _required_names(sub)) if keyword == "allOf" else ()
    for i, sub in enumerate(value):
        ctx.plain(sub, (keyword, i), keyword, keyword == "allOf", outer + siblings)


def _on_single(ctx: _Expansion, keyword: str, value: object) -> None:
    ctx.plain(value, (keyword,), ctx.frame.via)


def _on_tuple(ctx: _Expansion, keyword: str, value: object) -> None:
    if isinstance(value, list):
        for i, sub in enumerate(value):
            ctx.plain(sub, (keyword, i), ctx.frame.via)


def _on_pattern_properties(ctx: _Expansion, keyword: str, value: object) -> None:
    if isinstance(value, dict):
        for name, sub in value.items():
            ctx.plain(sub, (keyword, name), ctx.frame.via)


def _on_extends(ctx: _Expansion, keyword: str, value: object) -> None:
    f = ctx.frame
    for i, sub in enumerate(value if isinstance(value, list) else [value]):
        key = ("extends", i) if isinstance(value, list) else ("extends",)
        if isinstance(sub, str):
            context = "/".join(str(t) for t in f.own + f.rel + key)
            relation = "references" if f.refs_only else "extends"
            ctx.walked.refs.append(Ref(f.owner, sub, relation, context, ctx.line, ctx.base))
        else:
            ctx.plain(sub, key, f.via, True)


_HANDLERS = {
    "properties": _on_properties,
    "definitions": _on_definitions,
    "$defs": _on_definitions,
    "items": _on_items,
    "allOf": _on_combinator,
    "oneOf": _on_combinator,
    "anyOf": _on_combinator,
    "not": _on_single,
    "additionalProperties": _on_single,
    "patternProperties": _on_pattern_properties,
    # Further schema-valued keywords. A `$ref` under any of them is a
    # `references` edge; only `prefixItems` members can hold fields, the rest
    # are `_REFS_ONLY`. `dependencies` also holds arrays of property names,
    # which are not schemas and are skipped on visit.
    "prefixItems": _on_tuple,
    "if": _on_single,
    "then": _on_single,
    "else": _on_single,
    "contains": _on_single,
    "additionalItems": _on_single,
    "propertyNames": _on_single,
    "unevaluatedItems": _on_single,
    "unevaluatedProperties": _on_single,
    "dependencies": _on_pattern_properties,
    "dependentSchemas": _on_pattern_properties,
    "extends": _on_extends,
}


def _enter_declarations(
    node: dict, declared: str | None, base: str | None, pointer: str, walked: Walked
) -> str | None:
    """Record `node`'s anchors and declared id; return the base URI beneath it."""
    anchor = node.get("$anchor")
    if isinstance(anchor, str) and anchor:
        walked.anchors.setdefault(anchor, pointer)
    if declared is None:
        return base
    if declared.startswith("#"):
        name = declared[1:]
        if name and not name.startswith("/"):
            walked.anchors.setdefault(name, pointer)
        return base
    absolute = (
        declared if _is_absolute_uri(declared) else (urljoin(base, declared) if base else None)
    )
    if absolute is None or not _is_absolute_uri(absolute):
        return base
    normalised = normalise_uri(absolute)
    walked.declared.append((normalised, pointer))
    return normalised
