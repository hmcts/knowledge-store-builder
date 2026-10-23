"""Invented JSON Schema documents shared by the json-schema stage tests.

The layout is exact: expected line numbers are derived by hand from it, so do
not reformat these strings. Each test writes the files it needs.
"""

# repo `widgets`, path `schemas/widget-created.json`. Lines: root 1, widgetId 7,
# owner 8, parts 9, Part 13, sku 15. The root brace must stay on line 1.
WIDGET_CREATED = """{
  "$schema": "http://json-schema.org/draft-04/schema#",
  "id": "http://schemas.example/widgets/widget-created.json",
  "title": "WidgetCreated",
  "type": "object",
  "properties": {
    "widgetId": {"type": "string", "description": "The widget."},
    "owner": {"$ref": "http://schemas.example/common/person.json"},
    "parts": {"type": "array", "items": {"$ref": "#/definitions/Part"}}
  },
  "required": ["widgetId"],
  "definitions": {
    "Part": {
      "type": "object",
      "properties": {"sku": {"type": "string", "enum": ["a", "b"]}}
    }
  }
}
"""

# repo `common`, path `person.json`; the same text also stands in for
# `widgets-fork/person.json` and `widgets/vendor/person.json`.
PERSON = """{
  "$schema": "http://json-schema.org/draft-04/schema#",
  "id": "http://schemas.example/common/person.json",
  "type": "object",
  "properties": {
    "name": {"type": "string"}
  }
}
"""

# repo `events`, path `events/base.json`
BASE_2020 = (
    '{"$schema": "https://json-schema.org/draft/2020-12/schema", '
    '"$id": "https://schemas.example/events/base.json", '
    '"$defs": {"Envelope": {"type": "object", "properties": '
    '{"at": {"type": "string", "format": "date-time"}}}}}\n'
)

# repo `events`, path `events/order-placed.json`
ORDER_PLACED_2020 = (
    '{"$schema": "https://json-schema.org/draft/2020-12/schema", '
    '"$id": "https://schemas.example/events/order-placed.json", '
    '"allOf": [{"$ref": "base.json#/$defs/Envelope"}, '
    '{"properties": {"orderId": {"type": "string"}}}]}\n'
)

# repo `widgets`, path `schemas/plain.json`: `$schema` and `id` but no
# definitions, the shape graphify 0.9.94 keyword-walks.
PLAIN_DRAFT04 = (
    '{"$schema": "http://json-schema.org/draft-04/schema#", '
    '"id": "http://schemas.example/widgets/plain.json", '
    '"type": "object", "properties": {"colour": {"type": "string"}}}\n'
)

# repo `common`, path `child.json`: no id, extends by path, relative ref.
POJO_CHILD = (
    '{"$schema": "http://json-schema.org/draft-04/schema#", '
    '"type": "object", "extends": {"$ref": "person.json"}, '
    '"properties": {"nickname": {"type": "string"}}}\n'
)
