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

import json
import json.decoder
import json.scanner
import re
from typing import Any

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
    """The text is not valid JSON."""


def load_with_lines(text: str) -> tuple[object, dict[int, int]]:
    """Decode `text`; also map `id(obj)` to the 1-based line of each object's `{`."""
    if text.startswith("﻿"):
        text = text[1:]
    starts: dict[int, int] = {}
    decoder: Any = json.JSONDecoder()

    def parse_object(s_and_end, strict, scan_once, object_hook, object_pairs_hook, memo):
        s, after_brace = s_and_end
        result = _decoder_module.JSONObject(
            s_and_end, strict, scan_once, object_hook, object_pairs_hook, memo
        )
        starts[id(result[0])] = s.count("\n", 0, after_brace - 1) + 1
        return result

    decoder.parse_object = parse_object
    # The C scanner ignores an overridden parse_object; rebuild with the Python one.
    decoder.scan_once = _scanner_module.py_make_scanner(decoder)
    try:
        value = decoder.decode(text)
    except json.JSONDecodeError as exc:
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
